#!/usr/bin/env python3
"""
claude-usage-collect ― Claude Code 로컬 사용량 수집기

~/.claude/projects/**/*.jsonl (Claude Code 세션 트랜스크립트)를 파싱해
일자·모델·프로젝트·시간대별 토큰 사용량으로 집계하고, 머신 라벨을 붙인
단일 JSON 파일로 내보낸다. 표준 라이브러리만 사용한다 (Python 3.6+).

사용 예:
    python3 claude-usage-collect.py                       # ~/claude-usage/<machine>.json 으로 출력
    python3 claude-usage-collect.py -o ./data/laptop.json  # 경로 지정
    python3 claude-usage-collect.py --machine "work-mac"   # 머신 라벨 지정
    python3 claude-usage-collect.py --since 2026-01-01     # 기간 제한
    python3 claude-usage-collect.py --pricing pricing.json # 비용 추정 켜기
    python3 claude-usage-collect.py --print-summary        # 터미널 요약 출력

주의: 토큰 수치는 Claude Code가 로컬 트랜스크립트에 기록한 값이다.
Anthropic 청구 기준 수치가 아니며, 구독제 사용량 한도와도 직접 대응하지 않는다.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# Claude Code가 usage를 기록하는 필드들
USAGE_FIELDS = (
    ("input_tokens", "i"),
    ("output_tokens", "o"),
    ("cache_creation_input_tokens", "cw"),
    ("cache_read_input_tokens", "cr"),
)

# 집계에서 제외할 model 값
SKIP_MODELS = {"<synthetic>", "synthetic", None, ""}


# ---------------------------------------------------------------- helpers


def local_tz():
    """시스템 로컬 타임존 (IANA 이름을 알아낼 수 있으면 그것, 아니면 UTC offset)."""
    name = os.environ.get("TZ")
    if not name:
        tzpath = Path("/etc/localtime")
        try:
            if tzpath.is_symlink():
                p = os.readlink(tzpath)
                if "zoneinfo/" in p:
                    name = p.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    if not name:
        name = time.tzname[0] if time.tzname else "UTC"
    return name


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,6})\d*)?(Z|z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(raw):
    """ISO8601 (Z / 오프셋 포함) → aware datetime. 실패 시 None.

    datetime.fromisoformat 은 3.7+ 이라 직접 파싱한다 (3.6 호환).
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _ISO_RE.match(raw.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0"))
    if tz in (None, "", "Z", "z"):
        off = timezone.utc
    else:
        digits = tz[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
        off = timezone(-delta if tz[0] == "-" else delta)
    try:
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), micro, off)
    except ValueError:
        return None


def decode_project(dirname):
    """
    ~/.claude/projects/ 하위 디렉터리 이름은 프로젝트 절대경로를 인코딩한 것이다.
    (예: -home-user-work-myapp) 마지막 세그먼트만 프로젝트 이름으로 쓴다.
    """
    if not dirname:
        return "unknown"
    cleaned = dirname.strip("-")
    parts = [p for p in cleaned.split("-") if p]
    return parts[-1] if parts else dirname


def machine_identity(label):
    host = socket.gethostname()
    lbl = label or host
    raw = f"{host}|{platform.system()}|{platform.node()}"
    mid = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return {
        "id": mid,
        "label": lbl,
        "hostname": host,
        "os": f"{platform.system()} {platform.release()}".strip(),
    }


def new_bucket():
    return {"i": 0, "o": 0, "cw": 0, "cr": 0, "m": 0}


def add_usage(bucket, u):
    for src, dst in USAGE_FIELDS:
        v = u.get(src)
        if isinstance(v, (int, float)) and v > 0:
            bucket[dst] += int(v)
    bucket["m"] += 1


def bucket_total(b):
    return b["i"] + b["o"] + b["cw"] + b["cr"]


# ---------------------------------------------------------------- scanning


def iter_records(root, verbose=False):
    """
    ~/.claude/projects 아래 모든 .jsonl 을 순회하며
    (project, session_id, timestamp, model, usage, dedup_key) 를 yield.
    """
    files = sorted(root.rglob("*.jsonl"))
    if verbose:
        print(f"  {len(files)}개 트랜스크립트 파일 발견", file=sys.stderr)

    for path in files:
        try:
            rel = path.relative_to(root)
            project = decode_project(rel.parts[0]) if rel.parts else "unknown"
        except ValueError:
            project = "unknown"

        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"  ! 열기 실패 {path}: {e}", file=sys.stderr)
            continue

        with fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue

                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                model = msg.get("model") or rec.get("model")
                if model in SKIP_MODELS:
                    continue

                dt = parse_ts(rec.get("timestamp"))
                if dt is None:
                    continue

                # 중복 제거 키: 같은 assistant 응답이 세션 재개/포크 시
                # 여러 파일에 중복 기록될 수 있다.
                dedup = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                if dedup and rec.get("requestId"):
                    dedup = f"{dedup}:{rec['requestId']}"

                yield {
                    "project": project,
                    "session": rec.get("sessionId") or path.stem,
                    "dt": dt,
                    "model": str(model),
                    "usage": usage,
                    "dedup": dedup,
                    "sidechain": bool(rec.get("isSidechain")),
                }


def aggregate(root, since=None, until=None, verbose=False):
    daily = defaultdict(new_bucket)
    models = defaultdict(new_bucket)
    projects = defaultdict(new_bucket)
    daily_models = defaultdict(lambda: defaultdict(int))
    hours = [0] * 24
    weekday_hour = [[0] * 24 for _ in range(7)]

    daily_sessions = defaultdict(set)
    project_last = {}
    all_sessions = set()

    seen = set()
    dup_count = 0
    kept = 0

    for rec in iter_records(root, verbose=verbose):
        key = rec["dedup"]
        if key:
            if key in seen:
                dup_count += 1
                continue
            seen.add(key)

        local_dt = rec["dt"].astimezone()
        day = local_dt.strftime("%Y-%m-%d")
        if since and day < since:
            continue
        if until and day > until:
            continue

        u = rec["usage"]
        add_usage(daily[day], u)
        add_usage(models[rec["model"]], u)
        add_usage(projects[rec["project"]], u)

        tot = sum(
            int(u.get(src, 0) or 0) for src, _ in USAGE_FIELDS
        )
        daily_models[day][rec["model"]] += tot

        hours[local_dt.hour] += 1
        weekday_hour[local_dt.weekday()][local_dt.hour] += 1

        daily_sessions[day].add(rec["session"])
        all_sessions.add(rec["session"])
        prev = project_last.get(rec["project"])
        if prev is None or day > prev:
            project_last[rec["project"]] = day
        kept += 1

    if verbose:
        print(f"  집계 {kept}건 / 중복 제외 {dup_count}건", file=sys.stderr)

    for day, sess in daily_sessions.items():
        daily[day]["s"] = len(sess)

    for name, b in projects.items():
        b["last"] = project_last.get(name)

    return {
        "daily": dict(daily),
        "models": dict(models),
        "projects": dict(projects),
        "daily_models": {d: dict(m) for d, m in daily_models.items()},
        "hours": hours,
        "weekday_hour": weekday_hour,
        "sessions": len(all_sessions),
        "records": kept,
        "duplicates": dup_count,
    }


# ---------------------------------------------------------------- derived


def streaks(days_sorted):
    """연속 사용 일수: (현재 연속, 최장 연속). 오늘/어제까지 이어지면 현재 연속으로 인정."""
    if not days_sorted:
        return 0, 0
    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in days_sorted]
    best = cur = 1
    run = 1
    for a, b in zip(dates, dates[1:]):
        if (b - a).days == 1:
            run += 1
        else:
            run = 1
        best = max(best, run)
    today = datetime.now().date()
    gap = (today - dates[-1]).days
    cur = run if gap <= 1 else 0
    return cur, best


def load_pricing(path):
    """
    비용 추정용 단가표. USD / 1M tokens 기준.
    형식: {"model-substring": {"input":X,"output":Y,"cache_write":Z,"cache_read":W}, ...}
    부분 문자열 매칭(가장 긴 것 우선). 단가는 사용자가 직접 채워야 한다.
    """
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def estimate_cost(models_agg, pricing):
    if not pricing:
        return None
    keys = sorted(pricing.keys(), key=len, reverse=True)
    per_model = {}
    total = 0.0
    unmatched = []
    for model, b in models_agg.items():
        hit = next((k for k in keys if k in model), None)
        if not hit:
            unmatched.append(model)
            continue
        p = pricing[hit]
        c = (
            b["i"] * p.get("input", 0)
            + b["o"] * p.get("output", 0)
            + b["cw"] * p.get("cache_write", 0)
            + b["cr"] * p.get("cache_read", 0)
        ) / 1_000_000
        per_model[model] = round(c, 4)
        total += c
    return {
        "currency": "USD",
        "total": round(total, 2),
        "per_model": per_model,
        "unpriced_models": unmatched,
        "note": "사용자 제공 단가표 기반 추정치. 실제 청구액이 아님.",
    }


# ---------------------------------------------------------------- main


def build_payload(args):
    root = Path(args.claude_dir).expanduser() / "projects"
    if not root.is_dir():
        raise SystemExit(
            f"트랜스크립트 디렉터리를 찾을 수 없습니다: {root}\n"
            f"--claude-dir 로 Claude Code 홈(기본 ~/.claude)을 지정하세요."
        )

    if args.verbose:
        print(f"스캔: {root}", file=sys.stderr)

    agg = aggregate(root, since=args.since, until=args.until, verbose=args.verbose)
    daily = agg["daily"]
    days_sorted = sorted(daily.keys())

    totals = new_bucket()
    for b in daily.values():
        for k in ("i", "o", "cw", "cr", "m"):
            totals[k] += b.get(k, 0)
    totals["total"] = bucket_total(totals)
    totals["sessions"] = agg["sessions"]
    totals["active_days"] = len(days_sorted)

    cur_streak, max_streak = streaks(days_sorted)
    peak_hour = max(range(24), key=lambda h: agg["hours"][h]) if any(agg["hours"]) else None
    top_model = (
        max(agg["models"].items(), key=lambda kv: bucket_total(kv[1]))[0]
        if agg["models"]
        else None
    )

    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tz": local_tz(),
        "machine": machine_identity(args.machine),
        "source": {
            "kind": "claude-code-jsonl",
            "root": str(root),
            "records": agg["records"],
            "duplicates_skipped": agg["duplicates"],
        },
        "range": {
            "first": days_sorted[0] if days_sorted else None,
            "last": days_sorted[-1] if days_sorted else None,
        },
        "totals": totals,
        "streak": {"current": cur_streak, "max": max_streak},
        "peak_hour": peak_hour,
        "top_model": top_model,
        "daily": daily,
        "daily_models": agg["daily_models"],
        "models": agg["models"],
        "projects": agg["projects"],
        "hours": agg["hours"],
        "weekday_hour": agg["weekday_hour"],
    }

    cost = estimate_cost(agg["models"], load_pricing(args.pricing))
    if cost:
        payload["cost_estimate"] = cost

    return payload


def console_safe(text):
    """콘솔 인코딩으로 못 쓰는 문자를 대체 문자로 낮춘다.

    cp949(한국어)·cp932(일본어) 콘솔에는 em dash(U+2014) 슬롯이 없다.
    한글 수백 자는 멀쩡히 나가는데 그 한 글자에서 argparse 가 --help 를
    찍다 죽는다. 도움말은 사람이 읽는 글이니 크래시보다 대체가 낫다.

    stdout 으로 나가는 JSON 에는 절대 쓰지 마라. 조용히 데이터가 바뀐다.
    """
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return text
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, "replace").decode(enc, "replace")


def human(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return str(n)


def print_summary(p):
    t = p["totals"]
    print()
    print(f"  머신          {p['machine']['label']}  ({p['machine']['os']})")
    print(f"  기간          {p['range']['first']} ~ {p['range']['last']}  ({p['tz']})")
    print(f"  총 토큰       {human(t['total'])}")
    print(
        f"    입력 {human(t['i'])} / 출력 {human(t['o'])} / "
        f"캐시쓰기 {human(t['cw'])} / 캐시읽기 {human(t['cr'])}"
    )
    print(f"  메시지        {t['m']:,}")
    # em dash 는 cp949 콘솔에서 죽는다. int 로 감싸는 것도 필수 ― human() 은
    # 1000 미만이면 str(n) 을 그대로 돌려주므로 float 이 그대로 새어 나온다.
    print(f"  턴당 컨텍스트 {human(int(t['cr'] / t['m'])) if t['m'] else '-'}")
    print(f"  세션          {t['sessions']:,}")
    print(f"  활성 일수     {t['active_days']}  (현재 연속 {p['streak']['current']}일 / 최장 {p['streak']['max']}일)")
    if p.get("peak_hour") is not None:
        print(f"  최다 사용 시간 {p['peak_hour']:02d}시")
    if p.get("top_model"):
        print(f"  주 사용 모델   {p['top_model']}")
    if p.get("cost_estimate"):
        print(f"  비용 추정      ${p['cost_estimate']['total']:,.2f} (사용자 단가표 기준)")
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Claude Code 로컬 트랜스크립트에서 토큰 사용량을 집계합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=console_safe(__doc__),
    )
    ap.add_argument("-o", "--output", help="출력 JSON 경로 (기본: ~/claude-usage/<machine>.json)")
    ap.add_argument("--machine", help="머신 라벨 (기본: hostname)")
    ap.add_argument("--claude-dir", default="~/.claude", help="Claude Code 홈 (기본 ~/.claude)")
    ap.add_argument("--since", help="시작일 YYYY-MM-DD")
    ap.add_argument("--until", help="종료일 YYYY-MM-DD")
    ap.add_argument("--pricing", help="비용 추정 단가표 JSON 경로 (USD/1M tokens)")
    ap.add_argument("--print-summary", action="store_true", help="터미널에 요약 출력")
    ap.add_argument("--stdout", action="store_true", help="파일 대신 stdout으로 JSON 출력")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    payload = build_payload(args)

    if args.stdout:
        json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
    else:
        if args.output:
            out = Path(args.output).expanduser()
        else:
            safe = "".join(
                c if (c.isalnum() or c in "-_") else "-"
                for c in payload["machine"]["label"]
            )
            out = Path.home() / "claude-usage" / f"{safe}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(out)
        size = out.stat().st_size
        print(f"저장됨: {out}  ({size/1024:.1f} KB)", file=sys.stderr)

    if args.print_summary:
        print_summary(payload)


if __name__ == "__main__":
    main()
