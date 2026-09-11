#!/usr/bin/env python3
"""
claude-usage ― Claude Code 토큰 사용량 대시보드 (단일 파일)

이 파일 하나만 있으면 된다. 설치할 것도, 서버도, 계정도 필요 없다.

    python3 claude-usage.py

브라우저가 열리면서 이 PC의 Claude Code 사용량이 바로 보인다.
페이지를 열 때마다 ~/.claude/projects 를 다시 읽으므로 갱신 작업이 따로 없다.

다른 PC·서버 것도 같이 보고 싶으면, 그 머신에서 한 줄만 실행한다.

    python3 claude-usage.py --push http://<이-PC-주소>:8787 --machine "빌드-서버"

네트워크가 닿지 않으면 공유 폴더를 대신 쓴다.

    # 다른 머신에서
    python3 claude-usage.py --export ~/Dropbox/claude-usage/server.json
    # 이 PC에서
    python3 claude-usage.py --watch ~/Dropbox/claude-usage

주의: 토큰 수치는 Claude Code가 로컬 트랜스크립트에 기록한 값이다.
Anthropic 청구 기준 수치가 아니며, 구독 한도와도 직접 대응하지 않는다.
"""

import sys as _sys
if _sys.version_info < (3, 6):
    raise SystemExit(
        "Python 3.6 이상이 필요합니다. 현재: %s\n"
        "  다른 버전이 깔려 있으면 그것으로 실행해 보세요 (예: python3.8 claude-usage.py ...)"
        % _sys.version.split()[0]
    )

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
    print()


# ================================================================= 서버


import argparse
import errno
import http.server
import signal
import subprocess
import json as _json
import re
import shutil
import socket
import socketserver
import threading
import urllib.request
import webbrowser
from pathlib import Path as _Path

STORE = _Path.home() / ".claude-usage"
PAGE = r"""<title>Claude 토큰 미터</title>

<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">

<style>
/* ---------- tokens: warm-paper ledger, cool blue instrument accent ---------- */
:root {
  color-scheme: light;
  --paper:      #faf9f4;
  --panel:      #ffffff;
  --panel-sunk: #f2f0e8;
  --rule:       #e4e1d6;
  --rule-soft:  #eeece3;
  --ink:        #1a1915;
  --ink-2:      #55524699;
  --ink-mute:   #6b6759;
  --accent:     #2a78d6;
  --accent-dim: #2a78d61a;

  --s1: #2a78d6;  /* 캐시 읽기 */
  --s2: #eb6834;  /* 캐시 쓰기 */
  --s3: #1baf7a;  /* 입력 */
  --s4: #eda100;  /* 출력 */
  --s5: #e87ba4;
  --s6: #008300;
  --s7: #4a3aa7;
  --s8: #e34948;

  --heat-0: #f2f0e8;
  --heat-1: #cde2fb;
  --heat-2: #9ec5f4;
  --heat-3: #6da7ec;
  --heat-4: #3987e5;
  --heat-5: #256abf;
  --heat-6: #104281;

  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --sans: "IBM Plex Sans KR", "IBM Plex Sans", -apple-system, BlinkMacSystemFont,
          "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --paper:      #14140f;
    --panel:      #1c1c18;
    --panel-sunk: #24241f;
    --rule:       #33322c;
    --rule-soft:  #2a2a24;
    --ink:        #f3f1e8;
    --ink-2:      #a5a29799;
    --ink-mute:   #a5a297;
    --accent:     #3987e5;
    --accent-dim: #3987e526;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
    --heat-0: #24241f;
    --heat-1: #184f95; --heat-2: #1c5cab; --heat-3: #256abf;
    --heat-4: #3987e5; --heat-5: #6da7ec; --heat-6: #9ec5f4;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper:      #14140f;
  --panel:      #1c1c18;
  --panel-sunk: #24241f;
  --rule:       #33322c;
  --rule-soft:  #2a2a24;
  --ink:        #f3f1e8;
  --ink-2:      #a5a29799;
  --ink-mute:   #a5a297;
  --accent:     #3987e5;
  --accent-dim: #3987e526;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
  --heat-0: #24241f;
  --heat-1: #184f95; --heat-2: #1c5cab; --heat-3: #256abf;
  --heat-4: #3987e5; --heat-5: #6da7ec; --heat-6: #9ec5f4;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 22px 72px; }

/* ---------- ledger primitives ---------- */
.lbl {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--ink-mute);
}
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.hr { height: 1px; background: var(--rule); border: 0; margin: 0; }

/* ---------- header ---------- */
header { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 18px 26px; padding-bottom: 16px; }
h1 { font-size: 19px; font-weight: 600; letter-spacing: -.01em; margin: 0 0 2px; }
.sub { color: var(--ink-mute); font-size: 12.5px; margin: 0; }
.head-meta { margin-left: auto; text-align: right; }

/* meter reading */
.meter { display: flex; align-items: baseline; gap: 10px; margin-top: 4px; }
.meter b {
  font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: 40px; font-weight: 500; letter-spacing: -.02em; line-height: 1;
}
.meter span { font-family: var(--mono); font-size: 13px; color: var(--ink-mute); }

/* ---------- controls ---------- */
.bar {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  padding: 12px 0; border-bottom: 1px solid var(--rule);
}
.seg { display: inline-flex; border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; background: var(--panel); }
.seg button {
  font: inherit; font-family: var(--mono); font-size: 11.5px;
  padding: 5px 11px; border: 0; background: none; color: var(--ink-mute); cursor: pointer;
  border-right: 1px solid var(--rule);
}
.seg button:last-child { border-right: 0; }
.seg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.seg button:hover:not([aria-pressed="true"]) { background: var(--panel-sunk); color: var(--ink); }

.chip {
  display: inline-flex; align-items: center; gap: 7px;
  font-family: var(--mono); font-size: 11.5px;
  padding: 4px 10px 4px 8px; border: 1px solid var(--rule); border-radius: 999px;
  background: var(--panel); color: var(--ink); cursor: pointer;
}
.chip .dot { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.chip[aria-pressed="false"] { opacity: .42; }
.chip:focus-visible, .seg button:focus-visible, .btn:focus-visible, .drop:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px;
}
.spacer { margin-left: auto; }
.btn {
  font: inherit; font-family: var(--mono); font-size: 11.5px;
  padding: 5px 12px; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--panel); color: var(--ink); cursor: pointer;
}
.btn:hover { background: var(--panel-sunk); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.primary:hover { filter: brightness(1.08); }
.btn:disabled { opacity: .45; cursor: default; }

/* ---------- banner ---------- */
.banner {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  margin: 14px 0 0; padding: 9px 13px;
  border: 1px dashed var(--rule); border-radius: 8px;
  background: var(--panel-sunk); color: var(--ink-mute); font-size: 12.5px;
}
.banner b { color: var(--ink); font-weight: 600; }

/* ---------- stat row ---------- */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 0; margin: 22px 0 0; }
.stat { padding: 2px 16px 2px 0; border-left: 1px solid var(--rule-soft); padding-left: 16px; }
.stats > .stat:first-child { border-left: 0; padding-left: 0; }
.stat .v { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 22px; font-weight: 500; letter-spacing: -.01em; display: block; margin-top: 3px; }
.stat .u { font-size: 11.5px; color: var(--ink-mute); font-family: var(--mono); }

/* ---------- sections ---------- */
section { margin-top: 34px; }
.sec-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.sec-head h2 { font-size: 14px; font-weight: 600; margin: 0; letter-spacing: -.005em; }
.sec-head .note { font-size: 12px; color: var(--ink-mute); margin-left: auto; font-family: var(--mono); }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 34px; }
@media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; gap: 30px; } }

/* ---------- charts ---------- */
.chartbox { position: relative; }
.chartbox svg { display: block; width: 100%; height: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; }
.legend i { display: inline-flex; align-items: center; gap: 6px; font-style: normal; font-family: var(--mono); font-size: 11px; color: var(--ink-mute); }
.legend i b { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }

.tip {
  position: absolute; pointer-events: none; z-index: 5;
  background: var(--panel); border: 1px solid var(--rule); border-radius: 7px;
  padding: 8px 10px; font-size: 12px; min-width: 128px;
  box-shadow: 0 6px 20px #0000001f;
  transition: opacity .08s linear;
}
.tip[hidden] { display: none; }
.tip .t-day { font-family: var(--mono); font-size: 11px; color: var(--ink-mute); letter-spacing: .04em; }
.tip .t-tot { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 16px; font-weight: 500; margin: 1px 0 6px; }
.tip table { border-collapse: collapse; width: 100%; }
.tip td { padding: 1px 0; font-size: 11.5px; }
.tip td:last-child { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; padding-left: 14px; }
.tip .sw { width: 8px; height: 8px; border-radius: 2px; display: inline-block; margin-right: 6px; }

/* ---------- composition bar ---------- */
.comp { display: flex; height: 34px; border-radius: 5px; overflow: hidden; gap: 2px; background: var(--paper); }
.comp > div { min-width: 2px; }
.comp-rows { margin-top: 12px; }

/* ---------- ledger table ---------- */
table.ledger { width: 100%; border-collapse: collapse; }
table.ledger th {
  text-align: left; font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  letter-spacing: .12em; text-transform: uppercase; color: var(--ink-mute);
  padding: 0 0 7px; border-bottom: 1px solid var(--rule); white-space: nowrap;
}
table.ledger td { padding: 9px 0; border-bottom: 1px solid var(--rule-soft); vertical-align: middle; }
table.ledger th + th, table.ledger td + td { padding-left: 18px; }
table.ledger th.r, table.ledger td.r { text-align: right; }
table.ledger td.r { font-family: var(--mono); font-variant-numeric: tabular-nums; }
table.ledger td.mut { color: var(--ink-mute); font-size: 12.5px; }
.tbl-scroll { overflow-x: auto; }
.mname { display: flex; align-items: center; gap: 9px; }
.mname .dot { width: 9px; height: 9px; border-radius: 2px; flex: none; }
.mname small { display: block; color: var(--ink-mute); font-size: 11px; font-family: var(--mono); line-height: 1.3; }
.spark { width: 108px; height: 24px; display: block; }
.mini { height: 6px; background: var(--panel-sunk); border-radius: 3px; overflow: hidden; min-width: 60px; }
.mini > i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
.stale { color: var(--s2); }

/* ---------- heatmap ---------- */
.heat { display: grid; grid-template-columns: 26px repeat(24, 1fr); gap: 3px; align-items: center; }
.heat .hl { font-family: var(--mono); font-size: 10px; color: var(--ink-mute); }
.heat .cell { aspect-ratio: 1; border-radius: 2px; background: var(--heat-0); }
.heat-x { display: grid; grid-template-columns: 26px repeat(24, 1fr); gap: 3px; margin-top: 5px; }
.heat-x span { font-family: var(--mono); font-size: 9.5px; color: var(--ink-mute); text-align: center; }
.heat-wrap { overflow-x: auto; }
.heat-inner { min-width: 560px; }

/* ---------- drop zone ---------- */
.drop {
  border: 1px dashed var(--rule); border-radius: 10px; padding: 22px;
  text-align: center; color: var(--ink-mute); background: var(--panel-sunk);
  cursor: pointer; font-size: 13px;
}
.drop.over { border-color: var(--accent); background: var(--accent-dim); color: var(--ink); }
.drop code { font-family: var(--mono); font-size: 12px; color: var(--ink); }
.hint { font-size: 12px; color: var(--ink-mute); margin: 8px 0 0; }
.hint code { font-family: var(--mono); background: var(--panel-sunk); padding: 1px 5px; border-radius: 4px; }

footer { margin-top: 46px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--ink-mute); font-size: 12px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>

<div class="wrap">

  <header>
    <div>
      <h1>Claude 토큰 미터</h1>
      <p class="sub">여러 PC·서버의 Claude Code 사용량을 하나의 계량기로</p>
    </div>
    <div class="head-meta">
      <div class="lbl">누적 검침값</div>
      <div class="meter"><b id="meterTotal">—</b><span id="meterUnit">tokens</span></div>
      <div class="lbl" id="meterRange" style="margin-top:4px">—</div>
    </div>
  </header>

  <div class="bar">
    <div class="seg" role="group" aria-label="기간">
      <button data-range="7" aria-pressed="false">7일</button>
      <button data-range="30" aria-pressed="false">30일</button>
      <button data-range="90" aria-pressed="false">90일</button>
      <button data-range="all" aria-pressed="true">전체</button>
    </div>
    <div id="machineChips" style="display:flex;flex-wrap:wrap;gap:8px"></div>
    <div class="spacer"></div>
    <button class="btn" id="btnAdd">+ 수집 파일 불러오기</button>
    <button class="btn" id="btnSync" hidden>이 대시보드에 저장</button>
    <button class="btn" id="btnExport" hidden>내보내기</button>
    <input type="file" id="fileInput" accept=".json,application/json" multiple hidden>
  </div>

  <div class="banner" id="banner" hidden></div>

  <div class="stats" id="stats"></div>

  <section>
    <div class="sec-head">
      <h2>일별 사용량</h2>
      <div class="note" id="dailyNote"></div>
    </div>
    <div class="chartbox" id="dailyBox">
      <svg id="dailySvg" role="img" aria-label="머신별 일일 토큰 사용량 누적 막대 차트"></svg>
      <div class="tip" id="dailyTip" hidden></div>
    </div>
    <div class="legend" id="dailyLegend"></div>
  </section>

  <section>
    <div class="sec-head">
      <h2>턴당 컨텍스트</h2>
      <div class="note" id="ctxNote">캐시 읽기 ÷ 메시지 · 실효 컨텍스트 크기</div>
    </div>
    <div class="chartbox" id="ctxBox">
      <svg id="ctxSvg" role="img" aria-label="일별 턴당 컨텍스트 추이 라인 차트"></svg>
      <div class="tip" id="ctxTip" hidden></div>
    </div>
    <p class="hint">우상향 추세는 세션 컨텍스트가 점점 길어지고 있다는 신호입니다.</p>
  </section>

  <section class="grid2">
    <div>
      <div class="sec-head">
        <h2>토큰 구성</h2>
        <div class="note">입·출력 vs 캐시</div>
      </div>
      <div class="comp" id="comp"></div>
      <div class="comp-rows">
        <table class="ledger" id="compTable"></table>
      </div>
      <p class="hint" id="compNote"></p>
    </div>
    <div>
      <div class="sec-head">
        <h2>모델별</h2>
        <div class="note" id="modelNote"></div>
      </div>
      <table class="ledger" id="modelTable"></table>
    </div>
  </section>

  <section>
    <div class="sec-head">
      <h2>머신별</h2>
      <div class="note" id="machineNote"></div>
    </div>
    <div class="tbl-scroll">
      <table class="ledger" id="machineTable"></table>
    </div>
  </section>

  <section class="grid2">
    <div>
      <div class="sec-head">
        <h2>프로젝트별</h2>
        <div class="note">상위 8개</div>
      </div>
      <div class="tbl-scroll">
        <table class="ledger" id="projectTable"></table>
      </div>
    </div>
    <div>
      <div class="sec-head">
        <h2>사용 시간대</h2>
        <div class="note" id="heatNote"></div>
      </div>
      <div class="heat-wrap"><div class="heat-inner">
        <div class="heat" id="heat"></div>
        <div class="heat-x" id="heatX"></div>
      </div></div>
    </div>
  </section>

  <section id="loadSection">
    <div class="sec-head"><h2>데이터 연결</h2></div>
    <div class="drop" id="drop" tabindex="0" role="button"
         aria-label="수집 JSON 파일 불러오기">
      각 PC·서버에서 <code>claude-usage-collect.py</code>를 돌려 만든 JSON을
      여기에 끌어다 놓거나 클릭해서 선택하세요.
    </div>
    <p class="hint">
      수집: <code>python3 claude-usage-collect.py --machine "집-데스크탑"</code> ·
      머신 id가 같은 파일을 다시 넣으면 최신 스냅샷으로 교체됩니다.
    </p>
  </section>

  <footer>
    <div id="diag" class="lbl" style="margin-bottom:10px;letter-spacing:.08em"></div>
    수치는 Claude Code가 <code style="font-family:var(--mono)">~/.claude/projects</code>의 세션 트랜스크립트에 직접 기록한
    토큰 값을 합산한 것입니다. Anthropic의 청구·한도 기준 수치가 아니며,
    트랜스크립트가 정리·삭제된 기간은 집계에서 빠집니다.
  </footer>
</div>

<script>
(function () {
  "use strict";

  var SERIES = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
  var COMP = [
    { k: "cr", name: "캐시 읽기", v: "--s1" },
    { k: "cw", name: "캐시 쓰기", v: "--s2" },
    { k: "i",  name: "입력",      v: "--s3" },
    { k: "o",  name: "출력",      v: "--s4" }
  ];
  var WD = ["월","화","수","목","금","토","일"];

  var state = { machines: {}, off: {}, range: "all", sample: true, db: null,
               dirty: {}, scanning: false, scanElapsed: 0 };

  // ------------------------------------------------------------ formatting
  function fmt(n) {
    n = n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(n));
  }
  function comma(n) { return (n || 0).toLocaleString("ko-KR"); }
  function pct(a, b) { return b ? (a / b * 100) : 0; }
  var _cv = {};
  function cvar(name) {
    if (!(name in _cv)) {
      _cv[name] = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim() || "#888";
    }
    return _cv[name];
  }
  function dropStyleCache() { _cv = {}; _colorCache = null; }
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function svgEl(tag, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    return e;
  }
  function shortModel(m) {
    return String(m).replace(/-2\d{7}$/, "").replace(/^claude-/, "");
  }
  // 날짜는 전부 UTC 기준으로 다룬다.
  // "YYYY-MM-DDT00:00:00" 은 로컬로 파싱되는데 toISOString() 은 UTC로 되돌리므로,
  // UTC+9 같은 곳에서는 하루를 더해도 같은 날짜가 나와 루프가 끝나지 않는다.
  function dayKey(d) {
    return d.getUTCFullYear() + "-" +
      ("0" + (d.getUTCMonth() + 1)).slice(-2) + "-" +
      ("0" + d.getUTCDate()).slice(-2);
  }
  function dayParse(iso) { return new Date(iso.slice(0, 10) + "T00:00:00Z"); }
  function dayAdd(iso, n) {
    var d = dayParse(iso);
    d.setUTCDate(d.getUTCDate() + n);
    return dayKey(d);
  }
  function weekdayOf(iso) {           // 0=월 … 6=일
    return (dayParse(iso).getUTCDay() + 6) % 7;
  }
  function todayKey() {
    var n = new Date();
    return n.getFullYear() + "-" +
      ("0" + (n.getMonth() + 1)).slice(-2) + "-" +
      ("0" + n.getDate()).slice(-2);   // 사용자가 보는 '오늘'은 로컬 기준
  }
  function relDays(iso) {
    if (!iso) return null;
    return Math.round((dayParse(todayKey()) - dayParse(iso)) / 86400000);
  }

  // ------------------------------------------------------------ sample data
  function makeSample() {
    var seed = 20260909;
    function rnd() { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }
    var specs = [
      { id: "sample-a", label: "회사-노트북", os: "macOS 15", days: 210, w: 1.0, hours: [9,10,11,13,14,15,16,17] },
      { id: "sample-b", label: "집-데스크탑", os: "Windows 11", days: 210, w: 0.55, hours: [20,21,22,23,0,1] },
      { id: "sample-c", label: "빌드-서버",   os: "Ubuntu 24.04", days: 120, w: 0.3, hours: [2,3,4,9,14] }
    ];
    var models = [
      ["claude-opus-5", 0.34],
      ["claude-sonnet-5", 0.29],
      ["claude-opus-4-8", 0.17],
      ["claude-fable-5", 0.12],
      ["claude-haiku-4-5-20251001", 0.08]
    ];
    var projects = ["moonlog", "bookapp", "api-gateway", "scratch", "etl-jobs"];
    var today = todayKey();
    return specs.map(function (s) {
      var daily = {}, dailyModels = {}, mAgg = {}, pAgg = {}, hours = new Array(24).fill(0);
      var wh = []; for (var q = 0; q < 7; q++) wh.push(new Array(24).fill(0));
      var sessions = 0, first = null;
      for (var d = s.days; d >= 0; d--) {
        var day = dayAdd(today, -d);
        var wd = weekdayOf(day);
        var idle = rnd() < (wd >= 5 ? 0.55 : 0.16) * (2 - s.w);
        if (idle) continue;
        if (!first) first = day;
        var ramp = 0.45 + 0.55 * (1 - d / s.days);
        var msgs = Math.round((14 + rnd() * 70) * s.w * ramp);
        var cr = Math.round(msgs * (32000 + rnd() * 46000));
        var cw = Math.round(msgs * (900 + rnd() * 2600));
        var ip = Math.round(msgs * (18 + rnd() * 42));
        var op = Math.round(msgs * (240 + rnd() * 720));
        var cw1 = Math.round(cw * (0.16 + rnd() * 0.12));
        var cw5 = Math.round(cw * (0.62 + rnd() * 0.08));
        var th = Math.round(op * (0.24 + rnd() * 0.32));
        var ses = 1 + Math.round(rnd() * 3 * s.w);
        sessions += ses;
        daily[day] = { i: ip, o: op, cw: cw, cr: cr, cw1: cw1, cw5: cw5, th: th, m: msgs, s: ses };
        var mi = rnd(); var acc = 0, mm = models[0][0];
        for (var k = 0; k < models.length; k++) { acc += models[k][1]; if (mi <= acc) { mm = models[k][0]; break; } }
        dailyModels[day] = {}; dailyModels[day][mm] = ip + op + cw + cr;
        var b = mAgg[mm] || (mAgg[mm] = { i: 0, o: 0, cw: 0, cr: 0, cw1: 0, cw5: 0, th: 0, m: 0 });
        b.i += ip; b.o += op; b.cw += cw; b.cr += cr; b.cw1 += cw1; b.cw5 += cw5; b.th += th; b.m += msgs;
        var pn = projects[Math.floor(rnd() * projects.length)];
        var pb = pAgg[pn] || (pAgg[pn] = { i: 0, o: 0, cw: 0, cr: 0, cw1: 0, cw5: 0, th: 0, m: 0, last: day });
        pb.i += ip; pb.o += op; pb.cw += cw; pb.cr += cr; pb.cw1 += cw1; pb.cw5 += cw5; pb.th += th; pb.m += msgs; pb.last = day;
        for (var h = 0; h < msgs; h++) {
          var hh = s.hours[Math.floor(rnd() * s.hours.length)];
          hours[hh]++; wh[wd][hh]++;
        }
      }
      var tot = { i: 0, o: 0, cw: 0, cr: 0, cw1: 0, cw5: 0, th: 0, m: 0 };
      Object.keys(daily).forEach(function (d2) {
        ["i","o","cw","cr","cw1","cw5","th","m"].forEach(function (k2) { tot[k2] += daily[d2][k2]; });
      });
      tot.total = tot.i + tot.o + tot.cw + tot.cr;
      tot.sessions = sessions;
      tot.active_days = Object.keys(daily).length;
      return {
        schema: 2, sample: true,
        generated_at: new Date(Date.now() - Math.floor(rnd() * 5) * 3600000).toISOString(),
        tz: "Asia/Seoul",
        machine: { id: s.id, label: s.label, hostname: s.id, os: s.os },
        range: { first: first, last: today },
        totals: tot, daily: daily, daily_models: dailyModels,
        models: mAgg, projects: pAgg, hours: hours, weekday_hour: wh
      };
    });
  }

  // ------------------------------------------------------------ derive
  function activeMachines() {
    return Object.keys(state.machines)
      .filter(function (id) { return !state.off[id]; })
      .sort(function (a, b) {
        return (state.machines[b].totals.total || 0) - (state.machines[a].totals.total || 0);
      });
  }
  function allMachines() {
    return Object.keys(state.machines).sort(function (a, b) {
      return (state.machines[b].totals.total || 0) - (state.machines[a].totals.total || 0);
    });
  }
  var _colorCache = null;
  function colorOf(id) {
    if (!_colorCache) {
      _colorCache = {};
      allMachines().forEach(function (m, i) {
        _colorCache[m] = cvar(SERIES[i % SERIES.length]);
      });
    }
    return _colorCache[id] || cvar(SERIES[0]);
  }

  function derive() {
    var ids = activeMachines();
    var lastDay = null;
    ids.forEach(function (id) {
      var r = state.machines[id].range || {};
      if (r.last && (!lastDay || r.last > lastDay)) lastDay = r.last;
    });
    var today = todayKey();
    var endDay = lastDay && lastDay > today ? lastDay : today;
    var from = null;
    if (state.range !== "all") from = dayAdd(endDay, -(parseInt(state.range, 10) - 1));

    var daySet = {};
    ids.forEach(function (id) {
      Object.keys(state.machines[id].daily || {}).forEach(function (d) {
        if (!from || d >= from) daySet[d] = 1;
      });
    });
    var days = Object.keys(daySet).sort();

    // continuous axis for fixed windows
    var axis = days;
    if (from) {
      axis = [];
      var d = from;
      var guard = 4000;                    // 날짜 계산이 어긋나도 멈추지 않도록
      while (d <= endDay && guard-- > 0) {
        axis.push(d);
        var next = dayAdd(d, 1);
        if (next <= d) break;              // 전진하지 않으면 즉시 중단
        d = next;
      }
    }

    var rows = axis.map(function (day) {
      var per = {}, tot = 0, msgs = 0, ses = 0, cr = 0;
      ids.forEach(function (id) {
        var b = (state.machines[id].daily || {})[day];
        if (!b) return;
        var t = (b.i || 0) + (b.o || 0) + (b.cw || 0) + (b.cr || 0);
        if (t) per[id] = t;
        tot += t; msgs += b.m || 0; ses += b.s || 0; cr += b.cr || 0;
      });
      return { day: day, per: per, total: tot, msgs: msgs, sessions: ses, cr: cr };
    });

    var comp = { i: 0, o: 0, cw: 0, cr: 0, cw1: 0, cw5: 0, th: 0 }, msgs = 0, sessions = 0;
    var models = {}, projects = {}, hours = new Array(24).fill(0);
    var wh = []; for (var q = 0; q < 7; q++) wh.push(new Array(24).fill(0));
    var inWindow = !!from;

    ids.forEach(function (id) {
      var M = state.machines[id];
      Object.keys(M.daily || {}).forEach(function (day) {
        if (from && day < from) return;
        var b = M.daily[day];
        comp.i += b.i || 0; comp.o += b.o || 0; comp.cw += b.cw || 0; comp.cr += b.cr || 0;
        comp.cw1 += b.cw1 || 0; comp.cw5 += b.cw5 || 0; comp.th += b.th || 0;
        msgs += b.m || 0; sessions += b.s || 0;
        var dm = (M.daily_models || {})[day] || {};
        Object.keys(dm).forEach(function (mo) { models[mo] = (models[mo] || 0) + dm[mo]; });
      });
      // 프로젝트·시간대는 머신 전체 집계만 제공되므로 창 필터 시 근사 비율을 적용
      var scale = 1;
      if (inWindow) {
        var win = 0, all = M.totals.total || 0;
        Object.keys(M.daily || {}).forEach(function (day) {
          if (day < from) return;
          var b = M.daily[day];
          win += (b.i || 0) + (b.o || 0) + (b.cw || 0) + (b.cr || 0);
        });
        scale = all ? win / all : 0;
      }
      Object.keys(M.projects || {}).forEach(function (p) {
        var b = M.projects[p];
        var t = ((b.i || 0) + (b.o || 0) + (b.cw || 0) + (b.cr || 0)) * scale;
        var e = projects[p] || (projects[p] = { t: 0, m: 0, last: null });
        e.t += t; e.m += (b.m || 0) * scale;
        if (b.last && (!e.last || b.last > e.last)) e.last = b.last;
      });
      (M.hours || []).forEach(function (v, h) { hours[h] += v * scale; });
      (M.weekday_hour || []).forEach(function (row, w) {
        (row || []).forEach(function (v, h) { wh[w][h] += v * scale; });
      });
    });

    var total = comp.i + comp.o + comp.cw + comp.cr;
    var active = rows.filter(function (r) { return r.total > 0; });
    var streak = 0, best = 0, run = 0, prev = null;
    active.forEach(function (r) {
      run = (prev && dayAdd(prev, 1) === r.day) ? run + 1 : 1;
      if (run > best) best = run;
      prev = r.day;
    });
    if (prev && (relDays(prev) <= 1)) streak = run;

    return {
      ids: ids, rows: rows, active: active, total: total, comp: comp,
      msgs: msgs, sessions: sessions, models: models, projects: projects,
      hours: hours, wh: wh, streak: streak, best: best, approx: inWindow,
      first: active.length ? active[0].day : null,
      last: active.length ? active[active.length - 1].day : null
    };
  }

  // ------------------------------------------------------------ render
  var lastTiming = "";
  function render() {
    var t0 = (window.performance || Date).now();
    dropStyleCache();
    var D = derive();
    var t1 = (window.performance || Date).now();
    renderHeader(D);
    renderChips();
    renderStats(D);
    renderDaily(D);
    renderContext(D);
    renderComp(D);
    renderModels(D);
    renderMachines(D);
    renderProjects(D);
    renderHeat(D);
    renderBanner();
    document.getElementById("btnExport").hidden = state.sample;
    var t2 = (window.performance || Date).now();

    var days = 0, projects = {}, models = {};
    Object.keys(state.machines).forEach(function (id) {
      var M = state.machines[id];
      days += Object.keys(M.daily || {}).length;
      Object.keys(M.projects || {}).forEach(function (k) { projects[k] = 1; });
      Object.keys(M.models || {}).forEach(function (k) { models[k] = 1; });
    });
    lastTiming =
      "계산 " + (t1 - t0).toFixed(0) + "ms · 그리기 " + (t2 - t1).toFixed(0) + "ms · " +
      "머신 " + Object.keys(state.machines).length + " · 일자 " + days + " · " +
      "프로젝트 " + Object.keys(projects).length + " · 모델 " + Object.keys(models).length;
    var dg = document.getElementById("diag");
    if (dg) dg.textContent = lastTiming;
    if (window.console && window.console.log) console.log("[토큰미터] " + lastTiming);
  }

  function renderHeader(D) {
    document.getElementById("meterTotal").textContent = fmt(D.total);
    document.getElementById("meterUnit").textContent = "tokens · " + comma(Math.round(D.total));
    document.getElementById("meterRange").textContent =
      D.first ? (D.first + " → " + D.last) : "데이터 없음";
  }

  function renderBanner() {
    var b = document.getElementById("banner");
    b.textContent = "";
    if (state.scanning) {
      b.hidden = false;
      b.appendChild(el("b", null, "처음 스캔 중"));
      var n = Math.round(state.scanElapsed || 0);
      b.appendChild(document.createTextNode(
        " — 트랜스크립트 전체를 읽고 있습니다 (" + n + "초 경과). "
        + "끝나면 자동으로 화면이 채워집니다. 갱신은 백그라운드에서 돌기 때문에 "
        + "이후로는 화면이 멈추지 않습니다."));
      return;
    }
    if (state.sample) {
      b.hidden = false;
      b.appendChild(el("b", null, "예시 데이터"));
      b.appendChild(document.createTextNode(
        "입니다 — 실제 수치가 아닙니다. 아래에서 수집 JSON을 불러오면 예시는 사라집니다."));
      return;
    }
    var stale = allMachines().filter(function (id) {
      var r = relDays((state.machines[id].generated_at || "").slice(0, 10));
      return r != null && r >= 2;
    });
    if (stale.length) {
      b.hidden = false;
      b.appendChild(el("b", null, "오래된 스냅샷"));
      b.appendChild(document.createTextNode(
        " — " + stale.map(function (id) { return state.machines[id].machine.label; }).join(", ") +
        " 은(는) 2일 이상 갱신되지 않았습니다. 해당 머신에서 수집기를 다시 실행하세요."));
      return;
    }
    b.hidden = true;
  }

  function renderChips() {
    var box = document.getElementById("machineChips");
    box.textContent = "";
    var ids = allMachines();
    if (ids.length < 2) return;
    ids.forEach(function (id) {
      var on = !state.off[id];
      var c = el("button", "chip");
      c.type = "button";
      c.setAttribute("aria-pressed", on ? "true" : "false");
      var dot = el("span", "dot");
      dot.style.background = colorOf(id);
      c.appendChild(dot);
      c.appendChild(document.createTextNode(state.machines[id].machine.label));
      c.onclick = function () {
        if (on && activeMachines().length === 1) return;
        state.off[id] = on; render();
      };
      box.appendChild(c);
    });
  }

  function renderStats(D) {
    var box = document.getElementById("stats");
    box.textContent = "";
    var items = [
      ["활성 일수", String(D.active.length), "일"],
      ["연속", D.streak + " / " + D.best, "현재 / 최장"],
      ["세션", comma(D.sessions), ""],
      ["메시지", comma(D.msgs), ""],
      ["머신", String(D.ids.length) + " / " + allMachines().length, "활성 / 전체"],
      ["일 평균", fmt(D.active.length ? D.total / D.active.length : 0), "tokens"],
      ["턴당 컨텍스트", D.msgs ? fmt(D.comp.cr / D.msgs) : "—", "tokens"]
    ];
    items.forEach(function (it) {
      var s = el("div", "stat");
      s.appendChild(el("div", "lbl", it[0]));
      s.appendChild(el("span", "v", it[1]));
      if (it[2]) s.appendChild(el("span", "u", it[2]));
      box.appendChild(s);
    });
  }

  // ---- daily stacked bars
  var dailyGeom = null;
  function renderDaily(D) {
    var box = document.getElementById("dailyBox");
    var svg = document.getElementById("dailySvg");
    var W = Math.max(320, box.clientWidth || 900);
    _lastW = box.clientWidth || W;
    var H = 240;
    var padL = 46, padR = 10, padT = 12, padB = 26;
    var iw = W - padL - padR, ih = H - padT - padB;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("height", H);
    svg.textContent = "";

    var rows = D.rows;
    document.getElementById("dailyNote").textContent =
      rows.length ? rows.length + "일 · 머신별 누적" : "";

    if (!rows.length) { dailyGeom = null; return; }

    var max = 0;
    rows.forEach(function (r) { if (r.total > max) max = r.total; });
    if (max <= 0) max = 1;
    var step = Math.pow(10, Math.floor(Math.log10(max)));
    var top = Math.ceil(max / step) * step;
    if (top / max > 1.6) top = Math.ceil(max / (step / 2)) * (step / 2);

    var muted = cvar("--ink-mute"), rule = cvar("--rule-soft"), paper = cvar("--paper");

    // gridlines + y labels
    [0, 0.5, 1].forEach(function (f) {
      var y = padT + ih - ih * f;
      svg.appendChild(svgEl("line", {
        x1: padL, x2: W - padR, y1: y, y2: y, stroke: rule, "stroke-width": 1
      }));
      var t = svgEl("text", {
        x: padL - 8, y: y + 3.5, fill: muted, "text-anchor": "end",
        "font-family": "var(--mono)", "font-size": 10
      });
      t.textContent = f === 0 ? "0" : fmt(top * f);
      svg.appendChild(t);
    });

    var n = rows.length;
    var bw = iw / n;
    var barW = Math.max(1, Math.min(18, bw - (bw > 4 ? 1.5 : 0.4)));
    var ids = D.ids;

    rows.forEach(function (r, i) {
      if (!r.total) return;
      var x = padL + bw * i + (bw - barW) / 2;
      var y = padT + ih;
      ids.forEach(function (id) {
        var v = r.per[id];
        if (!v) return;
        var h = (v / top) * ih;
        if (h < 0.7) h = 0.7;
        y -= h;
        svg.appendChild(svgEl("rect", {
          x: x.toFixed(2), y: y.toFixed(2), width: barW.toFixed(2), height: h.toFixed(2),
          fill: colorOf(id), rx: Math.min(2, barW / 2)
        }));
        if (ids.length > 1 && barW >= 3) {
          svg.appendChild(svgEl("rect", {
            x: x.toFixed(2), y: (y - 1).toFixed(2), width: barW.toFixed(2), height: 2,
            fill: paper
          }));
        }
      });
    });

    // x labels
    var ticks = Math.min(7, n);
    var seen = {};
    for (var t2 = 0; t2 < ticks; t2++) {
      var idx = Math.round((n - 1) * (ticks === 1 ? 0 : t2 / (ticks - 1)));
      if (seen[idx]) continue;
      seen[idx] = 1;
      var lab = rows[idx].day.slice(5).replace("-", ".");
      var tx = padL + bw * idx + bw / 2;
      tx = Math.max(padL + 12, Math.min(W - padR - 12, tx));
      var te = svgEl("text", {
        x: tx, y: H - 8, fill: muted, "text-anchor": "middle",
        "font-family": "var(--mono)", "font-size": 10
      });
      te.textContent = lab;
      svg.appendChild(te);
    }

    // crosshair
    var cross = svgEl("line", {
      x1: 0, x2: 0, y1: padT, y2: padT + ih,
      stroke: cvar("--ink-mute"), "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0
    });
    svg.appendChild(cross);
    dailyGeom = { W: W, H: H, padL: padL, padT: padT, ih: ih, bw: bw, rows: rows, ids: ids, cross: cross, svg: svg };

    // legend
    var lg = document.getElementById("dailyLegend");
    lg.textContent = "";
    if (ids.length > 1) {
      ids.forEach(function (id) {
        var i2 = el("i");
        var b2 = el("b"); b2.style.background = colorOf(id);
        i2.appendChild(b2);
        i2.appendChild(document.createTextNode(state.machines[id].machine.label));
        lg.appendChild(i2);
      });
    }
  }

  function bindDailyHover() {
    var box = document.getElementById("dailyBox");
    var tip = document.getElementById("dailyTip");
    function hide() {
      tip.hidden = true;
      if (dailyGeom) dailyGeom.cross.setAttribute("opacity", 0);
    }
    box.addEventListener("mouseleave", hide);
    box.addEventListener("mousemove", function (ev) {
      if (!dailyGeom || !dailyGeom.rows.length) return;
      var g = dailyGeom;
      var rect = g.svg.getBoundingClientRect();
      var scale = rect.width / g.W;
      var px = (ev.clientX - rect.left) / scale;
      var i = Math.floor((px - g.padL) / g.bw);
      if (i < 0 || i >= g.rows.length) { hide(); return; }
      var r = g.rows[i];
      var cx = g.padL + g.bw * i + g.bw / 2;
      g.cross.setAttribute("x1", cx); g.cross.setAttribute("x2", cx);
      g.cross.setAttribute("opacity", r.total ? 0.5 : 0.25);

      tip.textContent = "";
      tip.appendChild(el("div", "t-day", r.day + " (" + WD[weekdayOf(r.day)] + ")"));
      tip.appendChild(el("div", "t-tot", fmt(r.total) + " tokens"));
      if (r.total) {
        var tb = el("table");
        g.ids.forEach(function (id) {
          var v = r.per[id]; if (!v) return;
          var tr = el("tr");
          var td1 = el("td");
          var sw = el("span", "sw"); sw.style.background = colorOf(id);
          td1.appendChild(sw);
          td1.appendChild(document.createTextNode(state.machines[id].machine.label));
          var td2 = el("td", null, fmt(v));
          tr.appendChild(td1); tr.appendChild(td2);
          tb.appendChild(tr);
        });
        var tr2 = el("tr");
        var c1 = el("td"); c1.style.color = "var(--ink-mute)";
        c1.textContent = "메시지 / 세션";
        var c2 = el("td", null, r.msgs + " / " + r.sessions);
        c2.style.color = "var(--ink-mute)";
        tr2.appendChild(c1); tr2.appendChild(c2);
        tb.appendChild(tr2);
        tip.appendChild(tb);
      } else {
        tip.appendChild(el("div", "t-day", "사용 없음"));
      }
      tip.hidden = false;
      var bw2 = box.clientWidth, tw = tip.offsetWidth;
      var left = cx * scale + 14;
      if (left + tw > bw2) left = cx * scale - tw - 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = "8px";
    });
  }

  // ---- context per turn line
  var contextGeom = null;
  function renderContext(D) {
    var box = document.getElementById("ctxBox");
    var svg = document.getElementById("ctxSvg");
    var W = Math.max(320, box.clientWidth || 900);
    _lastCtxW = box.clientWidth || W;
    var H = 240;
    var padL = 46, padR = 10, padT = 12, padB = 26;
    var iw = W - padL - padR, ih = H - padT - padB;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("height", H);
    svg.textContent = "";

    var rows = D.rows;
    if (!rows.length) { contextGeom = null; return; }

    var values = rows.map(function (r) { return r.msgs > 0 ? r.cr / r.msgs : null; });
    var max = 0;
    values.forEach(function (v) { if (v != null && v > max) max = v; });
    if (max <= 0) max = 1;
    var step = Math.pow(10, Math.floor(Math.log10(max)));
    var top = Math.ceil(max / step) * step;
    if (top / max > 1.6) top = Math.ceil(max / (step / 2)) * (step / 2);

    var muted = cvar("--ink-mute"), rule = cvar("--rule-soft"), accent = cvar("--accent");

    [0, 0.5, 1].forEach(function (f) {
      var y = padT + ih - ih * f;
      svg.appendChild(svgEl("line", {
        x1: padL, x2: W - padR, y1: y, y2: y, stroke: rule, "stroke-width": 1
      }));
      var t = svgEl("text", {
        x: padL - 8, y: y + 3.5, fill: muted, "text-anchor": "end",
        "font-family": "var(--mono)", "font-size": 10
      });
      t.textContent = f === 0 ? "0" : fmt(top * f);
      svg.appendChild(t);
    });

    var n = rows.length;
    var bw = iw / n;
    var path = "";
    values.forEach(function (v, i) {
      if (v == null) return;
      var x = padL + bw * i + bw / 2;
      var y = padT + ih - (v / top) * ih;
      path += (i === 0 || values[i - 1] == null ? "M" : "L") + x.toFixed(2) + " " + y.toFixed(2) + " ";
    });
    if (path) {
      svg.appendChild(svgEl("path", {
        d: path.trim(), fill: "none", stroke: accent, "stroke-width": 2,
        "stroke-linecap": "round", "stroke-linejoin": "round"
      }));
      values.forEach(function (v, i) {
        if (v == null) return;
        var x = padL + bw * i + bw / 2;
        var y = padT + ih - (v / top) * ih;
        svg.appendChild(svgEl("circle", {
          cx: x.toFixed(2), cy: y.toFixed(2), r: 2.5, fill: accent
        }));
      });
    }

    var ticks = Math.min(7, n);
    var seen = {};
    for (var t2 = 0; t2 < ticks; t2++) {
      var idx = Math.round((n - 1) * (ticks === 1 ? 0 : t2 / (ticks - 1)));
      if (seen[idx]) continue;
      seen[idx] = 1;
      var lab = rows[idx].day.slice(5).replace("-", ".");
      var tx = padL + bw * idx + bw / 2;
      tx = Math.max(padL + 12, Math.min(W - padR - 12, tx));
      var te = svgEl("text", {
        x: tx, y: H - 8, fill: muted, "text-anchor": "middle",
        "font-family": "var(--mono)", "font-size": 10
      });
      te.textContent = lab;
      svg.appendChild(te);
    }

    var cross = svgEl("line", {
      x1: 0, x2: 0, y1: padT, y2: padT + ih,
      stroke: muted, "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0
    });
    svg.appendChild(cross);
    contextGeom = {
      W: W, H: H, padL: padL, padT: padT, ih: ih, bw: bw,
      rows: rows, values: values, cross: cross, svg: svg
    };
  }

  function bindContextHover() {
    var box = document.getElementById("ctxBox");
    var tip = document.getElementById("ctxTip");
    function hide() {
      tip.hidden = true;
      if (contextGeom) contextGeom.cross.setAttribute("opacity", 0);
    }
    box.addEventListener("mouseleave", hide);
    box.addEventListener("mousemove", function (ev) {
      if (!contextGeom || !contextGeom.rows.length) return;
      var g = contextGeom;
      var rect = g.svg.getBoundingClientRect();
      var scale = rect.width / g.W;
      var px = (ev.clientX - rect.left) / scale;
      var i = Math.floor((px - g.padL) / g.bw);
      if (i < 0 || i >= g.rows.length) { hide(); return; }
      var r = g.rows[i], v = g.values[i];
      var cx = g.padL + g.bw * i + g.bw / 2;
      g.cross.setAttribute("x1", cx); g.cross.setAttribute("x2", cx);
      g.cross.setAttribute("opacity", v != null ? 0.5 : 0.25);

      tip.textContent = "";
      tip.appendChild(el("div", "t-day", r.day + " (" + WD[weekdayOf(r.day)] + ")"));
      if (v != null) {
        tip.appendChild(el("div", "t-tot", fmt(v) + " tokens"));
      } else {
        tip.appendChild(el("div", "t-day", "사용 없음"));
      }
      tip.hidden = false;
      var bw2 = box.clientWidth, tw = tip.offsetWidth;
      var left = cx * scale + 14;
      if (left + tw > bw2) left = cx * scale - tw - 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = "8px";
    });
  }

  // ---- composition
  function renderComp(D) {
    var bar = document.getElementById("comp");
    var tbl = document.getElementById("compTable");
    bar.textContent = ""; tbl.textContent = "";
    var total = D.total || 1;

    COMP.forEach(function (c) {
      var v = D.comp[c.k] || 0;
      var d = el("div");
      d.style.flexGrow = String(Math.max(v / total, 0.002));
      d.style.background = cvar(c.v);
      d.title = c.name + " " + fmt(v);
      bar.appendChild(d);
    });

    var head = el("tr");
    ["구분", "토큰", "비중"].forEach(function (h, i) {
      var th = el("th", i ? "r" : null, h);
      head.appendChild(th);
    });
    tbl.appendChild(head);
    COMP.forEach(function (c) {
      var v = D.comp[c.k] || 0;
      var tr = el("tr");
      var td = el("td");
      var w = el("div", "mname");
      var dot = el("span", "dot"); dot.style.background = cvar(c.v);
      w.appendChild(dot);
      w.appendChild(document.createTextNode(c.name));
      td.appendChild(w);
      tr.appendChild(td);
      tr.appendChild(el("td", "r", fmt(v)));
      tr.appendChild(el("td", "r", pct(v, total).toFixed(1) + "%"));
      tbl.appendChild(tr);
    });

    function subsetRow(name, value, parent) {
      var tr = el("tr");
      tr.appendChild(el("td", null, "↳ " + name));
      tr.appendChild(el("td", "r", fmt(value)));
      tr.appendChild(el("td", "r", pct(value, parent).toFixed(1) + "%"));
      tbl.appendChild(tr);
    }

    var hasCacheSplit = (D.comp.cw1 || 0) > 0 || (D.comp.cw5 || 0) > 0;
    if (hasCacheSplit) {
      subsetRow("캐시 쓰기 1h", D.comp.cw1 || 0, D.comp.cw || 0);
      subsetRow("캐시 쓰기 5m", D.comp.cw5 || 0, D.comp.cw || 0);
      var remainder = (D.comp.cw || 0) - (D.comp.cw1 || 0) - (D.comp.cw5 || 0);
      var remainderThreshold = Math.max(1000, (D.comp.cw || 0) * 0.001);
      if (remainder >= remainderThreshold) subsetRow("캐시 쓰기 미분류", remainder, D.comp.cw || 0);
    }
    if ((D.comp.th || 0) > 0) {
      subsetRow("출력 중 thinking", D.comp.th, D.comp.o || 0);
    }

    var cacheShare = pct((D.comp.cr || 0) + (D.comp.cw || 0), total);
    var note =
      "프롬프트 캐시가 전체의 " + cacheShare.toFixed(1) +
      "%. 캐시 읽기는 새로 생성되는 토큰이 아니라 이전 컨텍스트를 다시 읽어들인 양입니다.";
    if ((D.comp.th || 0) > 0) {
      note += " 출력 중 " + pct(D.comp.th, D.comp.o || 0).toFixed(1) + "%가 thinking입니다.";
    }
    note += " 로컬 기록의 사용량 구성 참고치이며, 청구액이나 구독 한도를 뜻하지 않습니다.";
    document.getElementById("compNote").textContent = note;
  }

  // ---- models
  function renderModels(D) {
    var tbl = document.getElementById("modelTable");
    tbl.textContent = "";
    var arr = Object.keys(D.models).map(function (k) { return [k, D.models[k]]; })
      .sort(function (a, b) { return b[1] - a[1]; });
    document.getElementById("modelNote").textContent = arr.length + "종";
    var head = el("tr");
    ["모델", "", "토큰", "비중"].forEach(function (h, i) {
      head.appendChild(el("th", i >= 2 ? "r" : null, h));
    });
    tbl.appendChild(head);
    if (!arr.length) {
      var tr0 = el("tr");
      var td0 = el("td", "mut", "데이터 없음");
      td0.colSpan = 4; tr0.appendChild(td0); tbl.appendChild(tr0);
      return;
    }
    var max = arr[0][1] || 1;
    arr.slice(0, 8).forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", null, shortModel(p[0])));
      var tdb = el("td");
      tdb.style.width = "40%";
      var mini = el("div", "mini");
      var fill = el("i");
      fill.style.width = Math.max(2, pct(p[1], max)) + "%";
      mini.appendChild(fill);
      tdb.appendChild(mini);
      tr.appendChild(tdb);
      tr.appendChild(el("td", "r", fmt(p[1])));
      tr.appendChild(el("td", "r", pct(p[1], D.total || 1).toFixed(1) + "%"));
      tbl.appendChild(tr);
    });
  }

  // ---- machines
  function sparkline(id, from) {
    var M = state.machines[id];
    var daily = M.daily || {};
    var days = Object.keys(daily).sort();
    if (from) days = days.filter(function (d) { return d >= from; });
    days = days.slice(-60);
    var svg = svgEl("svg", { class: "spark", viewBox: "0 0 108 24", "aria-hidden": "true" });
    if (!days.length) return svg;
    var vals = days.map(function (d) {
      var b = daily[d];
      return (b.i || 0) + (b.o || 0) + (b.cw || 0) + (b.cr || 0);
    });
    var max = Math.max.apply(null, vals) || 1;
    var w = 108 / days.length;
    var col = colorOf(id);
    vals.forEach(function (v, i) {
      var h = Math.max(1, (v / max) * 20);
      svg.appendChild(svgEl("rect", {
        x: (i * w).toFixed(2), y: (23 - h).toFixed(2),
        width: Math.max(0.8, w - 0.6).toFixed(2), height: h.toFixed(2),
        fill: col, opacity: 0.55 + 0.45 * (v / max)
      }));
    });
    return svg;
  }

  function renderMachines(D) {
    var tbl = document.getElementById("machineTable");
    tbl.textContent = "";
    var ids = allMachines();
    document.getElementById("machineNote").textContent = ids.length + "대 연결됨";
    var head = el("tr");
    ["머신", "최근 추이", "토큰", "비중", "세션", "활성일", "마지막 수집"].forEach(function (h, i) {
      head.appendChild(el("th", i >= 2 && i <= 5 ? "r" : null, h));
    });
    tbl.appendChild(head);

    var from = null;
    if (state.range !== "all" && D.last) from = dayAdd(D.last, -(parseInt(state.range, 10) - 1));

    ids.forEach(function (id) {
      var M = state.machines[id];
      var t = 0, ses = 0, act = 0;
      Object.keys(M.daily || {}).forEach(function (d) {
        if (from && d < from) return;
        var b = M.daily[d];
        t += (b.i || 0) + (b.o || 0) + (b.cw || 0) + (b.cr || 0);
        ses += b.s || 0; act++;
      });
      var tr = el("tr");
      if (state.off[id]) tr.style.opacity = ".42";

      var td1 = el("td");
      var nm = el("div", "mname");
      var dot = el("span", "dot"); dot.style.background = colorOf(id);
      nm.appendChild(dot);
      var box = el("div");
      box.appendChild(document.createTextNode(M.machine.label));
      box.appendChild(el("small", null, M.machine.os || M.machine.hostname || ""));
      nm.appendChild(box);
      td1.appendChild(nm);
      tr.appendChild(td1);

      var td2 = el("td");
      td2.appendChild(sparkline(id, from));
      tr.appendChild(td2);

      tr.appendChild(el("td", "r", fmt(t)));
      tr.appendChild(el("td", "r", pct(t, D.total || 1).toFixed(1) + "%"));
      tr.appendChild(el("td", "r", comma(ses)));
      tr.appendChild(el("td", "r", String(act)));

      var rel = relDays((M.generated_at || "").slice(0, 10));
      var when = rel == null ? "—" : rel === 0 ? "오늘" : rel === 1 ? "어제" : rel + "일 전";
      var td7 = el("td", "mut" + (rel != null && rel >= 2 ? " stale" : ""), when);
      tr.appendChild(td7);
      tbl.appendChild(tr);
    });
  }

  // ---- projects
  function renderProjects(D) {
    var tbl = document.getElementById("projectTable");
    tbl.textContent = "";
    var arr = Object.keys(D.projects).map(function (k) { return [k, D.projects[k]]; })
      .sort(function (a, b) { return b[1].t - a[1].t; }).slice(0, 8);
    var head = el("tr");
    ["프로젝트", "", "토큰", "최근"].forEach(function (h, i) {
      head.appendChild(el("th", i === 2 ? "r" : null, h));
    });
    tbl.appendChild(head);
    if (!arr.length) {
      var tr0 = el("tr"); var td0 = el("td", "mut", "데이터 없음");
      td0.colSpan = 4; tr0.appendChild(td0); tbl.appendChild(tr0); return;
    }
    var max = arr[0][1].t || 1;
    arr.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", null, p[0]));
      var tdb = el("td"); tdb.style.width = "34%";
      var mini = el("div", "mini");
      var fill = el("i");
      fill.style.width = Math.max(2, pct(p[1].t, max)) + "%";
      mini.appendChild(fill); tdb.appendChild(mini);
      tr.appendChild(tdb);
      tr.appendChild(el("td", "r", fmt(p[1].t)));
      tr.appendChild(el("td", "mut", p[1].last || "—"));
      tbl.appendChild(tr);
    });
  }

  // ---- heatmap
  function renderHeat(D) {
    var grid = document.getElementById("heat");
    var xs = document.getElementById("heatX");
    grid.textContent = ""; xs.textContent = "";
    var max = 0;
    D.wh.forEach(function (row) { row.forEach(function (v) { if (v > max) max = v; }); });
    var steps = ["--heat-0","--heat-1","--heat-2","--heat-3","--heat-4","--heat-5","--heat-6"];
    for (var w = 0; w < 7; w++) {
      grid.appendChild(el("div", "hl", WD[w]));
      for (var h = 0; h < 24; h++) {
        var v = D.wh[w][h];
        var lvl = !max || !v ? 0 : Math.max(1, Math.min(6, Math.ceil(v / max * 6)));
        var c = el("div", "cell");
        c.style.background = cvar(steps[lvl]);
        c.title = WD[w] + "요일 " + h + "시 · 메시지 " + Math.round(v);
        grid.appendChild(c);
      }
    }
    xs.appendChild(el("span"));
    for (var h2 = 0; h2 < 24; h2++) xs.appendChild(el("span", null, h2 % 3 === 0 ? String(h2) : ""));
    var peak = 0, ph = 0;
    D.hours.forEach(function (v, i) { if (v > peak) { peak = v; ph = i; } });
    document.getElementById("heatNote").textContent = peak ? "피크 " + ph + "시" : "";
  }

  // ------------------------------------------------------------ ingest
  function normalizeBucket(b) {
    if (!b) return;
    ["cw1", "cw5", "th"].forEach(function (k) {
      if (typeof b[k] !== "number" || !isFinite(b[k])) b[k] = 0;
    });
  }

  function normalizePayload(payload) {
    normalizeBucket(payload.totals);
    ["daily", "models", "projects"].forEach(function (group) {
      Object.keys(payload[group] || {}).forEach(function (key) {
        normalizeBucket(payload[group][key]);
      });
    });
  }

  function ingest(payload, quiet) {
    if (!payload || (payload.schema !== 1 && payload.schema !== 2) || !payload.machine || !payload.machine.id) {
      if (!quiet) alert("수집기 형식(schema 1 또는 2)의 JSON이 아닙니다.");
      return false;
    }
    normalizePayload(payload);
    if (state.sample) { state.machines = {}; state.off = {}; state.sample = false; }
    var id = payload.machine.id;
    var prev = state.machines[id];
    if (prev && !prev.sample && (prev.generated_at || "") > (payload.generated_at || "")) return true;
    state.machines[id] = payload;
    if (!quiet) state.dirty[id] = true;
    return true;
  }

  function readFiles(files) {
    var list = Array.prototype.slice.call(files);
    var ok = 0, done = 0;
    if (!list.length) return;
    list.forEach(function (f) {
      var fr = new FileReader();
      fr.onload = function () {
        try {
          var d = JSON.parse(fr.result);
          if (d && d.kind === "merged" && Array.isArray(d.machines)) {
            d.machines.forEach(function (m) { if (ingest(m)) ok++; });
          } else if (ingest(d)) ok++;
        } catch (e) {
          console.warn("parse fail", f.name, e);
        }
        if (++done === list.length) {
          render();
          document.getElementById("btnSync").hidden = !(state.db && Object.keys(state.dirty).length);
        }
      };
      fr.readAsText(f);
    });
  }

  // ------------------------------------------------------------ wiring
  document.querySelectorAll(".seg button").forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll(".seg button").forEach(function (o) {
        o.setAttribute("aria-pressed", o === b ? "true" : "false");
      });
      state.range = b.dataset.range;
      render();
    };
  });

  var fi = document.getElementById("fileInput");
  fi.onchange = function () { readFiles(fi.files); fi.value = ""; };
  document.getElementById("btnAdd").onclick = function () { fi.click(); };
  var drop = document.getElementById("drop");
  drop.onclick = function () { fi.click(); };
  drop.onkeydown = function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); } };
  ["dragenter", "dragover"].forEach(function (t) {
    drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files) readFiles(e.dataTransfer.files);
  });
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });

  document.getElementById("btnExport").onclick = function () {
    var out = { schema: 2, kind: "merged", generated_at: new Date().toISOString(),
                machines: Object.keys(state.machines).map(function (k) { return state.machines[k]; }) };
    var txt = JSON.stringify(out);
    (async function () {
      var dl = window.claude && await window.claude.use("downloads");
      if (dl) {
        try { await dl.save({ filename: "claude-usage-merged.json", data: txt }); return; }
        catch (e) { console.warn(e); }
      }
      var url = URL.createObjectURL(new Blob([txt], { type: "application/json" }));
      var a = document.createElement("a");
      a.href = url; a.download = "claude-usage-merged.json"; a.click();
      setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
    })();
  };

  var _lastW = 0;
  var ro = window.ResizeObserver ? new ResizeObserver(function () {
    var w = document.getElementById("dailyBox").clientWidth;
    if (Math.abs(w - _lastW) < 2) return;   // 자기 렌더가 자기를 다시 부르는 것 방지
    _lastW = w;
    renderDaily(derive());
  }) : null;
  if (ro) ro.observe(document.getElementById("dailyBox"));

  var _lastCtxW = 0;
  var ctxRo = window.ResizeObserver ? new ResizeObserver(function () {
    var w = document.getElementById("ctxBox").clientWidth;
    if (Math.abs(w - _lastCtxW) < 2) return;
    _lastCtxW = w;
    renderContext(derive());
  }) : null;
  if (ctxRo) ctxRo.observe(document.getElementById("ctxBox"));

  // 테마가 바뀌면 캐시된 색을 버리고 다시 그린다
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onTheme = function () { render(); };
    if (mq.addEventListener) mq.addEventListener("change", onTheme);
    else if (mq.addListener) mq.addListener(onTheme);
  }

  bindDailyHover();
  bindContextHover();

  // initial: 로컬 서버가 심어준 데이터가 있으면 그것, 없으면 예시
  var boot = window.__BOOTSTRAP__;
  var scanInfo = window.__SCAN__ || {};
  if (boot && boot.length) {
    boot.forEach(function (p) { ingest(p, true); });
  } else if (window.__LOCAL__ && scanInfo.scanning) {
    // 첫 스캔 중 — 예시 데이터를 보여주면 진짜 수치로 오해하게 된다
    state.sample = false;
    state.scanning = true;
    state.scanElapsed = scanInfo.elapsed || 0;
  } else {
    makeSample().forEach(function (p) { state.machines[p.machine.id] = p; });
  }
  render();

  // 로컬 서버 모드: 주기적으로 다시 읽어 화면을 최신으로 유지
  if (window.__LOCAL__) {
    document.getElementById("loadSection").hidden = true;
    var timer = null;
    var poll = function () {
      fetch("api/data", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          // 배열(구버전)과 객체(현재) 둘 다 받는다
          var list = Array.isArray(res) ? res : (res.machines || []);
          var wasScanning = state.scanning;
          state.scanning = Array.isArray(res) ? false : !!res.scanning;
          state.scanElapsed = Array.isArray(res) ? 0 : (res.elapsed || 0);
          var changed = wasScanning !== state.scanning;
          list.forEach(function (p) {
            var prev = state.machines[p.machine && p.machine.id];
            if (!prev || prev.generated_at !== p.generated_at) {
              if (ingest(p, true)) changed = true;
            }
          });
          if (changed) render();
          schedule();
        })
        .catch(function () { schedule(); });  // 서버가 내려가도 화면은 그대로 둔다
    };
    var schedule = function () {
      if (timer) clearTimeout(timer);
      // 스캔 중에는 자주, 평소에는 느긋하게
      timer = setTimeout(poll, state.scanning ? 2000 : 60000);
    };
    schedule();
    window.addEventListener("focus", poll);
    // 스캔 중 경과 시간 표시
    setInterval(function () {
      if (state.scanning) {
        state.scanElapsed = (state.scanElapsed || 0) + 1;
        renderBanner();
      }
    }, 1000);
  }

  // ------------------------------------------------------------ db sync
  (async function () {
    if (!window.claude || !window.claude.use) return;
    var db;
    try { db = await window.claude.use("db"); } catch (e) { db = null; }
    if (!db) return;
    state.db = db;

    var sync = document.getElementById("btnSync");
    sync.hidden = !Object.keys(state.dirty).length;
    sync.onclick = async function () {
      sync.disabled = true;
      var ids = Object.keys(state.dirty);
      var okc = 0;
      for (var i = 0; i < ids.length; i++) {
        try { await db.doc("machines/" + ids[i]).set(state.machines[ids[i]]); okc++; delete state.dirty[ids[i]]; }
        catch (e) { console.warn("sync fail", ids[i], e); }
      }
      sync.disabled = false;
      sync.textContent = okc ? "저장됨 · " + okc + "대" : "저장 실패";
      setTimeout(function () {
        sync.textContent = "이 대시보드에 저장";
        sync.hidden = !Object.keys(state.dirty).length;
      }, 2200);
    };

    db.collection("machines").onSnapshot(function (snap) {
      var got = 0;
      snap.docs.forEach(function (d) {
        var v = d.data();
        if (v && ingest(v, true)) got++;
      });
      if (got) {
        document.getElementById("loadSection").querySelector(".hint").textContent =
          "이 대시보드에 저장된 머신 " + got + "대를 불러왔습니다. 다른 PC에서 이 링크를 열면 같은 화면이 보입니다.";
        render();
      }
    }, function (err) { console.warn("db snapshot", err); });
  })();
})();
</script>
"""

SHELL = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>:root{color-scheme:light}body{margin:0}img{max-width:100%%}[hidden]{display:none!important}</style>
<script>window.__LOCAL__=true;window.__BOOTSTRAP__=%s;window.__SCAN__=%s;</script>
</head><body>
%s
</body></html>
"""


def store_dir():
    d = STORE / "machines"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(payload):
    """다른 머신에서 받은 스냅샷을 로컬에 보관한다."""
    mid = payload.get("machine", {}).get("id")
    if not mid or payload.get("schema") not in (1, 2):
        return False
    normalize_payload_usage(payload)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(mid))[:64]
    p = store_dir() / (safe + ".json")
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(p)
    return True


def load_remote(local_id=None):
    out = []
    for p in sorted(store_dir().glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                d = _json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("schema") in (1, 2) and d.get("machine", {}).get("id") != local_id:
            out.append(normalize_payload_usage(d))
    return out




# ------------------------------------------------- 증분 스캔 (파일별 캐시)
#
# 세션 트랜스크립트는 append-only 다. 한 번 읽은 부분을 다시 읽을 이유가 없다.
# 파일마다 (크기, 수정시각, 읽은 위치, 뽑아낸 행)을 캐시해 두고,
# 다음 스캔에서는 늘어난 꼬리만 파싱한다.
#
# 행 형식: [dedup, "YYYY-MM-DD", hour, weekday, model_idx, session_idx,
#             i, o, cw, cr, cw1, cw5, th]
# 모델·세션 이름은 파일별 표에 두고 인덱스만 저장한다 (UUID 반복 제거).

CACHE_DIR = STORE / "cache"
CACHE_VERSION = 2


def _cache_path(rel):
    """rel 은 프로젝트 폴더 이름. 폴더당 캐시 파일 하나."""
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / (h + ".json")


def _load_dir_cache(dirname):
    try:
        with _cache_path(dirname).open("r", encoding="utf-8") as f:
            d = _json.load(f)
        if d.get("v") == CACHE_VERSION and d.get("dir") == dirname:
            return d
    except (OSError, ValueError):
        pass
    return {"v": CACHE_VERSION, "dir": dirname, "files": {}}


def _save_dir_cache(dirname, data):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp = _cache_path(dirname)
        tmp = cp.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            _json.dump(data, f, separators=(",", ":"))
        tmp.replace(cp)
    except OSError:
        pass


def _read_rows(path, start_off, models, sessions):
    """start_off 부터 완결된 줄만 파싱한다. (rows, 새 offset) 반환."""
    rows = []
    m_index = {n: i for i, n in enumerate(models)}
    s_index = {n: i for i, n in enumerate(sessions)}
    off = start_off
    try:
        fh = open(str(path), "rb")
    except OSError:
        return rows, start_off
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
                break            # 아직 쓰는 중인 마지막 줄 ― 다음에 다시 읽는다
            off += len(raw)
            line = raw.strip()
            if not line or line[:1] != b"{":
                continue
            try:
                rec = _json.loads(line.decode("utf-8", "replace"))
            except ValueError:
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
            dedup = msg.get("id") or rec.get("requestId") or rec.get("uuid")
            if dedup and rec.get("requestId"):
                dedup = "%s:%s" % (dedup, rec["requestId"])

            model = str(model)
            if model not in m_index:
                m_index[model] = len(models)
                models.append(model)
            session = rec.get("sessionId") or path.stem
            if session not in s_index:
                s_index[session] = len(sessions)
                sessions.append(session)

            local = dt.astimezone()
            rows.append([
                dedup, local.strftime("%Y-%m-%d"), local.hour, local.weekday(),
                m_index[model], s_index[session],
            ] + usage_values(usage))
    return rows, off


def _dir_entries(root, dirname, paths, stats):
    """프로젝트 폴더 하나를 처리한다. 캐시를 살려 쓰고, 바뀐 파일만 다시 읽는다."""
    cache = _load_dir_cache(dirname)
    files = cache.get("files", {})
    dirty = False
    live_names = set()
    out = []

    for path in paths:
        rel = path.name
        live_names.add(rel)
        try:
            st = path.stat()
        except OSError:
            continue
        size, mtime = st.st_size, int(st.st_mtime)
        entry = files.get(rel)

        if entry and entry.get("sz") == size and entry.get("mt") == mtime:
            stats["reused"] += 1
            out.append(entry)
            continue

        if entry and size >= entry.get("off", 0) and entry.get("off", 0) > 0:
            prev_off = entry["off"]
            models = entry.get("ms", [])
            sessions = entry.get("ss", [])
            new_rows, off = _read_rows(path, prev_off, models, sessions)
            entry["r"] = entry.get("r", []) + new_rows
            entry["ms"], entry["ss"], entry["off"] = models, sessions, off
            entry["sz"], entry["mt"] = size, mtime
            stats["tail"] += 1
            stats["bytes"] += max(0, size - prev_off)
        else:
            models, sessions = [], []
            rows, off = _read_rows(path, 0, models, sessions)
            entry = {"sz": size, "mt": mtime, "off": off,
                     "ms": models, "ss": sessions, "r": rows}
            stats["full"] += 1
            stats["bytes"] += size

        files[rel] = entry
        dirty = True
        out.append(entry)

    # 사라진 세션 파일의 캐시 제거
    for gone in [k for k in files if k not in live_names]:
        del files[gone]
        dirty = True

    if dirty:
        cache["files"] = files
        _save_dir_cache(dirname, cache)
        stats["dirs_written"] += 1
    return out


PAYLOAD_CACHE = STORE / "payload.json"


def _load_payload_cache(fp, args):
    """디렉터리 지문이 그대로면 지난번 결과를 그대로 쓴다 (재시작 대비)."""
    if args.since or args.until or args.pricing:
        return None
    try:
        with PAYLOAD_CACHE.open("r", encoding="utf-8") as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("fp") != list(fp) or d.get("schema_v") != SCHEMA_VERSION:
        return None
    pl = d.get("payload")
    if not pl or pl.get("machine", {}).get("label") != machine_identity(args.machine)["label"]:
        return None
    return pl


def _save_payload_cache(fp, payload, args):
    if args.since or args.until or args.pricing:
        return
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        tmp = PAYLOAD_CACHE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            _json.dump({"fp": list(fp), "schema_v": SCHEMA_VERSION, "payload": payload},
                       f, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(PAYLOAD_CACHE)
    except OSError:
        pass


def build_payload_incremental(args):
    root = Path(args.claude_dir).expanduser() / "projects"
    if not root.is_dir():
        raise SystemExit(
            "트랜스크립트 디렉터리를 찾을 수 없습니다: %s\n"
            "--claude-dir 로 Claude Code 홈(기본 ~/.claude)을 지정하세요." % root
        )

    fp = _fingerprint(root)
    hit = _load_payload_cache(fp, args)
    if hit is not None:
        hit["source"] = dict(hit.get("source", {}), cache_hit=True)
        return hit

    daily = {}
    models_agg = {}
    projects_agg = {}
    daily_models = {}
    hours = [0] * 24
    weekday_hour = [[0] * 24 for _ in range(7)]
    daily_sessions = {}
    project_last = {}
    all_sessions = set()
    seen = set()
    dups = 0
    kept = 0
    stats = {"reused": 0, "tail": 0, "full": 0, "bytes": 0, "dirs_written": 0}

    by_dir = {}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            dirname = path.relative_to(root).parts[0]
        except (ValueError, IndexError):
            dirname = "unknown"
        by_dir.setdefault(dirname, []).append(path)

    live = set()
    entries = []
    for dirname in sorted(by_dir):
        live.add(_cache_path(dirname).name)
        pj = decode_project(dirname)
        for e in _dir_entries(root, dirname, by_dir[dirname], stats):
            entries.append((pj, e))

    for pj, entry in entries:
        ms = entry.get("ms", [])
        ss = entry.get("ss", [])
        for r in entry.get("r", []):
            key = r[0]
            if key:
                if key in seen:
                    dups += 1
                    continue
                seen.add(key)
            day = r[1]
            if args.since and day < args.since:
                continue
            if args.until and day > args.until:
                continue
            u = usage_from_values(r[6:])
            model = ms[r[4]] if r[4] < len(ms) else "unknown"
            session = ss[r[5]] if r[5] < len(ss) else "unknown"

            if day not in daily:
                daily[day] = new_bucket()
                daily_sessions[day] = set()
            add_usage(daily[day], u)
            if model not in models_agg:
                models_agg[model] = new_bucket()
            add_usage(models_agg[model], u)
            if pj not in projects_agg:
                projects_agg[pj] = new_bucket()
            add_usage(projects_agg[pj], u)

            tot = r[6] + r[7] + r[8] + r[9]
            daily_models.setdefault(day, {})
            daily_models[day][model] = daily_models[day].get(model, 0) + tot
            hours[r[2]] += 1
            weekday_hour[r[3]][r[2]] += 1
            daily_sessions[day].add(session)
            all_sessions.add(session)
            if not project_last.get(pj) or day > project_last[pj]:
                project_last[pj] = day
            kept += 1

    # 사라진 트랜스크립트의 캐시 파일을 정리한다
    try:
        for f in CACHE_DIR.glob("*.json"):
            if f.name not in live:
                f.unlink()
    except OSError:
        pass

    for day, sess in daily_sessions.items():
        daily[day]["s"] = len(sess)
    for name, b in projects_agg.items():
        b["last"] = project_last.get(name)

    days_sorted = sorted(daily.keys())
    totals = new_bucket()
    for b in daily.values():
        for k in BUCKET_KEYS:
            totals[k] += b.get(k, 0)
    totals["total"] = bucket_total(totals)
    totals["sessions"] = len(all_sessions)
    totals["active_days"] = len(days_sorted)

    cur_streak, max_streak = streaks(days_sorted)
    peak_hour = max(range(24), key=lambda h: hours[h]) if any(hours) else None
    top_model = (max(models_agg.items(), key=lambda kv: bucket_total(kv[1]))[0]
                 if models_agg else None)

    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tz": local_tz(),
        "machine": machine_identity(args.machine),
        "source": {"kind": "claude-code-jsonl", "root": str(root),
                   "records": kept, "duplicates_skipped": dups,
                   "files_reused": stats["reused"], "files_tail": stats["tail"],
                   "files_full": stats["full"], "bytes_read": stats["bytes"],
                   "cache_files_written": stats["dirs_written"],
                   "days": len(daily), "projects": len(projects_agg),
                   "models": len(models_agg)},
        "range": {"first": days_sorted[0] if days_sorted else None,
                  "last": days_sorted[-1] if days_sorted else None},
        "totals": totals,
        "streak": {"current": cur_streak, "max": max_streak},
        "peak_hour": peak_hour,
        "top_model": top_model,
        "daily": daily,
        "daily_models": daily_models,
        "models": models_agg,
        "projects": projects_agg,
        "hours": hours,
        "weekday_hour": weekday_hour,
    }
    cost = estimate_cost(models_agg, load_pricing(args.pricing))
    if cost:
        payload["cost_estimate"] = cost
    _save_payload_cache(fp, payload, args)
    return payload


def scan_local(args):
    """이 PC를 지금 다시 스캔한다 (증분)."""
    ns = argparse.Namespace(
        claude_dir=args.claude_dir, machine=args.machine,
        since=args.since, until=args.until, pricing=args.pricing, verbose=False,
    )
    return build_payload_incremental(ns)


# 로컬 스캔은 트랜스크립트 전체를 다시 읽는 작업이라, 기록이 쌓이면 수십 초가
# 걸린다. 매 요청마다 하면 화면이 멈춘 것처럼 보이므로 결과를 캐시하고,
# 파일이 바뀌었을 때만 백그라운드로 다시 훑는다.
_scan = {"payload": None, "fp": None, "at": 0.0, "busy": False,
         "secs": None, "started": 0.0}
_scan_lock = threading.Lock()
RESCAN_MIN_INTERVAL = 30.0   # 초 (하한)


def _rescan_interval():
    """지난 스캔이 오래 걸렸으면 그만큼 더 뜸하게. CPU를 계속 물지 않게 한다."""
    with _scan_lock:
        last = _scan["secs"] or 0
    return max(RESCAN_MIN_INTERVAL, min(600.0, last * 10))


def _fingerprint(root):
    """파일 개수·총 크기·최신 mtime. 내용을 읽지 않아 싸다."""
    n = 0
    total = 0
    newest = 0.0
    try:
        for p in root.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            n += 1
            total += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    except OSError:
        pass
    return (n, total, int(newest))


def _do_scan(args):
    t0 = time.time()
    payload = None
    try:
        payload = scan_local(args)
    except SystemExit as e:
        print(f"  ! 로컬 스캔 실패: {e}", file=sys.stderr)
    except Exception as e:                     # 스캔이 죽어도 서버는 살아 있어야 한다
        print(f"  ! 스캔 중 오류: {e}", file=sys.stderr)
    finally:
        took = time.time() - t0
        root = Path(args.claude_dir).expanduser() / "projects"
        try:
            fp = _fingerprint(root)
        except Exception:
            fp = None
        with _scan_lock:
            if payload is not None:
                payload["scan_seconds"] = round(took, 2)
                _scan["payload"] = payload
            _scan["fp"] = fp
            _scan["at"] = time.time()
            _scan["secs"] = round(took, 2)
            _scan["busy"] = False              # 반드시 풀린다
        if payload is not None:
            print(f"  스캔 완료: {took:.1f}초", file=sys.stderr)


def _start_scan(args):
    """스캔은 언제나 한 번에 하나만. 이미 돌고 있으면 아무것도 하지 않는다."""
    with _scan_lock:
        if _scan["busy"]:
            return False
        _scan["busy"] = True
        _scan["started"] = time.time()
    threading.Thread(target=_do_scan, args=(args,), daemon=True).start()
    return True


def collect_all(args):
    """절대 스캔을 기다리지 않는다. 지금 가진 것을 즉시 돌려준다."""
    root = Path(args.claude_dir).expanduser() / "projects"
    with _scan_lock:
        cached = _scan["payload"]
        busy = _scan["busy"]
        last_at = _scan["at"]
        last_fp = _scan["fp"]
        started = _scan["started"]
        secs = _scan["secs"]

    if cached is None:
        _start_scan(args)
    elif not busy and (time.time() - last_at) >= _rescan_interval():
        try:
            if _fingerprint(root) != last_fp:
                _start_scan(args)
        except Exception:
            pass

    with _scan_lock:
        busy = _scan["busy"]
        cached = _scan["payload"]
        started = _scan["started"]

    lid = cached["machine"]["id"] if cached else None
    data = load_remote(lid)
    if cached:
        data.insert(0, cached)
    return {
        "machines": data,
        "scanning": bool(busy),
        "elapsed": round(time.time() - started, 1) if busy and started else None,
        "scan_seconds": secs,
    }


def pull_watch(paths):
    """공유 폴더에 있는 다른 머신의 JSON을 로컬 보관소로 끌어온다."""
    n = 0
    for raw in paths:
        d = _Path(raw).expanduser()
        files = sorted(d.glob("*.json")) if d.is_dir() else ([d] if d.is_file() else [])
        for f in files:
            try:
                with f.open("r", encoding="utf-8") as fh:
                    if save_snapshot(_json.load(fh)):
                        n += 1
            except (OSError, ValueError):
                continue
    return n


def make_handler(args):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if args.watch:
                pull_watch(args.watch)
            if path == "/":
                res = collect_all(args)
                boot = _json.dumps(res["machines"], ensure_ascii=False, separators=(",", ":"))
                scan = _json.dumps({"scanning": res["scanning"], "elapsed": res["elapsed"],
                                    "seconds": res["scan_seconds"]})
                self._send(200, SHELL % (boot, scan, PAGE), "text/html; charset=utf-8")
            elif path == "/api/data":
                self._send(200, _json.dumps(collect_all(args), ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path == "/api/ping":
                self._send(200, '{"ok":true}', "application/json")
            elif path == "/favicon.ico":
                self._send(200,
                           '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                           '<rect width="16" height="16" rx="3" fill="#2a78d6"/>'
                           '<rect x="3" y="9" width="2" height="4" fill="#fff"/>'
                           '<rect x="7" y="5" width="2" height="8" fill="#fff"/>'
                           '<rect x="11" y="7" width="2" height="6" fill="#fff"/></svg>',
                           "image/svg+xml")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/api/ingest":
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 * 1024 * 1024:
                self._send(413, '{"ok":false,"error":"too large"}', "application/json")
                return
            try:
                payload = _json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                self._send(400, '{"ok":false,"error":"bad json"}', "application/json")
                return
            if args.token and self.headers.get("X-Usage-Token") != args.token:
                self._send(403, '{"ok":false,"error":"bad token"}', "application/json")
                return
            if save_snapshot(payload):
                label = payload["machine"].get("label", "?")
                print(f"  ← 수신: {label}", file=sys.stderr)
                self._send(200, '{"ok":true}', "application/json")
            else:
                self._send(400, '{"ok":false,"error":"schema"}', "application/json")

    return H


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ----------------------------------------------------------- 백그라운드 실행

PIDFILE = STORE / "server.json"
LOGFILE = STORE / "server.log"


def _ping(host, port, timeout=1.0):
    h = "127.0.0.1" if host in ("0.0.0.0", "", None, "::") else host
    try:
        with urllib.request.urlopen(f"http://{h}:{port}/api/ping", timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def running():
    """백그라운드 인스턴스가 살아 있으면 그 정보를 돌려준다."""
    try:
        info = _json.loads(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _ping(info.get("host", "127.0.0.1"), info.get("port", 8787)):
        return info
    return None


def _view_url(info):
    return f"http://127.0.0.1:{info['port']}/"


def _passthrough(args):
    out = []
    for flag, val in (("--host", args.host), ("--port", str(args.port)),
                      ("--machine", args.machine), ("--claude-dir", args.claude_dir),
                      ("--since", args.since), ("--until", args.until),
                      ("--pricing", args.pricing), ("--token", args.token)):
        if val:
            out += [flag, str(val)]
    for w in (args.watch or []):
        out += ["--watch", str(_Path(w).expanduser())]
    return out + ["--no-browser"]


def _launcher():
    """콘솔 창을 띄우지 않는 인터프리터를 고른다 (Windows: pythonw)."""
    exe = sys.executable
    if platform.system() == "Windows":
        cand = _Path(exe).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return exe


def do_daemon(args):
    live = running()
    if live:
        print(f"\n  이미 실행 중입니다 → {_view_url(live)}")
        print("  종료: --stop\n")
        if not args.no_browser:
            webbrowser.open(_view_url(live))
        return

    STORE.mkdir(parents=True, exist_ok=True)
    script = str(_Path(__file__).resolve())
    argv = [_launcher(), script] + _passthrough(args)
    log = LOGFILE.open("a", encoding="utf-8")
    log.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} 시작 ===\n")
    log.flush()

    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
              "cwd": str(_Path(script).parent)}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **kwargs)

    for _ in range(40):  # 최대 20초 대기
        if _ping(args.host, args.port, timeout=0.5):
            break
        if proc.poll() is not None:
            raise SystemExit(
                f"\n  백그라운드 시작에 실패했습니다. 원인은 로그에 있습니다:\n  {LOGFILE}\n"
            )
        time.sleep(0.5)
    else:
        raise SystemExit(
            f"\n  20초 안에 응답이 없어 포기했습니다. 로그를 확인하세요:\n  {LOGFILE}\n"
        )

    info = {"pid": proc.pid, "host": args.host, "port": args.port,
            "started_at": datetime.now().isoformat(timespec="seconds")}
    PIDFILE.write_text(_json.dumps(info), encoding="utf-8")

    url = _view_url(info)
    print(f"\n  백그라운드로 시작했습니다 (pid {proc.pid})")
    print(f"  대시보드  {url}")
    if args.host == "0.0.0.0":
        print(f"  다른 머신에서 → python3 claude-usage.py --push http://{lan_ip()}:{args.port}"
              + (f" --token {args.token}" if args.token else ""))
    print(f"  로그      {LOGFILE}")
    print("  상태 확인: --status   ·   종료: --stop\n")
    if not args.no_browser:
        webbrowser.open(url)


def _pid_is_python(pid):
    """Windows 에서는 그 pid 가 python 인터프리터인지 확인한다. 다른 OS 는 생략."""
    if platform.system() != "Windows":
        return True
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False
    return b"python" in out.lower()


def do_stop(args):
    try:
        info = _json.loads(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("\n  백그라운드로 돌고 있는 것이 없습니다.\n")
        return
    pid = info.get("pid")
    port = info.get("port", 8787)
    # 기록된 pid 가 아직 우리 데몬인지부터 본다. 데몬이 기록을 못 지우고 죽으면
    # (띄운 앱과 함께 강제 종료되는 경우 등) Windows 는 그 pid 를 곧 다른 프로세스에
    # 다시 준다. 확인 없이 taskkill /F 하면 그 엉뚱한 프로세스가 죽는다.
    alive = _ping(info.get("host", "127.0.0.1"), port)
    ours = bool(pid) and alive and _pid_is_python(pid)
    ok = False
    if ours:
        if platform.system() == "Windows":
            ok = os.system(f"taskkill /PID {pid} /F >nul 2>&1") == 0
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                ok = True
            except OSError:
                ok = False
    try:
        PIDFILE.unlink()
    except OSError:
        pass
    if ok:
        print(f"\n  종료했습니다 (pid {pid}).\n")
    elif ours:
        print(f"\n  pid {pid} 를 종료하지 못했습니다.\n")
    elif not alive:
        print(f"\n  기록된 인스턴스(pid {pid})가 응답하지 않아 이미 꺼진 것으로 봅니다.")
        print("  그 pid 는 지금 다른 프로세스일 수 있어 건드리지 않고 기록만 지웠습니다.\n")
    else:
        print(f"\n  포트 {port} 는 응답하지만 pid {pid} 가 python 이 아닙니다. 기록이 어긋나 있어")
        print(f"  아무것도 끄지 않고 기록만 지웠습니다. 포트 {port} 에 떠 있는 것은 직접 확인하세요.\n")


def do_status(args):
    live = running()
    if not live:
        print("\n  꺼져 있습니다.  시작: --daemon\n")
        return
    print(f"\n  실행 중 (pid {live.get('pid')}, {live.get('started_at', '?')} 시작)")
    print(f"  대시보드  {_view_url(live)}")
    if live.get("host") == "0.0.0.0":
        print(f"  외부 수신  열림 ― 이 PC 주소 {lan_ip()}")
    n = len(load_remote())
    if n:
        print(f"  보관 중인 원격 머신 {n}대")
    cz = _cache_size()
    if cz:
        print(f"  스캔 캐시  {cz/1e6:.0f}MB  (지우려면 --clear-cache)")
    print(f"  로그      {LOGFILE}")
    print("  종료: --stop\n")


def _cache_size():
    total = 0
    for d in (CACHE_DIR, ):
        try:
            for f in d.glob("*.json"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        except OSError:
            pass
    try:
        total += PAYLOAD_CACHE.stat().st_size
    except OSError:
        pass
    return total


def do_clear_cache(args):
    n = 0
    freed = _cache_size()
    try:
        for f in CACHE_DIR.glob("*.json"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    try:
        PAYLOAD_CACHE.unlink()
    except OSError:
        pass
    print(f"\n  캐시 {n}개 삭제, {freed/1e6:.0f}MB 확보.")
    print("  다음 실행 때 전체를 한 번 다시 읽습니다.\n")


def do_diag(args):
    """데이터 규모와 스캔 시간을 찍어본다. 느릴 때 원인 좁히는 용도."""
    root = Path(args.claude_dir).expanduser() / "projects"
    print("\n  트랜스크립트  %s" % root)
    n = 0
    total = 0
    try:
        for f in root.rglob("*.jsonl"):
            try:
                total += f.stat().st_size
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    print(f"  파일 {n:,}개, {total/1e9:.2f}GB")
    print(f"  스캔 캐시 {_cache_size()/1e6:.0f}MB")

    t0 = time.time()
    payload = scan_local(args)
    took = time.time() - t0
    src = payload.get("source", {})
    body = _json.dumps(payload, ensure_ascii=False)
    print(f"\n  스캔 {took:.2f}초")
    print(f"    재사용 {src.get('files_reused', 0):,}개 / 꼬리만 {src.get('files_tail', 0):,}개 "
          f"/ 전체파싱 {src.get('files_full', 0):,}개, 읽은 바이트 {src.get('bytes_read', 0)/1e6:.1f}MB")
    print(f"\n  브라우저로 보내는 데이터 {len(body.encode('utf-8'))/1e6:.2f}MB")
    print(f"    일자 {src.get('days', 0):,} · 프로젝트 {src.get('projects', 0):,} "
          f"· 모델 {src.get('models', 0):,} · 세션 {payload['totals'].get('sessions', 0):,}")
    print(f"    메시지 {payload['totals'].get('m', 0):,} · 토큰 {payload['totals'].get('total', 0):,}")
    print(f"    보관 중인 원격 머신 {len(load_remote())}대")
    print("\n  화면이 느리면 브라우저 화면 맨 아래 '계산 …ms · 그리기 …ms' 숫자도 함께 알려주세요.\n")


def do_serve(args):
    host = args.host
    handler = make_handler(args)
    try:
        srv = Server((host, args.port), handler)
    except OSError as e:
        code = getattr(e, "winerror", None) or e.errno
        if code in (errno.EADDRNOTAVAIL, 10049):
            raise SystemExit(
                f"\n  --host {host} 는 이 PC의 주소가 아닙니다.\n\n"
                "  --host 는 '이 PC의 어느 네트워크 카드로 열지'를 정하는 값입니다.\n"
                "  다른 머신의 주소를 넣는 자리가 아닙니다.\n\n"
                "    이 PC에서만 볼 때         →  --host 생략 (기본값)\n"
                "    다른 머신이 보내게 할 때  →  --host 0.0.0.0\n\n"
                f"  참고로 이 PC의 주소는 {lan_ip()} 입니다. 다른 머신에서는\n"
                f"  --push http://{lan_ip()}:{args.port} 로 이 PC를 가리키면 됩니다.\n"
            )
        if code in (errno.EADDRINUSE, 10048):
            raise SystemExit(
                f"\n  포트 {args.port} 는 이미 다른 프로그램이 쓰고 있습니다.\n"
                "  --port 18888 처럼 다른 번호를 지정하세요.\n"
            )
        raise SystemExit(f"\n  서버를 열 수 없습니다: {e}\n")

    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  대시보드  {url}")
    if host == "0.0.0.0":
        print(f"  다른 머신에서 → python3 claude-usage.py --push http://{lan_ip()}:{args.port}"
              + (f" --token {args.token}" if args.token else ""))
    else:
        print("  다른 머신에서도 보내려면 --host 0.0.0.0 으로 다시 실행하세요.")
    if args.watch:
        print(f"  감시 폴더  {', '.join(args.watch)}")
    n = len(load_remote())
    if n:
        print(f"  보관 중인 원격 머신 {n}대")
    print("  종료: Ctrl-C\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료했습니다.")
    finally:
        srv.server_close()


def do_push(args):
    payload = scan_local(args)
    body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = args.push.rstrip("/") + "/api/ingest"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if args.token:
        req.add_header("X-Usage-Token", args.token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:
        raise SystemExit(f"전송 실패 ({url}): {e}")
    t = payload["totals"]
    print(f"전송 완료 → {url}  [{payload['machine']['label']}] "
          f"{t['total']:,} tokens / {t['active_days']}일", file=sys.stderr)


def do_export(args):
    payload = scan_local(args)
    out = _Path(args.export).expanduser()
    if out.is_dir():
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", payload["machine"]["label"])
        out = out / f"{safe}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(out)
    print(f"저장됨: {out} ({out.stat().st_size/1024:.1f} KB)", file=sys.stderr)


def do_install(args):
    """이 머신에 주기 실행을 등록한다 (cron / launchd / 작업 스케줄러)."""
    script = _Path(__file__).resolve()
    system = platform.system()

    if args.push:
        mode, when = "push", "1시간마다"
        py = sys.executable
        target = ["--push", args.push]
    elif args.export:
        mode, when = "push", "1시간마다"
        py = sys.executable
        target = ["--export", str(_Path(args.export).expanduser())]
    else:
        # 대상을 안 주면 = 대시보드를 로그온할 때 자동으로 띄운다
        mode, when = "serve", "로그온할 때"
        py = _launcher()
        target = ["--daemon", "--no-browser", "--host", args.host, "--port", str(args.port)]
        for w in (args.watch or []):
            target += ["--watch", str(_Path(w).expanduser())]
    if args.machine:
        target += ["--machine", args.machine]
    if args.token:
        target += ["--token", args.token]

    def q(t):
        return f'"{t}"' if " " in str(t) else str(t)

    cmd = " ".join([q(py), q(script)] + [q(t) for t in target])

    if system == "Windows":
        sc = "/SC ONLOGON" if mode == "serve" else "/SC HOURLY"
        print(f"\n  PowerShell에서 아래를 실행하세요 ({when}):\n")
        print(f'  schtasks /Create {sc} /TN "ClaudeUsage" /TR "{cmd}" /F\n')
        print('  해제:  schtasks /Delete /TN "ClaudeUsage" /F\n')
        return

    if system == "Darwin":
        plist = _Path.home() / "Library/LaunchAgents/dev.claude-usage.plist"
        argv = [str(py), str(script)] + [str(t) for t in target]
        items = "".join(f"      <string>{a}</string>\n" for a in argv)
        sched = ("  <key>KeepAlive</key><true/>\n" if mode == "serve"
                 else "  <key>StartInterval</key><integer>3600</integer>\n")
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>Label</key><string>dev.claude-usage</string>\n"
            f"  <key>ProgramArguments</key><array>\n{items}  </array>\n"
            f"{sched}"
            "  <key>RunAtLoad</key><true/>\n"
            "</dict></plist>\n"
        )
        if args.dry_run:
            print(f"\n--- {plist} ---\n{body}")
            return
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(body, encoding="utf-8")
        os.system(f'launchctl unload "{plist}" 2>/dev/null')
        rc = os.system(f'launchctl load "{plist}"')
        print(f"\n  등록 완료: {plist}  ({when})")
        print(f"  해제:  launchctl unload {plist} && rm {plist}\n")
        if rc:
            print("  launchctl 등록이 실패했을 수 있습니다. 위 경로를 확인하세요.\n")
        return

    # Linux: cron
    line = ("@reboot " if mode == "serve" else "0 * * * * ") + cmd
    if args.dry_run or not shutil.which("crontab"):
        print(f"\n  crontab -e 에 아래 한 줄을 추가하세요 ({when}):\n\n  {line}\n")
        return
    cur = os.popen("crontab -l 2>/dev/null").read()
    if str(script) in cur:
        print("\n  이미 등록되어 있습니다. crontab -l 로 확인하세요.\n")
        return
    new = (cur.rstrip("\n") + "\n" if cur.strip() else "") + line + "\n"
    p = os.popen("crontab -", "w")
    p.write(new)
    if p.close() is None:
        print(f"\n  등록 완료 ({when}):\n  {line}\n  해제:  crontab -e 로 해당 줄 삭제\n")
    else:
        print(f"\n  자동 등록 실패. crontab -e 에 직접 추가하세요:\n\n  {line}\n")


def main():
    ap = argparse.ArgumentParser(
        prog="claude-usage",
        description="Claude Code 토큰 사용량 대시보드 (단일 파일)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=console_safe(__doc__),
    )
    ap.add_argument("--port", type=int, default=8787, help="대시보드 포트 (기본 8787)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 으로 열면 다른 머신이 --push 로 보낼 수 있다")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않는다")
    ap.add_argument("--watch", action="append", metavar="DIR",
                    help="공유 폴더를 감시해 다른 머신의 JSON을 읽어들인다 (여러 번 지정 가능)")
    ap.add_argument("--push", metavar="URL", help="이 머신 사용량을 대시보드로 전송하고 종료")
    ap.add_argument("--export", metavar="PATH", help="이 머신 사용량을 JSON 파일로 저장하고 종료")
    ap.add_argument("--token", help="--push 인증용 공유 비밀문자열")
    ap.add_argument("--daemon", action="store_true",
                    help="백그라운드로 띄우고 터미널을 돌려준다 (창을 닫아도 계속 돈다)")
    ap.add_argument("--stop", action="store_true", help="백그라운드 인스턴스를 종료")
    ap.add_argument("--status", action="store_true", help="백그라운드 인스턴스 상태 확인")
    ap.add_argument("--diag", action="store_true",
                    help="데이터 규모와 스캔 시간을 출력한다 (느릴 때 원인 확인용)")
    ap.add_argument("--clear-cache", action="store_true",
                    help="스캔 캐시를 지운다 (다음 실행 때 전체를 다시 읽음)")
    ap.add_argument("--install", action="store_true",
                    help="자동 시작 등록. 단독이면 대시보드를 로그온할 때 자동 실행, "
                         "--push/--export 와 함께면 1시간마다 전송")
    ap.add_argument("--dry-run", action="store_true", help="--install 시 등록 대신 내용만 출력")
    ap.add_argument("--machine", help="머신 이름 (기본: hostname)")
    ap.add_argument("--claude-dir", default="~/.claude", help="Claude Code 홈 (기본 ~/.claude)")
    ap.add_argument("--since", help="시작일 YYYY-MM-DD")
    ap.add_argument("--until", help="종료일 YYYY-MM-DD")
    ap.add_argument("--pricing", help="비용 추정 단가표 JSON (USD/1M tokens)")
    ap.add_argument("--print-summary", action="store_true", help="터미널에 사용량 요약 출력")
    args = ap.parse_args()

    if args.diag:
        do_diag(args)
    elif args.clear_cache:
        do_clear_cache(args)
    elif args.stop:
        do_stop(args)
    elif args.status:
        do_status(args)
    elif args.install:
        do_install(args)
    elif args.daemon:
        do_daemon(args)
    elif args.push:
        do_push(args)
    elif args.export:
        do_export(args)
    elif args.print_summary:
        print_summary(scan_local(args))
    else:
        do_serve(args)


if __name__ == "__main__":
    main()
