#!/usr/bin/env python3
"""Analyze DocMind slow-operation logs to inform the decision-C pool-sizing call.

Reads the structured JSON logs produced by ``backend/logging_config.py`` (default
``data/logs/*.err.log``) and tallies the operational slow-event signals that the
``async def`` -> ``def`` migration (proposal §9.1, decision C) needs to watch:

  * ``slow_db_query``     : a single SQL statement exceeded ``IT_SLOW_DB_MS`` (default 200ms).
                           -> the SQL itself is slow (missing index / lock / DB load).
                           NOTE: connection-wait time is NOT counted here (the timer starts
                           only after a connection is acquired).
  * ``slow_request``     : a whole request exceeded ``IT_SLOW_REQUEST_MS`` (default 1000ms).
                           -> request slow end-to-end. Thread-pool OR connection-pool queueing
                           surfaces HERE, never in ``slow_db_query``.
  * ``slow_admin_request`` : same, for the admin app.

It prints a decision-oriented summary keyed to proposal §9.1 §四:

  * slow_db_query present  -> slow SQL; fix queries/indexes (pool resize won't help this).
  * slow_request on DB routes + pool TimeoutError/5xx -> connection-pool saturation;
    raise IT_DB_POOL_SIZE / IT_DB_MAX_OVERFLOW (constraint chain: anyio 40 > PG pool 15).
  * neither                -> defaults adequate under the observed load.

Usage:
    python scripts/analyze_slow_logs.py                  # all data/logs/*.err.log
    python scripts/analyze_slow_logs.py --since 1h      # only the last hour
    python scripts/analyze_slow_logs.py data/logs/query.err.log
    python scripts/analyze_slow_logs.py --slow-db-ms 200 --slow-request-ms 1000
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SLOW_EVENTS = ("slow_db_query", "slow_request", "slow_admin_request")
DEFAULT_GLOB = "data/logs/*.err.log"
# Resolve the default log glob against the repo root (two levels up from this script) so the
# tool works regardless of the caller's current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_since(spec: str) -> datetime | None:
    """Accept '30m'/'1h'/'2d' relative, or an absolute ISO timestamp."""
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)\s*([smhd])", spec.strip())
    if m:
        n = int(m.group(1))
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(seconds=n * unit)
    # absolute ISO
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
               "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(spec, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise SystemExit(f"cannot parse --since: {spec!r}")


def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    idx = min(len(values) - 1, max(0, int(round(p / 100.0 * (len(values) - 1)))))
    return round(values[idx], 2)


def _load(paths: list[str], since: datetime | None) -> tuple[list[dict], dict]:
    """Return (filtered_rows, meta).

    meta carries scan statistics so the caller can explain an empty result:
      files   - number of log files matched & opened
      scanned - number of JSON records parsed
      min_ts  - earliest parsed timestamp (UTC-aware), or None
      max_ts  - latest parsed timestamp (UTC-aware), or None
    """
    rows: list[dict] = []
    meta: dict = {"files": 0, "scanned": 0, "min_ts": None, "max_ts": None}
    for p in paths:
        for fn in glob.glob(p):
            meta["files"] += 1
            try:
                with open(fn, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        meta["scanned"] += 1
                        ts = rec.get("timestamp")
                        dt = None
                        if ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            except ValueError:
                                dt = None
                        if dt is not None:
                            if meta["min_ts"] is None or dt < meta["min_ts"]:
                                meta["min_ts"] = dt
                            if meta["max_ts"] is None or dt > meta["max_ts"]:
                                meta["max_ts"] = dt
                        if since is not None and dt is not None and dt < since:
                            continue
                        rows.append(rec)
            except OSError as e:
                print(f"  warn: cannot read {fn}: {e}", file=sys.stderr)
    return rows, meta


def _fmt(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _report_empty(meta: dict, since: datetime | None, paths: list[str]) -> None:
    """Explain why no rows were produced instead of a bare 'no match'."""
    if meta["files"] == 0:
        print("no log files matched the given paths (check paths / glob):")
        for p in paths:
            print(f"  {p}")
        return
    if since is None:
        print(f"no parseable log rows in {meta['files']} file(s) "
              f"({meta['scanned']} rows scanned). Check log format / paths.")
        return
    print("no log rows matched the time window.")
    print(f"  已扫描 {meta['scanned']} 行（来自 {meta['files']} 个文件）")
    print(f"  日志时间跨度: {_fmt(meta['min_ts'])} → {_fmt(meta['max_ts'])}")
    print(f"  --since 要求 ≥ {_fmt(since)}")
    print("  -> 放宽 --since（如 --since 30d）或确认存在新鲜日志。")


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggregate DocMind slow logs for decision C")
    ap.add_argument("paths", nargs="*", default=[DEFAULT_GLOB],
                    help=f"glob(s) of JSON log files (default: {DEFAULT_GLOB})")
    ap.add_argument("--since", default="", help="only events newer than '30m'/'1h'/'2d' or ISO")
    ap.add_argument("--slow-db-ms", type=int, default=200, help="echoed threshold")
    ap.add_argument("--slow-request-ms", type=int, default=1000, help="echoed threshold")
    args = ap.parse_args()

    since = _parse_since(args.since) if args.since else None
    if args.paths == [DEFAULT_GLOB]:
        args.paths = [str(REPO_ROOT / DEFAULT_GLOB)]
    rows, meta = _load(args.paths, since)
    if not rows:
        _report_empty(meta, since, args.paths)
        return

    by_event: dict[str, list[dict]] = defaultdict(list)
    path_counts: Counter = Counter()
    status_5xx = 0
    timeout_hits = 0
    for r in rows:
        ev = r.get("event")
        if ev in SLOW_EVENTS:
            by_event[ev].append(r)
        if ev in ("slow_request", "slow_admin_request"):
            path_counts[r.get("path", "?")] += 1
        sc = r.get("status_code")
        if isinstance(sc, int) and 500 <= sc < 600:
            status_5xx += 1
        msg = str(r.get("event", "")) + str(r.get("error_type", "")) + str(r.get("message", ""))
        if "TimeoutError" in msg or "pool_timeout" in msg or "timeout" in msg.lower():
            timeout_hits += 1

    print(f"\n=== slow-log analysis (decision C) ===")
    print(f"rows scanned: {len(rows)}   window: {args.since or 'all'}")
    print(f"thresholds: slow_db_ms={args.slow_db_ms}  slow_request_ms={args.slow_request_ms}")
    print("-" * 56)

    for ev in SLOW_EVENTS:
        recs = by_event.get(ev, [])
        durs = [float(r["duration_ms"]) for r in recs if r.get("duration_ms") is not None]
        if not recs:
            print(f"{ev:20} : 0 events")
            continue
        print(f"{ev:20} : {len(recs)} events | "
              f"p50={_pct(durs,50)} p95={_pct(durs,95)} p99={_pct(durs,99)} "
              f"max={round(max(durs),2)} ms")

    if path_counts:
        print("\nslow_request by path (top 10):")
        for path, n in path_counts.most_common(10):
            print(f"  {n:6}  {path}")

    print("\n--- decision signals ---")
    dbq = len(by_event.get("slow_db_query", []))
    srq = len(by_event.get("slow_request", [])) + len(by_event.get("slow_admin_request", []))
    print(f"slow_db_query events : {dbq}")
    print(f"slow_request events : {srq}")
    print(f"5xx responses       : {status_5xx}")
    print(f"timeout/pool hints  : {timeout_hits}")

    print("\n--- recommendation ---")
    if dbq:
        print("  * slow_db_query present -> individual SQL is slow (index/lock/DB load). "
              "Tuning database_pool_size will NOT fix this; fix the query/index first.")
    if timeout_hits or (srq and status_5xx):
        print("  * pool TimeoutError / 5xx under load -> CONNECTION-POOL SATURATION is the "
              "likely bottleneck. Raise IT_DB_POOL_SIZE / IT_DB_MAX_OVERFLOW (constraint chain: "
              "anyio 40 > PG pool 15). Keep anyio tokens at default unless pool is fixed first.")
    elif srq:
        print("  * slow_request present but no pool-timeout/5xx -> check whether it concentrates "
              "on DB routes (see 'by path'); if so, still lean toward raising the PG pool before "
              "anyio tokens. If it's on non-DB routes, look at handler blocking instead.")
    else:
        print("  * No slow_db_query / slow_request / timeout signals in this window -> current "
              "defaults (pool_size=5, max_overflow=10, anyio 40) are adequate for the OBSERVED "
              "load. Re-run this after a higher-traffic window before concluding.")


if __name__ == "__main__":
    main()
