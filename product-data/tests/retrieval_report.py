"""Write a product retrieval test report to reports/.

Runs the pytest suite, then repeats each case in
docs/data/product-embedding-v2-retrieval-tests.md with the same inputs: basic
search, the slot searches for one living-room request, text queries, and speed.
Records each case's input, expected result, actual result, and verdict. Data
edits are rolled back. Needs LOCAL_CONNECTION_STRING and GEMINI_API_KEY.

Run: .venv/bin/python product-data/tests/retrieval_report.py
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]
sys.path.insert(0, str(TESTS))

import test_search_assets as cases
from embed_assets import DIMENSIONS, MODEL, gemini_client
from search_assets import embed_query, search_assets

# Retrieval is one stage of a 60-second request; keep its database time below 1% of that.
POOL_LIMIT_MS = 500
STALE_CHANGES = {
    "S1": ("Text changed", "description = description || ' Edited.'"),
    "S2": ("Image URL changed", "image_url = image_url || '?v=2'"),
    "S3": ("Became ineligible", "category = NULL"),
    "S5": ("Features changed", "features = features || ARRAY['adjustable shelves']"),
    "S6": ("Mount changed", "mount_type = CASE WHEN mount_type = 'wall_secured' THEN 'freestanding' ELSE 'wall_secured' END"),
}


def run_pytest() -> tuple[dict[str, str], str]:
    """Return each test's outcome and the summary line."""
    with tempfile.TemporaryDirectory() as directory:
        junit = Path(directory) / "junit.xml"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(TESTS), "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        outcomes = {}
        for case in ElementTree.parse(junit).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                outcomes[case.get("name")] = "failed"
            elif case.find("skipped") is not None:
                outcomes[case.get("name")] = "skipped"
            else:
                outcomes[case.get("name")] = "passed"
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return outcomes, lines[-1].strip("= ") if lines else "no pytest output"


def test_outcome(outcomes: dict[str, str], name: str) -> str:
    """Combine every parametrized run of a test, or match one exact run."""
    found = [value for key, value in outcomes.items() if key == name or key.startswith(name + "[")]
    if not found:
        return f"`{name}`: not run"
    worst = "failed" if "failed" in found else "skipped" if "skipped" in found else "passed"
    runs = f" ({len(found)} runs)" if len(found) > 1 else ""
    return f"`{name}`{runs}: {worst}"


def describe(row: dict) -> str:
    return f'"{row["title"]}" ({row["category"]}, `{row["asset_id"]}`)'


def product(connection: psycopg.Connection, asset_id) -> dict:
    return connection.execute("SELECT * FROM pipeline.pipeline_assets_v2 WHERE asset_id = %s", (asset_id,)).fetchone()


def filters_text(filters: dict) -> str:
    return ", ".join(
        f"`{key}={value if isinstance(value, Decimal) else repr(value)}`" for key, value in filters.items()
    ) or "none"


def rank_of(asset_id, rows: list[dict]) -> int | None:
    return next((index for index, row in enumerate(rows, start=1) if row["asset_id"] == asset_id), None)


def presence(connection, asset_id, vector, filters: dict) -> tuple[bool, str]:
    rows = search_assets(connection, vector, **filters)
    rank = rank_of(asset_id, rows)
    text = f"rank {rank} of {len(rows)}" if rank else f"absent from {len(rows)} results"
    return rank is not None, f"{filters_text(filters)}: {text}"


def set_case(case_id, title, connection, where, filters, keep, test) -> dict:
    """Compare search results with an independent Python check over all embedded products."""
    asset_id, vector = cases.stored_vector(connection, where)
    expected = cases.ids([row for row in cases.embedded_records(connection) if keep(row)])
    rows = search_assets(connection, vector, **filters)
    missing, unexpected = expected - cases.ids(rows), cases.ids(rows) - expected
    return {
        "id": case_id,
        "title": title,
        "input": f"Query vector: stored vector of {describe(product(connection, asset_id))}. Filters: {filters_text(filters)}.",
        "actual": f"{len(rows)} returned; {len(missing)} missing; {len(unexpected)} unexpected.",
        "passed": bool(expected) and len(expected) < filters["limit"] and not missing and not unexpected,
        "test": test,
        "rows": rows,
        "expected_count": len(expected),
    }


def collect(connection: psycopg.Connection, outcomes: dict[str, str]) -> list[dict]:
    results = []

    def add(builder):
        try:
            results.append(builder())
        except pytest.skip.Exception as exc:
            results.append({"id": builder.__name__.upper(), "title": "Skipped", "input": "-", "expected": "-",
                            "actual": str(exc), "passed": None, "test": "-"})

    def r1():
        case = set_case(
            "R1", "Purchase search returns exactly the matching products", connection, "a.category = 'sofa'",
            {"limit": 200, "categories": ["sofa"], "purchase": True, "max_price": Decimal("1500"),
             "max_width_m": 2.5, "colors": ["cream"]},
            lambda row: row["category"] == "sofa" and row["is_purchasable"] is True
            and row["price"] is not None and row["price"] <= 1500 and row["currency"] == "USD"
            and row["width_m"] is not None and row["width_m"] <= 2.5 and "cream" in row["colors"]
            and cases.placeable(row),
            test_outcome(outcomes, "test_purchase_search_returns_exactly_the_matching_products"),
        )
        case["expected"] = (
            f"Exactly the {case['expected_count']} embedded products that are sofas, purchasable, priced at most"
            " 1,500 USD, at most 2.5 m wide, cream, and placeable."
        )
        return case

    def r2():
        case = set_case(
            "R2", "Room-design price limit keeps design-only items", connection, "a.placement_type = 'surface'",
            {"limit": 200, "placements": ["surface"], "max_price": 50},
            lambda row: row["placement_type"] == "surface" and cases.placeable(row) and (
                row["is_purchasable"] is False
                or (row["price"] is not None and row["price"] <= 50 and row["currency"] == "USD")
            ),
            test_outcome(outcomes, "test_room_design_price_limit_keeps_design_only_items"),
        )
        rows = case["rows"]
        design_only = sum(row["is_purchasable"] is False for row in rows)
        priced = [row["price"] for row in rows if row["is_purchasable"]]
        case["expected"] = (
            f"Exactly the {case['expected_count']} placeable surface products that are design-only, or purchasable"
            " at most 50 USD. Both kinds appear."
        )
        case["actual"] += (
            f" {design_only} design-only, {len(priced)} purchasable"
            f" (highest price {max(priced) if priced else '-'} USD)."
        )
        case["passed"] = case["passed"] and design_only > 0 and len(priced) > 0
        return case

    def presence_case(case_id, title, where, checks, expected, test):
        asset_id, vector = cases.stored_vector(connection, where)
        observed = [presence(connection, asset_id, vector, filters) for filters, _ in checks]
        return {
            "id": case_id,
            "title": title,
            "input": f"Query vector: stored vector of {describe(product(connection, asset_id))}. {len(checks)} searches, see Actual.",
            "expected": expected,
            "actual": "<br>".join(text for _, text in observed),
            "passed": all(found == want for (found, _), (_, want) in zip(observed, checks)),
            "test": test_outcome(outcomes, test),
        }

    def r3():
        return presence_case(
            "R3", "Unknown price fails a price limit",
            "a.is_purchasable AND a.price IS NULL AND a.placement_type IS NOT NULL",
            [({"limit": 5, "purchase": True}, True),
             ({"limit": 200, "purchase": True, "max_price": 1_000_000}, False),
             ({"limit": 200, "max_price": 1_000_000}, False)],
            "A purchasable product with no price is found without a price limit, and absent under any price"
            " limit in purchase and room-design searches.",
            "test_unknown_price_fails_a_price_limit",
        )

    def r4():
        return presence_case(
            "R4", "Unknown purchase status fails purchase search", "a.is_purchasable IS NULL",
            [({"limit": 5, "placeable": False}, True),
             ({"limit": 200, "purchase": True, "placeable": False}, False),
             ({"limit": 200, "max_price": 1_000_000, "placeable": False}, False)],
            "A product with unknown purchase status is found without filters, and absent from purchase search"
            " and under a room-design price limit.",
            "test_unknown_purchase_status_fails_purchase_search",
        )

    def r5():
        return presence_case(
            "R5", "Unknown placement is excluded from room design", "a.placement_type IS NULL",
            [({"limit": 5, "placeable": False}, True), ({"limit": 200}, False)],
            "A product with no placement is found when unplaceable products are allowed, and absent by default.",
            "test_unknown_placement_is_excluded_from_room_design",
        )

    def r9():
        case = set_case(
            "R9", "Known-price pool filter keeps only budgetable products", connection, "a.category = 'side_table'",
            {"limit": 1000, "categories": ["side_table"], "known_price": True},
            lambda row: row["category"] == "side_table" and cases.placeable(row) and (
                row["is_purchasable"] is False or (row["price"] is not None and row["currency"] == "USD")
            ),
            test_outcome(outcomes, "test_known_price_keeps_only_budgetable_products"),
        )
        dropped = connection.execute(
            "SELECT count(*) AS n FROM pipeline.pipeline_assets_v2"
            " WHERE category = 'side_table' AND is_purchasable AND price IS NULL"
        ).fetchone()["n"]
        case["expected"] = (
            f"Exactly the {case['expected_count']} placeable side tables that are design-only or priced in USD."
            f" The {dropped} purchasable side tables with no price are left out."
        )
        case["actual"] += f" Unpriced purchasable products returned: {sum(row['is_purchasable'] is True and row['price'] is None for row in case['rows'])}."
        return case

    def stale(case_id):
        title, change = STALE_CHANGES[case_id]

        def build():
            asset_id, vector = cases.stored_vector(connection, "a.category = 'sofa'")
            before_found, before = presence(connection, asset_id, vector, {"limit": 5})
            connection.execute(f"UPDATE pipeline.pipeline_assets_v2 SET {change} WHERE asset_id = %s", (asset_id,))
            after_found, after = presence(connection, asset_id, vector, {"limit": 200})
            connection.rollback()
            return {
                "id": case_id,
                "title": title,
                "input": f"Query vector: stored vector of {describe(product(connection, asset_id))}."
                         f" Change in a rolled-back transaction: `{change}`.",
                "expected": "Found before the change; absent after it until the job rebuilds the vector.",
                "actual": f"Before: {before}.<br>After: {after}.",
                "passed": before_found and not after_found,
                "test": test_outcome(outcomes, f"test_stale_or_ineligible_vectors_are_excluded[{change}]"),
            }

        build.__name__ = case_id.lower()
        return build

    def s4():
        asset_id, vector = cases.stored_vector(connection, "a.category = 'sofa' AND a.price IS NOT NULL")
        row = product(connection, asset_id)
        connection.execute("UPDATE pipeline.pipeline_assets_v2 SET price = price + 1 WHERE asset_id = %s", (asset_id,))
        found, after = presence(connection, asset_id, vector, {"limit": 5})
        connection.rollback()
        return {
            "id": "S4",
            "title": "Price change keeps the vector current",
            "input": f"Query vector: stored vector of {describe(row)}. Change in a rolled-back transaction:"
                     f" price {row['price']} to {row['price'] + 1} {row['currency']}.",
            "expected": "Still found after the change. No rebuild is needed.",
            "actual": f"After: {after}.",
            "passed": found,
            "test": test_outcome(outcomes, "test_price_change_keeps_the_vector_current"),
        }

    def a1():
        asset_id, vector = cases.stored_vector(connection, "a.category = 'dining_table'")
        rows = search_assets(connection, vector, limit=10)
        similarities = [round(float(row["similarity"]), 4) for row in rows]
        columns = {
            row["column_name"]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'pipeline' AND table_name = 'pipeline_assets_v2'"
            )
        }
        missing = sorted(columns - set(rows[0]))
        test = test_outcome(outcomes, "test_results_are_complete_records_ranked_by_similarity")
        return [
            {
                "id": "A1",
                "title": "Results are ranked by similarity",
                "input": f"Query vector: stored vector of {describe(product(connection, asset_id))}. Filters: `limit=10`.",
                "expected": "The query product is first with similarity 1.0; similarities never increase down the list.",
                "actual": f"First result is the query product: {rows[0]['asset_id'] == asset_id}."
                          f" Similarities: {', '.join(map(str, similarities))}.",
                "passed": rows[0]["asset_id"] == asset_id and abs(similarities[0] - 1) < 1e-4
                and similarities == sorted(similarities, reverse=True),
                "test": test,
            },
            {
                "id": "A2",
                "title": "Results are complete prepared records",
                "input": "The first result of A1.",
                "expected": f"All {len(columns)} columns of `pipeline.pipeline_assets_v2`, plus `similarity`.",
                "actual": f"Missing columns: {', '.join(missing) or 'none'}."
                          f" Extra columns: {', '.join(sorted(set(rows[0]) - columns)) or 'none'}.",
                "passed": not missing,
                "test": test,
            },
        ]

    def a3():
        _, vector = cases.stored_vector(connection, "TRUE")
        observed = []
        for arguments in ({"limit": 0}, {"limit": 1001}, {"currency": "usd"}, {"max_price": 0}, {"max_width_m": -1}):
            try:
                search_assets(connection, vector, **arguments)
                observed.append((False, f"{filters_text(arguments)}: no error"))
            except ValueError as exc:
                observed.append((True, f"{filters_text(arguments)}: ValueError \"{exc}\""))
        return {
            "id": "A3",
            "title": "Invalid arguments are rejected",
            "input": "Five calls, each with one invalid argument. See Actual.",
            "expected": "Each call raises `ValueError` before querying.",
            "actual": "<br>".join(text for _, text in observed),
            "passed": all(ok for ok, _ in observed),
            "test": test_outcome(outcomes, "test_invalid_arguments_are_rejected"),
        }

    for builder in (r1, r2, r3, r4, r5, r9, stale("S1"), stale("S2"), stale("S3"), s4, stale("S5"), stale("S6")):
        add(builder)
    results.extend(a1())
    add(a3)

    return results


def table(rows: list[dict]) -> list[str]:
    lines = [
        "| # | Title | Category | Price | Purchasable | Placement | Size W x D x H (m) | Colors | Similarity |",
        "|---:|---|---|---|---|---|---|---|---:|",
    ]
    for index, row in enumerate(rows, start=1):
        price = f"{row['price']} {row['currency']}" if row["price"] is not None else "unknown"
        size = " x ".join("?" if row[axis] is None else f"{row[axis]:.2f}" for axis in ("width_m", "depth_m", "height_m"))
        lines.append(
            f"| {index} | {row['title']} | {row['category']} | {price} | {row['is_purchasable']}"
            f" | {row['placement_type']} | {size} | {', '.join(row['colors'])} | {row['similarity']:.4f} |"
        )
    return lines


def slot_cases(dsn: str, outcomes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Run the living-room slot searches defined in test_search_assets, and time them."""
    slots = cases.LIVING_ROOM_SLOTS
    client = gemini_client()

    def timed_embed(slot):
        started = time.perf_counter()
        vector = embed_query(client, slot["text"])
        return vector, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        embedded = list(pool.map(timed_embed, slots))
    embed_wall_ms = (time.perf_counter() - started) * 1000
    vectors = [vector for vector, _ in embedded]

    results, found, lookups = [], [], []
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        catalog = cases.load_catalog(connection)
        for index, (slot, vector) in enumerate(zip(slots, vectors), start=1):
            filters = cases.slot_filters(slot)
            started = time.perf_counter()
            rows = search_assets(connection, vector, **filters)
            lookups.append((time.perf_counter() - started) * 1000)
            found.append(rows)
            expected = cases.nearest(catalog, vector, slot)
            same = cases.same_nearest(rows, expected)
            kept = cases.keep_slot(slot, rows)
            keep = cases.KEEP[slot["kind"]]
            after = " After the search, keep only design-only products." if slot.get("design_only") else ""
            results.append({
                "id": f"L{index}",
                "title": f"{slot['kind'].capitalize()} slot: {slot['slot']}",
                "input": f"Search text: \"{slot['text']}\". Filters: {filters_text(filters)}.{after}",
                "expected": f"The {min(cases.FETCH, len(expected))} products nearest to the search text, out of the"
                            f" {len(expected)} that pass the filters. A brute-force similarity check over all"
                            f" {len(catalog)} products finds them. Target pool: {keep} kept products.",
                "actual": f"{len(rows)} returned. Same products and order as the brute-force check:"
                          f" {'yes' if same else 'no'}. {len(kept)} kept."
                          + (f" Catalog coverage is {keep - len(kept)} below the target." if same and len(kept) < keep else ""),
                "passed": bool(rows) and same and len(kept) == keep,
                "test": "<br>".join(
                    test_outcome(outcomes, f"{name}[{slot['slot']}]")
                    for name in ("test_slot_search_returns_the_nearest_matching_products",
                                 "test_slot_keeps_enough_after_the_post_search_check")
                ),
                "rows": kept,
            })

        _, dresser = cases.stored_vector(connection, "a.category = 'dresser'")
        gap_slot = {"slot": "dresser", "kind": "requested", "categories": ["dresser"], "max_price": Decimal("5")}
        gap_filters = cases.slot_filters(gap_slot)
        try:
            gap = search_assets(connection, dresser, **gap_filters)
            gap_actual, gap_passed = f"{len(gap)} products returned, no error.", gap == []
        except Exception as exc:
            gap_actual, gap_passed = f"Error: {exc}", False

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        parallel = list(pool.map(lambda pair: cases.search_slot(dsn, *pair), zip(slots, vectors)))
    parallel_ms = (time.perf_counter() - started) * 1000
    same_parallel = all(
        [row["asset_id"] for row in a] == [row["asset_id"] for row in b] for a, b in zip(parallel, found)
    )
    pool_size = sum(len(case["rows"]) for case in results)
    results += [
        {
            "id": f"L{len(slots) + 1}",
            "title": "All slots searched at the same time",
            "input": f"The {len(slots)} slot searches above, each on its own database connection, all at once.",
            "expected": "Each slot returns the same products, in the same order, as when searched one at a time.",
            "actual": f"Same results: {'yes' if same_parallel else 'no'}. Pool after keeping: {pool_size} products.",
            "passed": same_parallel,
            "test": test_outcome(outcomes, "test_slot_searches_run_in_parallel"),
        },
        {
            "id": f"L{len(slots) + 2}",
            "title": "A slot with no match is an empty gap",
            "input": f"A requested dresser slot. Filters: {filters_text(gap_filters)}. No dresser costs USD 5 or less.",
            "expected": "An empty list and no error, so the run can report the slot as a gap and continue.",
            "actual": gap_actual,
            "passed": gap_passed,
            "test": test_outcome(outcomes, "test_slot_with_no_match_is_an_empty_gap"),
        },
    ]
    speed = [
        {
            "id": "T1",
            "title": "Slot search time",
            "input": f"Database time of the {len(slots)} slot searches: one at a time, and all at once.",
            "expected": f"All slots at once in under {POOL_LIMIT_MS} ms.",
            "actual": f"All at once: {parallel_ms:.0f} ms. One at a time: median {statistics.median(lookups):.0f} ms"
                      f" per slot, range {min(lookups):.0f} to {max(lookups):.0f} ms.",
            "passed": parallel_ms < POOL_LIMIT_MS,
            "test": "Report only",
        },
        {
            "id": "T2",
            "title": "Search text embedding time",
            "input": f"Gemini time to embed the {len(slots)} slot search texts, all at once.",
            "expected": "Recorded separately from search time. No limit.",
            "actual": f"All at once: {embed_wall_ms / 1000:.2f} s. Each text: median"
                      f" {statistics.median(ms for _, ms in embedded) / 1000:.2f} s.",
            "passed": True,
            "test": "Report only",
        },
    ]
    return results, speed


def query_cases(dsn: str, outcomes: dict[str, str]) -> list[dict]:
    """Run the text queries defined in test_search_assets and score their top results."""
    vectors = cases.embed_all([case["query"] for case in cases.QUERY_CASES])
    results, repeated = [], {}
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        catalog = cases.load_catalog(connection)
        for case, vector in zip(cases.QUERY_CASES, vectors):
            filters = {"limit": cases.TOP, "categories": case["categories"]}
            rows = search_assets(connection, vector, **filters)
            score = cases.score_query(case, rows, catalog)
            repeated[case["query"]] = cases.duplicates(rows)
            results.append({
                "id": case["id"],
                "title": f"\"{case['query']}\"",
                "input": f"Query text: \"{case['query']}\". Filters: {filters_text(filters)} (current, placeable products).",
                "expected": f"At least {score['needed']} of the top {cases.TOP} match `{'`, `'.join(case['terms'])}`"
                            f" in the product text ({score['available']} such products in these categories).",
                "actual": f"{len(rows)} returned. {score['hits']} match the terms.",
                "passed": score["hits"] >= score["needed"],
                "test": test_outcome(outcomes, f"test_text_query_returns_relevant_products[{case['id']}]"),
                "rows": rows,
            })
    groups = [count for count in Counter(map(cases.duplicate_key, catalog)).values() if count > 1]
    found = {query: titles for query, titles in repeated.items() if titles}
    results.append({
        "id": f"Q{len(cases.QUERY_CASES) + 1}",
        "title": "Top results have no duplicates",
        "input": f"The top {cases.TOP} results of the queries above.",
        "expected": "No product page appears twice.",
        "actual": ("<br>".join(f"\"{query}\": {', '.join(titles)}" for query, titles in found.items()) or "No duplicates.")
                  + f"<br>Catalog: {len(groups)} such groups, {sum(groups)} products.",
        "passed": not found,
        "test": test_outcome(outcomes, "test_text_query_results_have_no_duplicates"),
    })
    return results


def verdict(passed: bool | None) -> str:
    return "SKIPPED" if passed is None else "PASS" if passed else "FAIL"


def counts_text(cases_: list[dict]) -> str:
    counts = {name: sum(verdict(case["passed"]) == name for case in cases_) for name in ("PASS", "FAIL", "SKIPPED")}
    return f"{counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIPPED']} skipped"


def case_lines(case: dict) -> list[str]:
    lines = [
        f"### {case['id']}. {case['title']}",
        "",
        "| | |",
        "|---|---|",
        f"| Input | {case['input']} |",
        f"| Expected | {case['expected']} |",
        f"| Actual | {case['actual']} |",
        f"| Result | **{verdict(case['passed'])}** |",
        f"| Pytest | {case['test']} |",
        "",
    ]
    if case.get("rows"):
        shown = case["rows"] if len(case["rows"]) <= 20 else case["rows"][:10]
        if shown is not case["rows"]:
            lines += [f"First 10 of {len(case['rows'])} results:", ""]
        lines += table(shown) + [""]
    return lines


def index_lines(cases_: list[dict]) -> list[str]:
    return [
        "| ID | Case | Result |",
        "|---|---|---|",
        *[f"| {case['id']} | {case['title']} | {verdict(case['passed'])} |" for case in cases_],
        "",
    ]


def render(
    basic: list[dict], slots: list[dict], queries: list[dict], speed: list[dict], catalog: dict, pytest_summary: str
) -> str:
    lines = [
        f"# Product retrieval test report: {date.today().isoformat()}",
        "",
        "Cases and rules: [product retrieval test cases](../docs/data/product-embedding-v2-retrieval-tests.md).",
        "Generated by `.venv/bin/python product-data/tests/retrieval_report.py`.",
        "",
        "## Summary",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Catalog | {catalog['products']} embedded products; {catalog['current']} current |",
        f"| Model | `{MODEL}`, {DIMENSIONS} dimensions |",
        f"| Pytest | {pytest_summary} |",
        f"| Slot search | {counts_text(slots)} |",
        f"| Text query | {counts_text(queries)} |",
        f"| Basic search | {counts_text(basic)} |",
        f"| Speed | {counts_text(speed)} |",
        "",
        "## Findings",
        "",
        "<!-- Replace this comment with findings from reviewing the results below. -->",
        "",
        "## Slot search",
        "",
        "These cases run one search per product slot, the way the room pipeline searches, for one living-room"
        " request: \"a cozy cream boucle sofa under USD 1,500, no rug\", with a total budget of USD 4,000.",
        "",
        f"The tests use the runtime planner's {len(cases.LIVING_ROOM_SLOTS)} slots and filters for this request."
        " There is no rug slot, because the user excluded rugs. Each slot"
        f" searches its own text with these filters: its categories, requested attributes, a price no higher than the item's limit and the"
        f" USD {cases.BUDGET_ALLOWANCE:,} allowance (110% of the budget), placeable (3D model, placement, and all"
        f" dimensions), and a known price. Each fetches up to {cases.FETCH} products. Target pools are"
        f" {cases.KEEP['requested']} for requested and required slots, {cases.KEEP['optional']} for optional slots, and {cases.KEEP['decor']} for decor.",
        "",
        "Pytest verifies that search returns and keeps the available eligible products. This report also checks"
        " the target pool size. A correct search can therefore pass pytest while this report flags a catalog coverage gap.",
        "",
        "The checks after the search that need room geometry, such as fitting the floor and minimum sizes, belong to"
        " the pipeline and are not part of this test.",
        "",
        *index_lines(slots),
    ]
    for case in slots:
        lines += case_lines(case)
    lines += [
        "## Text query",
        "",
        "These cases embed what a shopper might type and search it within its categories, the way the slot planner"
        f" searches. Each checks that at least {cases.MIN_HITS} of the top {cases.TOP} results match the named"
        " attributes (all of them when the catalog has fewer), and that no product page appears twice.",
        "",
        *index_lines(queries),
    ]
    for case in queries:
        lines += case_lines(case)
    lines += [
        "## Basic search",
        "",
        "These cases search with stored product vectors, so they make no API calls. Each compares the results with"
        " a separate Python check of the same rules.",
        "",
        *index_lines(basic),
    ]
    for case in basic:
        lines += case_lines(case)
    lines += ["## Speed", "", *index_lines(speed)]
    for case in speed:
        lines += case_lines(case)
    lines += [
        "## Not covered by this report",
        "",
        "- R6 (unknown dimension) and R8 (currency mismatch): the local catalog has no matching products.",
        "- J1 to J8: embedding job behavior. Check these by running `embed_assets.py`.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a product retrieval test report")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / "reports" / f"{date.today().isoformat()}-product-retrieval-tests.md",
    )
    args = parser.parse_args()
    dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not dsn or not os.environ.get("GEMINI_API_KEY", "").strip():
        raise SystemExit("LOCAL_CONNECTION_STRING and GEMINI_API_KEY are required")

    outcomes, pytest_summary = run_pytest()
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        catalog = connection.execute(
            """
            SELECT count(*) AS products,
                   count(*) FILTER (
                       WHERE e.embedding_text = pipeline.asset_embedding_text_v2(a) AND e.image_url = a.image_url
                   ) AS current
            FROM pipeline.asset_embeddings_v2 e
            JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
            """
        ).fetchone()
        basic = collect(connection, outcomes)
        connection.rollback()
    slots, speed = slot_cases(dsn, outcomes)
    queries = query_cases(dsn, outcomes)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(basic, slots, queries, speed, catalog, pytest_summary), encoding="utf-8")
    failed = [case["id"] for case in basic + slots + queries + speed if case["passed"] is False]
    print(f"report={args.output} pytest=\"{pytest_summary}\" failed_cases={','.join(failed) or 'none'}")


if __name__ == "__main__":
    main()
