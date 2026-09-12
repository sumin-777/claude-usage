#!/usr/bin/env python3
"""
claude-usage-merge ― 여러 머신의 수집 결과를 하나로 합친다.

각 PC/서버에서 claude-usage-collect.py 로 만든 JSON들을 한 폴더(동기화 폴더,
git repo 등)에 모아두고 이 스크립트를 돌리면 대시보드가 읽는 merged.json 이 나온다.

    python3 claude-usage-merge.py ./data -o ./data/merged.json

같은 머신 id의 파일이 여러 개면 generated_at 이 가장 최신인 것만 쓴다.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2
KEYS = ("i", "o", "cw", "cr", "cw1", "cw5", "th", "m")


def merge_bucket(dst, src):
    for k in KEYS:
        dst[k] = dst.get(k, 0) + src.get(k, 0)
    return dst


def normalize_machine(d):
    for bucket in [d.get("totals", {})]:
        if isinstance(bucket, dict):
            for key in ("cw1", "cw5", "th"):
                bucket.setdefault(key, 0)
    for section in ("daily", "models", "projects"):
        buckets = d.get(section, {})
        if not isinstance(buckets, dict):
            continue
        for bucket in buckets.values():
            if isinstance(bucket, dict):
                for key in ("cw1", "cw5", "th"):
                    bucket.setdefault(key, 0)
    return d


def load_machines(paths):
    """머신 id별로 가장 최신 스냅샷만 남긴다."""
    best = {}
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! 건너뜀 {p}: {e}", file=sys.stderr)
            continue
        if d.get("schema") not in (1, 2) or "machine" not in d:
            print(f"  ! 형식 불일치, 건너뜀: {p}", file=sys.stderr)
            continue
        normalize_machine(d)
        mid = d["machine"]["id"]
        prev = best.get(mid)
        if prev is None or d.get("generated_at", "") > prev.get("generated_at", ""):
            best[mid] = d
    return list(best.values())


def merge(machines):
    daily = defaultdict(dict)
    daily_by_machine = defaultdict(dict)
    models = defaultdict(dict)
    projects = defaultdict(dict)
    hours = [0] * 24
    weekday_hour = [[0] * 24 for _ in range(7)]
    totals = {k: 0 for k in KEYS}
    totals["sessions"] = 0
    codex_daily = defaultdict(dict)
    codex_limits = None
    limit_hits = {}

    for d in machines:
        label = d["machine"]["label"]
        for day, b in d.get("daily", {}).items():
            merge_bucket(daily[day], b)
            daily[day]["s"] = daily[day].get("s", 0) + b.get("s", 0)
            daily_by_machine[day][label] = (
                daily_by_machine[day].get(label, 0)
                + sum(b.get(k, 0) for k in ("i", "o", "cw", "cr"))
            )
        for name, b in d.get("models", {}).items():
            merge_bucket(models[name], b)
        for name, b in d.get("projects", {}).items():
            tgt = projects[f"{name}"]
            merge_bucket(tgt, b)
            if b.get("last") and b["last"] > tgt.get("last", ""):
                tgt["last"] = b["last"]
        for i, v in enumerate(d.get("hours", [])[:24]):
            hours[i] += v
        for wd, row in enumerate(d.get("weekday_hour", [])[:7]):
            for h, v in enumerate(row[:24]):
                weekday_hour[wd][h] += v
        for k in KEYS:
            totals[k] += d.get("totals", {}).get(k, 0)
        totals["sessions"] += d.get("totals", {}).get("sessions", 0)
        codex = d.get("codex")
        if isinstance(codex, dict):
            for day, b in codex.get("daily", {}).items():
                merge_bucket(codex_daily[day], b)
            limits = codex.get("limits")
            if limits and (codex_limits is None or
                           limits.get("at", "") > codex_limits.get("at", "")):
                codex_limits = limits
        for hit in d.get("limit_hits") or []:
            key = (hit.get("requestId"), hit.get("timestamp"))
            item = dict(hit)
            item["machine"] = label
            limit_hits[key] = item

    days = sorted(daily.keys())
    totals["total"] = sum(totals[k] for k in ("i", "o", "cw", "cr"))
    totals["active_days"] = len(days)

    cur = best = 0
    if days:
        dates = [datetime.strptime(x, "%Y-%m-%d").date() for x in days]
        run = best = 1
        for a, b in zip(dates, dates[1:]):
            run = run + 1 if (b - a).days == 1 else 1
            best = max(best, run)
        cur = run if (datetime.now().date() - dates[-1]).days <= 1 else 0

    out = {
        "schema": SCHEMA_VERSION,
        "kind": "merged",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "machines": [
            {
                **m["machine"],
                "generated_at": m.get("generated_at"),
                "tz": m.get("tz"),
                "range": m.get("range"),
                "totals": m.get("totals"),
                "plan": m.get("plan"),
            }
            for m in machines
        ],
        "range": {"first": days[0] if days else None, "last": days[-1] if days else None},
        "totals": totals,
        "streak": {"current": cur, "max": best},
        "peak_hour": max(range(24), key=lambda h: hours[h]) if any(hours) else None,
        "top_model": (
            max(models.items(), key=lambda kv: sum(kv[1].get(k, 0) for k in ("i", "o", "cw", "cr")))[0]
            if models else None
        ),
        "daily": dict(daily),
        "daily_by_machine": {d: v for d, v in daily_by_machine.items()},
        "models": dict(models),
        "projects": dict(projects),
        "hours": hours,
        "weekday_hour": weekday_hour,
    }
    if codex_daily or codex_limits:
        codex_totals = {k: 0 for k in KEYS}
        for b in codex_daily.values():
            merge_bucket(codex_totals, b)
        codex_totals["total"] = sum(codex_totals[k] for k in ("i", "o", "cw", "cr"))
        out["codex"] = {"daily": dict(codex_daily), "totals": codex_totals}
        if codex_limits:
            out["codex"]["limits"] = codex_limits
    if limit_hits:
        out["limit_hits"] = sorted(
            limit_hits.values(), key=lambda x: x.get("timestamp", ""), reverse=True)[:200]
    return out


def main():
    ap = argparse.ArgumentParser(description="머신별 수집 JSON을 하나로 병합")
    ap.add_argument("inputs", nargs="+", help="JSON 파일 또는 폴더")
    ap.add_argument("-o", "--output", default="merged.json")
    args = ap.parse_args()

    paths = []
    for i in args.inputs:
        p = Path(i).expanduser()
        if p.is_dir():
            paths += [x for x in sorted(p.glob("*.json")) if x.name != Path(args.output).name]
        elif p.is_file():
            paths.append(p)

    machines = load_machines(paths)
    if not machines:
        raise SystemExit("병합할 수집 파일이 없습니다.")

    out = merge(machines)
    Path(args.output).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(
        f"머신 {len(machines)}대 병합 → {args.output}  "
        f"({out['totals']['total']:,} tokens, {out['totals']['active_days']}일)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
