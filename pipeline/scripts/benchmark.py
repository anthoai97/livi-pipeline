"""Replay the benchmark requests against a running pipeline service and summarize the run records.

Sends each request of the chosen `--set` in benchmark_requests.json `--runs` times to POST /pipeline on
each `--url` (repeat it to spread runs over several services), up to `--parallel`
at once per service (default 1), reads the run ID from the stream's start event, then reads
pipeline/.data/runs/<run_id>.json, which the service writes when the stream ends.
The service must run on this machine so its run records are readable here.

Run from pipeline/: python scripts/benchmark.py --runs 3 --url http://127.0.0.1:8001 --url http://127.0.0.1:8002

`--set full` (the default) sends every case; `--set quick` sends the medium case of
each room type and the two tight rooms (dining_room_compact_two, studio_tight) once,
for everyday PR checks.

Per run, `pass_of_3` counts the ready designs with zero blocking findings (P0, P1,
critical P2) at the final layout; the summary's `pass` is that count over all designs.
`no_retrieve_s` is `all_ready_s` minus the `retrieve` stage, `full_run_s` the whole run.
`select_calls` counts the selection model calls (stage `select`, hedged
duplicates included) and `failed_turns` the `selection turn <n> failed` notes, over
all designs. The summary's `select_calls_per_design` and `failed_turns_per_design`
are the medians over runs of each count divided by the run's designs (3).

Runs on one service share its process, so `--parallel` above 1 makes run times longer
than sequential ones. Separate services keep them close; compare times only between
benchmarks run the same way.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

SCRIPTS = Path(__file__).resolve().parent
REQUESTS = SCRIPTS / "benchmark_requests.json"
RUNS_DIR = SCRIPTS.parent / ".data" / "runs"
SWITCHES = ("JEV_USES", "PRODUCT_REUSE_RATE")
FAILED_TURN = re.compile(r"selection turn \d+ failed")


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
    plans = {json.dumps(variant["plan"]) for variant in variants if variant["outcome"] == "ready" and variant.get("plan")}
    retrieve_s = sum(stage["elapsed"] or 0 for stage in record["stages"] if stage["stage"] == "retrieve")
    return {
        "room": label,
        "status": record["status"],
        "pass_of_3": sum(variant["outcome"] == "ready" and variant.get("blocking_findings") == [] for variant in variants),
        "no_retrieve_s": None if totals["all_ready_s"] is None else round(totals["all_ready_s"] - retrieve_s, 1),
        "full_run_s": totals["full_run_s"],
        "ready_of_3": sum(variant["outcome"] == "ready" for variant in variants),
        "model_calls": totals["model_calls"],
        "select_calls": sum(call["stage"] == "select" for call in record["model_calls"]),
        "failed_turns": sum(bool(FAILED_TURN.match(note["text"])) for note in record["notes"]),
        "designs": len(variants),
        "cost_usd": totals["cost_usd"],
        "jev_calls": len(record["jev_calls"]) if "jev_calls" in record else None,
        "findings": sum(len(f) if isinstance(f, list) else f for f in findings) if findings else None,
        "plans": len(plans) or None,
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
    parser.add_argument("--runs", type=int, default=3, help="times to send each request (1 with --set quick)")
    parser.add_argument("--set", choices=("quick", "full"), default="full", help="the cases to send")
    parser.add_argument("--url", action="append", help="service base URL (default http://127.0.0.1:8000); repeat to spread runs over services")
    parser.add_argument("--only", help="send only the request with this label, such as studio or living_room_scandi")
    parser.add_argument("--parallel", type=int, default=1, help="requests in flight at once on each service")
    args = parser.parse_args()
    if args.runs < 1 or args.parallel < 1:
        parser.error("--runs and --parallel must be at least 1")
    if args.set == "quick":
        args.runs = 1

    cases = [case for case in json.loads(REQUESTS.read_text())
             if args.set in case["set"] and args.only in (None, case["label"])]
    if not cases:
        parser.error(f"no benchmark request labeled {args.only!r} in the {args.set} set")

    urls = [url.rstrip("/") for url in args.url or ["http://127.0.0.1:8000"]]
    pending: queue.SimpleQueue[tuple[int, dict, int]] = queue.SimpleQueue()
    for index, (case, n) in enumerate((case, n) for case in cases for n in range(1, args.runs + 1)):
        pending.put((index, case, n))

    def work(url: str) -> list[tuple[int, dict]]:
        """Send queued requests to one service, one at a time, until none are left."""
        done = []
        while True:
            try:
                index, case, n = pending.get_nowait()
            except queue.Empty:
                return done
            print(f"{case['label']} run {n} of {args.runs} on {url}...", flush=True)
            done.append((index, summarize(case["label"], read_record(stream_run(client, url, case["request"])))))

    with httpx.Client(timeout=60) as client, ThreadPoolExecutor(len(urls) * args.parallel) as pool:
        rows = [row for _, row in sorted(pair for done in pool.map(work, urls * args.parallel) for pair in done)]

    print()
    print_table(rows)
    seconds = [row["full_run_s"] for row in rows]
    valid = sum(row["ready_of_3"] == 3 for row in rows)
    passed, designs = sum(row["pass_of_3"] for row in rows), sum(row["designs"] for row in rows)
    per_design = {key: statistics.median(row[key] / row["designs"] for row in rows) for key in ("select_calls", "failed_turns")}
    print()
    print(f"pass={passed}/{designs} ({passed / designs:.0%}) runs={len(rows)} max_s={max(seconds):.1f} median_s={statistics.median(seconds):.1f}"
          f" valid={valid}/{len(rows)} ({valid / len(rows):.0%}) under_60s={sum(s < 60 for s in seconds)}/{len(rows)}"
          f" distinct_plans={sum(row['plans'] == 3 for row in rows)}/{len(rows)}"
          f" select_calls_per_design={per_design['select_calls']:.2f} failed_turns_per_design={per_design['failed_turns']:.2f}")


if __name__ == "__main__":
    main()
