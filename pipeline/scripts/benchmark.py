"""Replay the benchmark requests against a running pipeline service and summarize the run records.

Sends each request in benchmark_requests.json `--runs` times, one at a time, to
POST /pipeline, reads the run ID from the stream's start event, then reads
pipeline/.data/runs/<run_id>.json, which the service writes when the stream ends.
The service must run on this machine so its run records are readable here.

Run from pipeline/: python scripts/benchmark.py --runs 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import httpx

SCRIPTS = Path(__file__).resolve().parent
REQUESTS = SCRIPTS / "benchmark_requests.json"
RUNS_DIR = SCRIPTS.parent / ".data" / "runs"
SWITCHES = ("JEV_USES", "PRODUCT_REUSE_RATE")


def stream_run(client: httpx.Client, url: str, request: dict) -> str:
    """POST one request, read the stream to its end, and return the run ID."""
    run_id = None
    with client.stream("POST", f"{url}/pipeline", json=request) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line.removeprefix("data: "))
                run_id = run_id or event.get("run_id")
    if run_id is None:
        raise RuntimeError("the stream had no start event")
    return run_id


def read_record(run_id: str, wait_s: float = 10) -> dict:
    """The run record, waiting briefly because the service writes it as the stream closes."""
    path = RUNS_DIR / f"{run_id}.json"
    deadline = time.monotonic() + wait_s
    while not path.exists():
        if time.monotonic() > deadline:
            raise FileNotFoundError(path)
        time.sleep(0.2)
    return json.loads(path.read_text())


def summarize(label: str, record: dict) -> dict:
    totals, variants = record["totals"], record["variants"]
    switches = record.get("switches") or {}
    findings = [variant["non_blocking_findings"] for variant in variants if "non_blocking_findings" in variant]
    return {
        "room": label,
        "status": record["status"],
        "seconds": totals["full_run_s"],
        "ready_of_3": sum(variant["outcome"] == "ready" for variant in variants),
        "model_calls": totals["model_calls"],
        "cost_usd": totals["cost_usd"],
        "jev_calls": len(record["jev_calls"]) if "jev_calls" in record else None,
        "findings": sum(len(f) if isinstance(f, list) else f for f in findings) if findings else None,
        **{key: record.get(key, switches.get(key)) for key in SWITCHES},
        "run_id": record["run_id"],
    }


def print_table(rows: list[dict]) -> None:
    columns = list(rows[0])
    cells = [[("-" if row[c] is None else str(row[c])) for c in columns] for row in rows]
    widths = [max(len(c), *(len(line[i]) for line in cells)) for i, c in enumerate(columns)]
    for line in [columns, *cells]:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the benchmark requests against a running pipeline service")
    parser.add_argument("--runs", type=int, default=3, help="times to send each request")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="service base URL")
    parser.add_argument("--only", help="send only the request for this room type, such as studio")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    cases = [case for case in json.loads(REQUESTS.read_text()) if args.only in (None, case["label"])]
    if not cases:
        parser.error(f"no benchmark request for room type {args.only!r}")

    rows = []
    with httpx.Client(timeout=60) as client:
        for case in cases:
            for n in range(1, args.runs + 1):
                print(f"{case['label']} run {n} of {args.runs}...", flush=True)
                rows.append(summarize(case["label"], read_record(stream_run(client, args.url.rstrip("/"), case["request"]))))

    print()
    print_table(rows)
    seconds = [row["seconds"] for row in rows]
    valid = sum(row["ready_of_3"] == 3 for row in rows)
    print()
    print(f"runs={len(rows)} max_s={max(seconds):.1f} median_s={statistics.median(seconds):.1f}"
          f" valid={valid}/{len(rows)} ({valid / len(rows):.0%}) under_60s={sum(s < 60 for s in seconds)}/{len(rows)}")


if __name__ == "__main__":
    main()
