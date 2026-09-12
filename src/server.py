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

@@CORE@@


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
PAGE = r"""@@HTML@@"""

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
CACHE_VERSION = 3
CODEX_CACHE_VERSION = 1


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
    """start_off 부터 완결된 줄만 파싱한다. (rows, 한도 기록, 새 offset) 반환."""
    rows = []
    hits = []
    m_index = {n: i for i, n in enumerate(models)}
    s_index = {n: i for i, n in enumerate(sessions)}
    off = start_off
    try:
        fh = open(str(path), "rb")
    except OSError:
        return rows, hits, start_off
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
                msg = {}
            session = rec.get("sessionId") or path.stem
            hit = quota_hit(rec, None, session)
            if hit is not None:
                hits.append(hit)
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
            if session not in s_index:
                s_index[session] = len(sessions)
                sessions.append(session)

            local = dt.astimezone()
            rows.append([
                dedup, local.strftime("%Y-%m-%d"), local.hour, local.weekday(),
                m_index[model], s_index[session],
            ] + usage_values(usage))
    return rows, hits, off


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
            new_rows, new_hits, off = _read_rows(path, prev_off, models, sessions)
            entry["r"] = entry.get("r", []) + new_rows
            entry["q"] = entry.get("q", []) + new_hits
            entry["ms"], entry["ss"], entry["off"] = models, sessions, off
            entry["sz"], entry["mt"] = size, mtime
            stats["tail"] += 1
            stats["bytes"] += max(0, size - prev_off)
        else:
            models, sessions = [], []
            rows, hits, off = _read_rows(path, 0, models, sessions)
            entry = {"sz": size, "mt": mtime, "off": off,
                     "ms": models, "ss": sessions, "r": rows, "q": hits}
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
CODEX_CACHE = STORE / "codex-cache.json"


def _codex_entries():
    """Codex 트리 하나를 append-only 캐시 하나로 읽는다."""
    root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    if not root.is_dir():
        return []
    try:
        with CODEX_CACHE.open("r", encoding="utf-8") as f:
            cache = _json.load(f)
        if cache.get("v") != CODEX_CACHE_VERSION or cache.get("root") != str(root):
            raise ValueError()
    except (OSError, ValueError):
        cache = {"v": CODEX_CACHE_VERSION, "root": str(root), "files": {}}
    files = cache.get("files", {})
    live = set()
    dirty = False
    for base in (root / "sessions", root / "archived_sessions"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("rollout-*.jsonl")):
            try:
                rel = str(path.relative_to(root))
                st = path.stat()
            except (OSError, ValueError):
                continue
            live.add(rel)
            size, mtime = st.st_size, int(st.st_mtime)
            entry = files.get(rel)
            if entry and entry.get("sz") == size and entry.get("mt") == mtime:
                continue
            if entry and size >= entry.get("off", 0) and entry.get("off", 0) > 0:
                daily, last, limits, off = parse_codex_file(
                    path, entry["off"], entry.get("last"))
                for day, bucket in daily.items():
                    add_bucket(entry.setdefault("daily", {}).setdefault(day, new_bucket()), bucket)
                entry["last"], entry["off"] = last, off
                if limits and (not entry.get("limits") or
                               limits.get("at", "") > entry["limits"].get("at", "")):
                    entry["limits"] = limits
                entry["sz"], entry["mt"] = size, mtime
            else:
                daily, last, limits, off = parse_codex_file(path)
                entry = {"sz": size, "mt": mtime, "off": off, "last": last,
                         "daily": daily, "limits": limits}
            files[rel] = entry
            dirty = True
    for gone in [name for name in files if name not in live]:
        del files[gone]
        dirty = True
    if dirty:
        try:
            STORE.mkdir(parents=True, exist_ok=True)
            tmp = CODEX_CACHE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                _json.dump(cache, f, separators=(",", ":"))
            tmp.replace(CODEX_CACHE)
        except OSError:
            pass
    return list(files.values())


def _load_payload_cache(fp, args):
    """디렉터리 지문이 그대로면 지난번 결과를 그대로 쓴다 (재시작 대비)."""
    if args.since or args.until or args.pricing:
        return None
    try:
        with PAYLOAD_CACHE.open("r", encoding="utf-8") as f:
            d = _json.load(f)
    except (OSError, ValueError):
        return None
    if (d.get("fp") != list(fp) or d.get("schema_v") != SCHEMA_VERSION or
            d.get("cache_v") != CACHE_VERSION):
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
            _json.dump({"fp": list(fp), "schema_v": SCHEMA_VERSION,
                        "cache_v": CACHE_VERSION, "payload": payload},
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
    limit_hits = []
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
        for cached_hit in entry.get("q", []):
            hit = dict(cached_hit)
            hit["project"] = pj
            limit_hits.append(hit)
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
    plan = load_plan(args.claude_dir)
    if plan:
        payload["plan"] = plan
    limit_hits = finish_limit_hits(limit_hits, args.since, args.until)
    if limit_hits:
        payload["limit_hits"] = limit_hits
    codex = make_codex_payload(_codex_entries(), args.since, args.until)
    if codex:
        payload["codex"] = codex
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
    """Claude·Codex 파일 개수·총 크기·최신 mtime. 내용을 읽지 않아 싸다."""
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
    codex_root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    for base in (codex_root / "sessions", codex_root / "archived_sessions"):
        try:
            paths = base.rglob("rollout-*.jsonl") if base.is_dir() else []
            for p in paths:
                try:
                    st = p.stat()
                except OSError:
                    continue
                n += 1
                total += st.st_size
                newest = max(newest, st.st_mtime)
        except OSError:
            pass
    plan = Path(str(root.parent) + ".json")
    try:
        st = plan.stat()
        total += st.st_size
        newest = max(newest, st.st_mtime)
    except OSError:
        pass
    return (n, total, int(newest), str(codex_root))


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
    try:
        total += CODEX_CACHE.stat().st_size
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
    try:
        CODEX_CACHE.unlink()
        n += 1
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
