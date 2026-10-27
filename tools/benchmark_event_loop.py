"""Compare identical blocking store calls with and without the async boundary.

Default: synthetic storage latency, no network. --postgres additionally requires
TEST_POSTGRES_DSN to identify a loopback-only, explicitly named test database.
No bot, model provider, QQ connection or schema mutation is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import time
from collections.abc import Callable

from psycopg.conninfo import conninfo_to_dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot_storage import DatabaseCalls, PostgresDatabase


def validate_test_dsn(dsn: str) -> None:
    values = conninfo_to_dict(dsn)
    hosts = str(values.get("host", "")).split(",")
    if not hosts or not all(
        host in {"127.0.0.1", "::1"} and ipaddress.ip_address(host).is_loopback
        for host in hosts
    ):
        raise ValueError("Benchmark requires an explicit loopback test database")
    if values.get("hostaddr") or values.get("service"):
        raise ValueError("Alternate database routing is not allowed")
    if not str(values.get("dbname", "")).endswith(("_test", "_acceptance")):
        raise ValueError("Benchmark database name must end in _test or _acceptance")


async def measure(
    query: Callable[[], object], *, offload: bool, iterations: int, interval: float
) -> dict[str, float | int]:
    calls = DatabaseCalls()
    samples: list[float] = []
    loop = asyncio.get_running_loop()

    async def heartbeat():
        while True:
            expected = loop.time() + interval
            await asyncio.sleep(interval)
            samples.append(max(loop.time() - expected, 0.0))

    monitor = asyncio.create_task(heartbeat())
    started = loop.time()
    try:
        await asyncio.sleep(interval * 2)
        for _ in range(iterations):
            if offload:
                await calls(query)
            else:
                query()
            await asyncio.sleep(interval * 2)
    finally:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        await calls.close()
    ordered = sorted(samples)
    return {
        "queries": iterations,
        "heartbeat_samples": len(samples),
        "elapsed_seconds": round(loop.time() - started, 4),
        "lag_p99_ms": round(
            ordered[max(math.ceil(len(ordered) * 0.99) - 1, 0)] * 1000, 4
        ),
        "lag_max_ms": round(max(samples) * 1000, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--delay-ms", type=float, default=30)
    args = parser.parse_args()
    if not 5 <= args.iterations <= 200 or not 5 <= args.delay_ms <= 100:
        parser.error("Use 5-200 iterations and 5-100 milliseconds of storage delay")
    database = connection = None
    delay = args.delay_ms / 1000
    query: Callable[[], object] = lambda: time.sleep(delay)
    if args.postgres:
        dsn = os.environ.get("TEST_POSTGRES_DSN", "")
        validate_test_dsn(dsn)
        database = PostgresDatabase(
            dsn,
            schema=os.environ.get("TEST_POSTGRES_SCHEMA", "qq_bot_test"),
            min_size=1,
            max_size=2,
            application_name="gaoji-loop-benchmark",
        )
        connection = database.store_connection()
        query = lambda: connection.execute("SELECT pg_sleep(?)", (delay,)).fetchone()

    async def compare():
        result = {
            "mode": "loopback_postgresql" if args.postgres else "synthetic",
            "injected_store_delay_ms": args.delay_ms,
            "sample_interval_ms": 5,
        }
        for name, offload in (("before_inline", False), ("after_db_call", True)):
            result[name] = await measure(
                query, offload=offload, iterations=args.iterations, interval=0.005
            )
        return result

    try:
        print(json.dumps(asyncio.run(compare()), indent=2))
    finally:
        if connection is not None:
            connection.close()
        if database is not None:
            database.close()


if __name__ == "__main__":
    main()
