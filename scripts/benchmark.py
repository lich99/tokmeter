"""Opt-in local benchmark. Temporary synthetic inputs are deleted on exit."""

import argparse
import gc
import json
import resource
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from tokmeter.engine import Store
from tokmeter.pricing import Catalog
from tokmeter.query import aggregate
from tokmeter.sources import Sources


def measure(sources, workers):
    store = Store(sources, Catalog(), workers=workers)
    started = time.perf_counter()
    snapshot = store.refresh()
    initial = time.perf_counter() - started
    end = int(time.time() * 1000)
    start = int(snapshot.frame["ts"].min()) if snapshot.frame.height else end - 86400000
    started = time.perf_counter()
    aggregate(snapshot, start, end, "1d", "UTC", False, "all")
    query = time.perf_counter() - started
    started = time.perf_counter()
    second = store.refresh()
    refresh = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = dict(
        load_seconds=round(initial, 4),
        query_seconds=round(query, 4),
        refresh_seconds=round(refresh, 4),
        rows=snapshot.frame.height,
        files=snapshot.scan["scanned"],
        source_bytes=snapshot.scan["bytes_read"],
        refresh_bytes=second.scan["bytes_read"],
        process_peak_mib=round(peak / (1048576 if sys.platform == "darwin" else 1024), 1),
        diagnostics={
            k: v
            for k, v in snapshot.scan.items()
            if k in ("malformed", "invalid_usage", "duplicate_snapshots", "inherited_events", "pending_files")
        },
        read_errors=len(snapshot.scan["errors"]),
    )
    store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="Read local Claude/Codex logs; prints only aggregate performance metadata",
    )
    parser.add_argument("--workers", type=int, default=8, choices=range(1, 33))
    parser.add_argument("--runs", type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    with TemporaryDirectory(prefix="tokmeter-bench-") as directory:
        root = Path(directory)
        sources = Sources.defaults() if args.real else Sources(root / "claude", root / "codex")
        if not args.real:
            path = sources.codex / "sessions/synthetic.jsonl"
            path.parent.mkdir(parents=True)
            with path.open("w") as file:
                file.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n")
                for i in range(10000):
                    file.write(
                        json.dumps(
                            {
                                "type": "event_msg",
                                "timestamp": "2026-01-01T00:00:00Z",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "last_token_usage": {"input_tokens": 1000, "output_tokens": 100},
                                        "total_token_usage": {
                                            "input_tokens": 1000 * (i + 1),
                                            "output_tokens": 100 * (i + 1),
                                        },
                                    },
                                },
                            }
                        )
                        + "\n"
                    )
        for run in range(args.runs):
            gc.collect()
            print(json.dumps({"run": run + 1, **measure(sources, args.workers)}), flush=True)


if __name__ == "__main__":
    main()
