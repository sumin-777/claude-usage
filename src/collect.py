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

SCHEMA_VERSION = 2

# Claude Code가 usage를 기록하는 필드들
LEGACY_USAGE_FIELDS = (
    (("input_tokens",), "i"),
    (("output_tokens",), "o"),
    (("cache_creation_input_tokens",), "cw"),
    (("cache_read_input_tokens",), "cr"),
)
USAGE_FIELDS = LEGACY_USAGE_FIELDS + (
    (("cache_creation", "ephemeral_1h_input_tokens"), "cw1"),
    (("cache_creation", "ephemeral_5m_input_tokens"), "cw5"),
    (("output_tokens_details", "thinking_tokens"), "th"),
)
BUCKET_KEYS = ("i", "o", "cw", "cr", "cw1", "cw5", "th", "m")
PLAN_FIELDS = (
    "organizationType", "organizationRateLimitTier", "hasExtraUsageEnabled",
    "seatTier", "userRateLimitTier", "billingType",
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
    return {k: 0 for k in BUCKET_KEYS}


def normalize_payload_usage(payload):
    for bucket in [payload.get("totals", {})]:
        if isinstance(bucket, dict):
            for key in ("cw1", "cw5", "th"):
                bucket.setdefault(key, 0)
    for section in ("daily", "models", "projects"):
        buckets = payload.get(section, {})
        if not isinstance(buckets, dict):
            continue
        for bucket in buckets.values():
            if isinstance(bucket, dict):
                for key in ("cw1", "cw5", "th"):
                    bucket.setdefault(key, 0)
    codex = payload.get("codex")
    if isinstance(codex, dict):
        cdaily = codex.get("daily", {})
        buckets = list(cdaily.values()) if isinstance(cdaily, dict) else []
        for bucket in [codex.get("totals", {})] + buckets:
            if isinstance(bucket, dict):
                for key in BUCKET_KEYS:
                    bucket.setdefault(key, 0)
    return payload


def usage_value(usage, path):
    value = usage
    for part in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(part)
    if not isinstance(value, (int, float)) or value <= 0:
        return 0
    try:
        return int(value)
    except (OverflowError, ValueError):
        return 0


def usage_values(usage):
    return [usage_value(usage, path) for path, _ in USAGE_FIELDS]


def usage_from_values(values):
    usage = {}
    for value, (path, _) in zip(values, USAGE_FIELDS):
        target = usage
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = value
    return usage


def add_usage(bucket, u):
    for path, dst in USAGE_FIELDS:
        bucket[dst] = bucket.get(dst, 0) + usage_value(u, path)
    bucket["m"] += 1


def bucket_total(b):
    return b["i"] + b["o"] + b["cw"] + b["cr"]


def add_bucket(dst, src):
    for key in BUCKET_KEYS:
        dst[key] = dst.get(key, 0) + src.get(key, 0)


def load_plan(claude_dir):
    """~/.claude.json 의 공개 가능한 요금제 라벨만 읽는다."""
    path = Path(str(Path(claude_dir).expanduser()) + ".json")
    try:
        with path.open("r", encoding="utf-8") as f:
            account = json.load(f).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(account, dict):
        return None
    plan = {k: account[k] for k in PLAN_FIELDS if k in account}
    return plan or None


def quota_hit(rec, project, session):
    q = rec.get("quotaLimits")
    timestamp = rec.get("timestamp")
    if not isinstance(q, dict) or not timestamp or parse_ts(timestamp) is None:
        return None
    hit = {"timestamp": timestamp, "session": session, "project": project}
    for key in ("rateLimitType", "resetsAt", "status", "isUsingOverage",
                "overageStatus", "overageDisabledReason"):
        if key in q:
            hit[key] = q[key]
    hit["requestId"] = rec.get("requestId")
    return hit


def finish_limit_hits(hits, since=None, until=None):
    """requestId+timestamp 로 중복 제거하고, 기간을 거른 뒤 최신 200건만 남긴다.

    기간 판정은 일별 버킷과 같게 로컬 날짜 기준이다. 두 경로(전체/증분)가 여기
    하나만 쓰므로 필터가 어긋나지 않는다.
    """
    best = {}
    for hit in hits:
        dt = parse_ts(hit.get("timestamp"))
        if dt is not None and (since or until):
            day = dt.astimezone().strftime("%Y-%m-%d")
            if (since and day < since) or (until and day > until):
                continue
        best[(hit.get("requestId"), hit.get("timestamp"))] = hit
    return sorted(best.values(), key=lambda x: x.get("timestamp", ""), reverse=True)[:200]


def codex_bucket(last):
    """Codex last_token_usage 를 기존 버킷 키로 옮긴다."""
    b = new_bucket()
    cached = usage_value(last, ("cached_input_tokens",))
    b["i"] = max(0, usage_value(last, ("input_tokens",)) - cached)
    b["cr"] = cached
    b["cw"] = usage_value(last, ("cache_write_input_tokens",))
    b["o"] = usage_value(last, ("output_tokens",))
    b["th"] = usage_value(last, ("reasoning_output_tokens",))
    b["m"] = 1
    return b


def parse_codex_file(path, start_off=0, previous_total=None):
    """Codex JSONL 의 완결된 꼬리만 읽는다."""
    daily = {}
    newest = None
    peaks = {}
    off = start_off
    try:
        fh = open(str(path), "rb")
    except OSError:
        return daily, previous_total, newest, start_off
    with fh:
        try:
            fh.seek(start_off)
        except OSError:
            fh.seek(0)
            off = 0
        while True:
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            off += len(raw)
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload", {})
            if not isinstance(payload, dict):
                continue
            info = payload.get("info") if (rec.get("type") == "event_msg" and
                                            payload.get("type") == "token_count") else None
            if not isinstance(info, dict):
                continue
            timestamp = rec.get("timestamp")
            dt = parse_ts(timestamp)
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if dt is not None and isinstance(total, dict) and isinstance(last, dict) and total != previous_total:
                day = dt.astimezone().strftime("%Y-%m-%d")
                add_bucket(daily.setdefault(day, new_bucket()), codex_bucket(last))
                previous_total = total
            limits = payload.get("rate_limits")
            if dt is not None and isinstance(limits, dict):
                windows = []
                for name in ("primary", "secondary"):
                    window = limits.get(name)
                    if isinstance(window, dict):
                        windows.append({k: window.get(k) for k in
                                        ("used_percent", "window_minutes", "resets_at")})
                # 같은 창 안에서도 보고값이 크게 오르내린다(2026-08-11 실측 9%~38%).
                # 마지막 값만 쓰면 과소 표시되므로 창별 최고치를 따로 모은다.
                for w in windows:
                    pct = w.get("used_percent")
                    if pct is not None:
                        key = "%s:%s" % (w.get("window_minutes"), w.get("resets_at"))
                        peaks[key] = max(peaks.get(key, 0.0), float(pct))
                if newest is None or timestamp > newest["at"]:
                    newest = {"at": timestamp, "plan": limits.get("plan_type"), "windows": windows}
    if newest is not None and peaks:
        newest["peaks"] = peaks
    return daily, previous_total, newest, off


def make_codex_payload(entries, since=None, until=None):
    daily = {}
    newest = None
    peaks = {}
    for entry in entries:
        for day, src in entry.get("daily", {}).items():
            if (since and day < since) or (until and day > until):
                continue
            add_bucket(daily.setdefault(day, new_bucket()), src)
        limit = entry.get("limits")
        if not limit:
            continue
        for key, pct in (limit.get("peaks") or {}).items():
            peaks[key] = max(peaks.get(key, 0.0), pct)
        if newest is None or limit.get("at", "") > newest.get("at", ""):
            newest = limit
    if not daily and newest is None:
        return None
    totals = new_bucket()
    for b in daily.values():
        add_bucket(totals, b)
    totals["total"] = bucket_total(totals)
    out = {"daily": daily, "totals": totals}
    if newest is not None:
        # 캐시에 든 원본은 건드리지 않는다. 내부용 peaks 는 내보내지 않고
        # 창마다 그 창의 최고치(peak_percent)만 붙인다.
        windows = []
        for w in newest.get("windows", []):
            w = dict(w)
            peak = peaks.get("%s:%s" % (w.get("window_minutes"), w.get("resets_at")))
            if peak is not None:
                w["peak_percent"] = peak
            windows.append(w)
        out["limits"] = {"at": newest.get("at"), "plan": newest.get("plan"), "windows": windows}
    return out


def collect_codex(since=None, until=None):
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    if not root.is_dir():
        return None
    entries = []
    for base in (root / "sessions", root / "archived_sessions"):
        if base.is_dir():
            for path in sorted(base.rglob("rollout-*.jsonl")):
                daily, last, limits, off = parse_codex_file(path)
                entries.append({"daily": daily, "last": last, "limits": limits, "off": off})
    return make_codex_payload(entries, since, until)


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

                dt = parse_ts(rec.get("timestamp"))
                if dt is None:
                    continue

                msg = rec.get("message")
                if not isinstance(msg, dict):
                    msg = {}
                usage = msg.get("usage")
                model = msg.get("model") or rec.get("model")
                hit = quota_hit(rec, project, rec.get("sessionId") or path.stem)
                if (not isinstance(usage, dict) or model in SKIP_MODELS) and hit is None:
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
                    "limit_hit": hit,
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
    limit_hits = []

    for rec in iter_records(root, verbose=verbose):
        if rec["limit_hit"] is not None:
            limit_hits.append(rec["limit_hit"])
        # 한도 거절 레코드는 model 이 <synthetic> 이다. 기록만 줍고 사용량에는 넣지 않는다
        # (이 필터가 빠지면 증분 경로와 어긋나 메시지 수가 부풀어 오른다).
        if not isinstance(rec["usage"], dict) or rec["model"] in SKIP_MODELS:
            continue
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

        tot = sum(usage_value(u, path) for path, _ in LEGACY_USAGE_FIELDS)
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
        "limit_hits": finish_limit_hits(limit_hits, since, until),
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
    형식: {"model-substring": {"input":X,"output":Y,"cache_write":Z,
          "cache_write_1h":A,"cache_write_5m":B,"cache_read":W}, ...}
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
    pricing_mode = {}
    total = 0.0
    unmatched = []
    for model, b in models_agg.items():
        hit = next((k for k in keys if k in model), None)
        if not hit:
            unmatched.append(model)
            continue
        p = pricing[hit]
        split = "cache_write_1h" in p or "cache_write_5m" in p
        if split:
            remainder = max(0, b.get("cw", 0) - b.get("cw1", 0) - b.get("cw5", 0))
            cache_cost = (
                b.get("cw1", 0) * p.get("cache_write_1h", p.get("cache_write", 0))
                + b.get("cw5", 0) * p.get("cache_write_5m", p.get("cache_write", 0))
                + remainder * p.get("cache_write", 0)
            )
            pricing_mode[model] = "split"
        else:
            cache_cost = b.get("cw", 0) * p.get("cache_write", 0)
            pricing_mode[model] = "legacy"
        c = (
            b["i"] * p.get("input", 0)
            + b["o"] * p.get("output", 0)
            + cache_cost
            + b["cr"] * p.get("cache_read", 0)
        ) / 1_000_000
        per_model[model] = round(c, 4)
        total += c
    return {
        "currency": "USD",
        "total": round(total, 2),
        "per_model": per_model,
        "pricing_mode": pricing_mode,
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
        for k in BUCKET_KEYS:
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
    plan = load_plan(args.claude_dir)
    if plan:
        payload["plan"] = plan
    if agg["limit_hits"]:
        payload["limit_hits"] = agg["limit_hits"]
    codex = collect_codex(args.since, args.until)
    if codex:
        payload["codex"] = codex

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
    if t.get("cw1", 0) or t.get("cw5", 0):
        remainder = t["cw"] - t.get("cw1", 0) - t.get("cw5", 0)
        detail = f"    캐시 쓰기 중 1h {human(t.get('cw1', 0))} / 5m {human(t.get('cw5', 0))}"
        if remainder > 0:
            detail += f" / 미분류 {human(remainder)}"
        print(detail)
    if t.get("th", 0):
        share = (t["th"] / t["o"] * 100) if t["o"] else 0
        print(f"    출력 중 thinking {human(t['th'])} ({share:.1f}%)")
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
    if p.get("plan"):
        plan = p["plan"]
        print("  Claude 요금제  %s / %s / 추가 사용 %s" % (
            plan.get("organizationType", "-"), plan.get("organizationRateLimitTier", "-"),
            ("켜짐" if plan.get("hasExtraUsageEnabled") else "꺼짐")
            if "hasExtraUsageEnabled" in plan else "-"))
    if p.get("limit_hits"):
        print("  Claude 한도    거절 기록 %s건" % format(len(p["limit_hits"]), ","))
    if p.get("codex"):
        ct = p["codex"].get("totals", {})
        print("  Codex 토큰     %s (요청 %s)" %
              (human(ct.get("total", 0)), format(int(ct.get("m", 0)), ",")))
        for window in p["codex"].get("limits", {}).get("windows", []):
            mins = window.get("window_minutes") or 0
            name = "주간" if mins == 10080 else ("5시간" if mins == 300 else "%s분" % mins)
            try:
                reset = datetime.fromtimestamp(int(window.get("resets_at"))).strftime("%m-%d %H:%M")
            except (TypeError, ValueError):
                reset = "-"
            print("    %s 한도 %.1f%% / 리셋 %s" % (
                name, float(window.get("used_percent") or 0), reset))
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
