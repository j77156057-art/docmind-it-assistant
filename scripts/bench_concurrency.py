#!/usr/bin/env python3
"""Offline concurrency benchmark for DocMind (A-5).

Builds the query app in-memory (no server, no network) and hammers a read-only endpoint with N
concurrent workers, then reports latency percentiles, throughput, and the live metrics snapshot.

Usage:
    python scripts/bench_concurrency.py --concurrency 8 --requests 128 --endpoint model
    python scripts/bench_concurrency.py --endpoint query --question "VPN 连接失败" --concurrency 4

`--endpoint query` uses the built-in knowledge router (no model network call), which is the safe
default for an offline run; it does write query history to a throwaway SQLite file.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Scripts live one level below the project root; make `app`/`backend` importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app import create_app
from backend import AppSettings
from backend.metrics import get_metrics, reset_metrics


def _build_app(root: Path) -> "object":
    (root / "knowledge.md").write_text(
        "# IT\n## VPN 连接失败\n同步设备时间后重试 VPN。\n", encoding="utf-8",
    )
    web = root / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    settings = AppSettings(
        project_root=root,
        environment="test",
        database_url=f"sqlite:///{(root / 'queries.db').as_posix()}",
        knowledge_path=root / "knowledge.md",
        web_index_path=web / "index.html",
        artifact_output_path=root / "artifacts",
        auth_mode="development",
        auth_subject_salt="bench-subject-salt",
        log_level="CRITICAL",
    )
    return create_app(settings)


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(p / 100.0 * (len(ordered) - 1)))))
    return ordered[idx]


def main() -> None:
    parser = argparse.ArgumentParser(description="DocMind offline concurrency benchmark")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--endpoint", choices=["model", "query"], default="model")
    parser.add_argument("--question", default="VPN 连接失败")
    args = parser.parse_args()

    reset_metrics()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        application = _build_app(root)

        latencies: list[float] = []
        failures = 0
        wall_start = time.perf_counter()
        # One shared client: its lifespan creates the schema once. httpx's transport is safe for
        # concurrent sends, which is exactly what we want to measure.
        with TestClient(application) as client:
            def fire_once() -> tuple[int, float]:
                started = time.perf_counter()
                if args.endpoint == "query":
                    resp = client.post("/api/query", json={"question": args.question})
                else:
                    resp = client.get("/api/runtime/model")
                elapsed = (time.perf_counter() - started) * 1000.0
                return resp.status_code, elapsed

            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                results = list(pool.map(lambda _: fire_once(), range(args.requests)))
        wall = time.perf_counter() - wall_start

        for code, elapsed in results:
            latencies.append(elapsed)
            if code >= 400:
                failures += 1

        snapshot = get_metrics().snapshot()
        rps = args.requests / wall if wall > 0 else 0.0
        print("=" * 56)
        print(f"endpoint={args.endpoint}  concurrency={args.concurrency}  requests={args.requests}")
        print("-" * 56)
        print(f"success={args.requests - failures}  failures={failures}")
        print(f"wall={wall:.3f}s  throughput={rps:.1f} req/s")
        print(f"latency_ms  min={min(latencies):.2f}  mean={statistics.mean(latencies):.2f}  "
              f"max={max(latencies):.2f}")
        print(f"latency_ms  p50={_pct(latencies, 50):.2f}  "
              f"p95={_pct(latencies, 95):.2f}  p99={_pct(latencies, 99):.2f}")
        print("-" * 56)
        print(f"metrics.requests_total={snapshot['requests_total']}  "
              f"recorded_samples={snapshot['request_duration_ms']['samples']}")
        print("=" * 56)


if __name__ == "__main__":
    main()
