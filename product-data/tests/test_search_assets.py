"""Retrieval checks against the local embedded catalog.

Filter tests search with stored product vectors, so they make no API calls, and
compare results with an independent Python check of the same rules. Data edits
run inside the test transaction and are rolled back.

Slot tests run one search per slot that the pipeline's slot planner
(pipeline/app/shared_stages.py) plans for a living-room request. They embed each
slot's search text with Gemini, so they need GEMINI_API_KEY, and compare each
result with a brute-force nearest search over all stored vectors.

Text query tests embed what a shopper might type, search it within its
categories, and score the top results for the named attributes and duplicates.

Run: .venv/bin/python -m pytest product-data/tests
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent / "pipeline"))
load_dotenv(ROOT.parent / ".env")

from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.shared_stages import FETCH, KEEP, plan_slots
from app.shared_stages import slot_filters as planned_filters
from search_assets import embed_query, search_assets

# One living-room request: "a cozy cream boucle sofa under $1,500, no rug",
# with a total budget of USD 4,000, in a 4.5 x 5.5 m room with one door.
# INTENT holds the fields the slot planner reads from the interpret stage's
# output for this prompt (gemini-3.8-flash, 2026-10-09).
BUDGET = 4000
BUDGET_ALLOWANCE = Decimal("4400")  # 110% of the budget
INTENT = {
    "normalized_prompt": "a cozy cream boucle sofa under $1,500, no rug",
    "requested_items": [{
        "label": "cream boucle sofa", "canonical_category": "sofa", "count": 1, "exact": False, "optional": False,
        "acceptable_substitutes": ["sectional", "loveseat"], "descriptors": ["cozy", "cream", "boucle"],
        "colors": ["cream"], "materials": ["boucle fabric"],
    }],
    "excluded_categories": ["rug"],
    "style_hints": ["cozy"],
    "price_constraints": [{"label": "sofa", "category": "sofa", "relation": "under", "max_price": 1500, "currency": "USD"}],
}
ROOM_AREA = (4.5, 5.5)
ROOM_DOORS = [{"center": [2.25, 0.0], "width": 0.9, "depth": 0.05}]
FILTER_KEYS = ("categories", "known_price", "max_price", "max_width_m", "max_depth_m", "max_height_m", "colors", "styles", "materials")


def planned_slots() -> list[dict]:
    """The pipeline's slots for the request: id, kind, search text, design-only flag, and search filters."""
    intent = coerce_intent_packet(INTENT, room_type="living_room")
    room = build_room_context(
        room_type="living_room", room_area=ROOM_AREA, room_vertices=None, wall_height=2.7,
        room_doors=ROOM_DOORS, room_windows=[], intent=intent,
    )
    return [
        {"slot": slot["id"], "kind": slot["kind"], "text": slot["text"], "design_only": slot["design_only"],
         **{key: value for key, value in planned_filters(slot).items() if key in FILTER_KEYS}}
        for slot in plan_slots(intent, room, BUDGET)
    ]


LIVING_ROOM_SLOTS = planned_slots()

# Text queries searched the way the slot planner searches: the query text with
# its categories as a filter. Each names terms that must all appear in the
# product text, so the cases check how attributes rank within a category.
SOFAS = ["sofa", "sectional", "sectional_sofa", "loveseat", "sleeper_sofa", "sofa_bed", "couch", "couches"]
ARMCHAIRS = ["accent_chair", "arm_chair", "armchair", "lounge_chair", "swivel_chair", "club_chair"]
QUERY_CASES = [
    {"id": "Q1", "query": "cream boucle sofa", "categories": SOFAS, "terms": ["cream", "boucl"]},
    {"id": "Q2", "query": "black leather sofa", "categories": SOFAS, "terms": ["black", "leather"]},
    {"id": "Q3", "query": "green velvet accent chair", "categories": ARMCHAIRS, "terms": ["green", "velvet"]},
    {"id": "Q4", "query": "marble side table", "categories": ["side_table", "end_table"], "terms": ["marble"]},
    {"id": "Q5", "query": "walnut coffee table", "categories": ["coffee_table"], "terms": ["walnut"]},
    {"id": "Q6", "query": "round dining table", "categories": ["dining_table"], "terms": ["round"]},
    {"id": "Q7", "query": "extendable dining table", "categories": ["dining_table"], "terms": ["exten(d|sion)"]},
    {"id": "Q8", "query": "nightstand with drawers", "categories": ["nightstand"], "terms": ["drawer"]},
    {"id": "Q9", "query": "rattan pendant light", "categories": ["pendant", "pendant_light"], "terms": ["rattan|wicker"]},
    {"id": "Q10", "query": "round wall mirror", "categories": ["wall_mirror", "mirror"], "terms": ["round"]},
    {"id": "Q11", "query": "swivel armchair", "categories": ARMCHAIRS, "terms": ["swivel"]},
    {"id": "Q12", "query": "sofa with a chaise", "categories": SOFAS, "terms": ["chaise"]},
    {"id": "Q13", "query": "upholstered dining chairs", "categories": ["dining_chair"], "terms": ["upholster"]},
    {"id": "Q14", "query": "arched floor lamp", "categories": ["floor_lamp"], "terms": [r"\barc"]},
    {"id": "Q15", "query": "queen bed frame", "categories": ["bed_frame", "bed"], "terms": ["queen"]},
    {"id": "Q16", "query": "tv stand with LED lights", "categories": ["tv_stand", "media_console", "media_unit", "tv_unit"], "terms": [r"\bled\b"]},
]
TOP = 10
MIN_HITS = 8


@pytest.fixture
def connection():
    dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not dsn:
        pytest.skip("LOCAL_CONNECTION_STRING is not set")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        if not connection.execute("SELECT to_regclass('pipeline.asset_embeddings_v2') AS table").fetchone()["table"]:
            pytest.skip("run embed_assets.py first")
        yield connection
        connection.rollback()


def stored_vector(connection: psycopg.Connection, where: str) -> tuple[str, list[float]]:
    """Return the asset_id and stored vector of the first embedded product matching where."""
    row = connection.execute(
        f"""
        SELECT asset_id, e.embedding::text AS embedding
        FROM pipeline.asset_embeddings_v2 e
        JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
        WHERE {where}
        ORDER BY asset_id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip(f"no embedded product where {where}")
    return row["asset_id"], json.loads(row["embedding"])


def embedded_records(connection: psycopg.Connection) -> list[dict]:
    return connection.execute(
        "SELECT a.* FROM pipeline.pipeline_assets_v2 a JOIN pipeline.asset_embeddings_v2 USING (asset_id)"
    ).fetchall()


def placeable(row: dict) -> bool:
    return all(row[field] is not None for field in ("model_url", "placement_type", "width_m", "depth_m", "height_m"))


def ids(rows: list[dict]) -> set:
    return {row["asset_id"] for row in rows}


def slot_filters(slot: dict) -> dict:
    """The search_assets arguments for one slot. A slot without max_price (TVs) has no price filters."""
    filters = {"limit": FETCH, **{key: slot[key] for key in FILTER_KEYS if key in slot}}
    if "max_price" in filters:
        filters.update(max_price=min(filters["max_price"], BUDGET_ALLOWANCE), known_price=True)
    return filters


def slot_matches(row: dict, slot: dict) -> bool:
    """The slot filters, written independently of search_assets."""
    limit = slot_filters(slot).get("max_price")
    return (
        row["category"] in slot["categories"]
        and placeable(row)
        and (
            limit is None
            or row["is_purchasable"] is False
            or (row["price"] is not None and row["currency"] == "USD" and row["price"] <= limit)
        )
        and all(row[axis] <= slot[f"max_{axis}"] for axis in ("width_m", "depth_m", "height_m") if f"max_{axis}" in slot)
        and all(value in row[field] for field in ("colors", "styles", "materials") for value in slot.get(field, []))
    )


def keep_slot(slot: dict, rows: list[dict]) -> list[dict]:
    """The check after the search that needs no room geometry, then the keep size."""
    if slot.get("design_only"):
        rows = [row for row in rows if row["is_purchasable"] is False]
    return rows[:KEEP[slot["kind"]]]


def nearest(catalog: list[dict], vector: list[float], slot: dict) -> list[tuple[float, dict]]:
    """Brute-force cosine similarity over every current product that passes the slot filters."""
    norm = math.sqrt(sum(value * value for value in vector))
    scored = [
        (sum(a * b for a, b in zip(vector, row["vector"])) / (norm * row["norm"]), row)
        for row in catalog
        if slot_matches(row, slot)
    ]
    return sorted(scored, key=lambda item: item[0], reverse=True)


def load_catalog(connection: psycopg.Connection) -> list[dict]:
    """Every product with a current vector, plus its stored vector."""
    rows = connection.execute(
        """
        SELECT a.*, e.embedding::text AS vector_text
        FROM pipeline.asset_embeddings_v2 e
        JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
        WHERE e.embedding_text = pipeline.asset_embedding_text_v2(a) AND e.image_url = a.image_url
        """
    ).fetchall()
    for row in rows:
        row["vector"] = json.loads(row.pop("vector_text"))
        row["norm"] = math.sqrt(sum(value * value for value in row["vector"]))
    return rows


def search_slot(dsn: str, slot: dict, vector: list[float]) -> list[dict]:
    """One slot search on its own connection, as concurrent slots need."""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        return search_assets(connection, vector, **slot_filters(slot))


def same_nearest(results: list[dict], expected: list[tuple[float, dict]]) -> bool:
    """True when results are the expected nearest products. Exact ties at the cut may swap."""
    if len(results) != min(FETCH, len(expected)):
        return False
    if not results:
        return True
    cut = expected[len(results) - 1][0]
    allowed = {row["asset_id"] for similarity, row in expected if similarity >= cut - 1e-6}
    return all(row["asset_id"] in allowed for row in results) and all(
        abs(row["similarity"] - similarity) < 1e-4 for row, (similarity, _) in zip(results, expected)
    )


def embed_all(texts: list[str]) -> list[list[float]]:
    """Embed search texts with Gemini, all at the same time."""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        pytest.skip("GEMINI_API_KEY is not set")
    from embed_assets import gemini_client

    client = gemini_client()
    with ThreadPoolExecutor(max_workers=len(texts)) as pool:
        return list(pool.map(lambda text: embed_query(client, text), texts))


def product_text(row: dict) -> str:
    return " ".join([row["title"] or "", row["description"] or "", *row["colors"], *row["materials"], *row["features"]]).lower()


def query_hit(row: dict, case: dict) -> bool:
    return all(re.search(term, product_text(row)) for term in case["terms"])


def score_query(case: dict, rows: list[dict], catalog: list[dict]) -> dict:
    """Top results with every term, against how many such products the catalog has."""
    available = sum(row["category"] in case["categories"] and placeable(row) and query_hit(row, case) for row in catalog)
    hits = sum(query_hit(row, case) for row in rows)
    return {"hits": hits, "needed": min(MIN_HITS, available), "available": available}


def duplicate_key(row: dict):
    """The product page, or the row itself when it has none."""
    return row["product_url"] or row["asset_id"]


def duplicates(rows: list[dict]) -> list[str]:
    """Titles of results that repeat an earlier result's product page."""
    seen, repeated = set(), []
    for row in rows:
        key = duplicate_key(row)
        if key in seen:
            repeated.append(row["title"])
        seen.add(key)
    return repeated


def test_purchase_search_returns_exactly_the_matching_products(connection):
    _, vector = stored_vector(connection, "a.category = 'sofa'")
    expected = ids([
        row for row in embedded_records(connection)
        if row["category"] == "sofa"
        and row["is_purchasable"] is True
        and row["price"] is not None and row["price"] <= 1500 and row["currency"] == "USD"
        and row["width_m"] is not None and row["width_m"] <= 2.5
        and "cream" in row["colors"]
        and placeable(row)
    ])
    results = search_assets(
        connection,
        vector,
        limit=200,
        categories=["sofa"],
        purchase=True,
        max_price=Decimal("1500"),
        max_width_m=2.5,
        colors=["cream"],
    )
    assert expected and len(expected) < 200
    assert ids(results) == expected


def test_room_design_price_limit_keeps_design_only_items(connection):
    _, vector = stored_vector(connection, "a.placement_type = 'surface'")
    expected = ids([
        row for row in embedded_records(connection)
        if row["placement_type"] == "surface"
        and (
            row["is_purchasable"] is False
            or (row["price"] is not None and row["price"] <= 50 and row["currency"] == "USD")
        )
        and placeable(row)
    ])
    results = search_assets(connection, vector, limit=200, placements=["surface"], max_price=50)
    assert expected and len(expected) < 200
    assert ids(results) == expected
    assert any(row["is_purchasable"] is False for row in results)
    assert any(row["is_purchasable"] is True for row in results)


def test_results_are_complete_records_ranked_by_similarity(connection):
    asset_id, vector = stored_vector(connection, "a.category = 'dining_table'")
    results = search_assets(connection, vector, limit=10)
    assert results[0]["asset_id"] == asset_id
    assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-4)
    similarities = [row["similarity"] for row in results]
    assert similarities == sorted(similarities, reverse=True)
    prepared_columns = {
        row["column_name"]
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'pipeline' AND table_name = 'pipeline_assets_v2'
            """
        )
    }
    assert prepared_columns <= set(results[0])


def test_unknown_price_fails_a_price_limit(connection):
    asset_id, vector = stored_vector(connection, "a.is_purchasable AND a.price IS NULL AND a.placement_type IS NOT NULL")
    assert asset_id in ids(search_assets(connection, vector, limit=5, purchase=True))
    assert asset_id not in ids(search_assets(connection, vector, limit=200, purchase=True, max_price=1_000_000))
    assert asset_id not in ids(search_assets(connection, vector, limit=200, max_price=1_000_000))


def test_known_price_keeps_only_budgetable_products(connection):
    _, vector = stored_vector(connection, "a.category = 'side_table'")
    expected = ids([
        row for row in embedded_records(connection)
        if row["category"] == "side_table"
        and (row["is_purchasable"] is False or (row["price"] is not None and row["currency"] == "USD"))
        and placeable(row)
    ])
    results = search_assets(connection, vector, limit=1000, categories=["side_table"], known_price=True)
    assert expected and ids(results) == expected
    unpriced, unpriced_vector = stored_vector(connection, "a.is_purchasable AND a.price IS NULL AND a.placement_type IS NOT NULL")
    assert unpriced in ids(search_assets(connection, unpriced_vector, limit=5))
    assert unpriced not in ids(search_assets(connection, unpriced_vector, limit=1000, known_price=True))


def test_unknown_purchase_status_fails_purchase_search(connection):
    asset_id, vector = stored_vector(connection, "a.is_purchasable IS NULL")
    assert asset_id in ids(search_assets(connection, vector, limit=5, placeable=False))
    assert asset_id not in ids(search_assets(connection, vector, limit=200, purchase=True, placeable=False))
    # Only known design-only items skip a room-design price limit.
    assert asset_id not in ids(search_assets(connection, vector, limit=200, max_price=1_000_000, placeable=False))
    assert asset_id not in ids(search_assets(connection, vector, limit=1000, known_price=True, placeable=False))


def test_unknown_placement_is_excluded_from_room_design(connection):
    asset_id, vector = stored_vector(connection, "a.placement_type IS NULL")
    assert asset_id in ids(search_assets(connection, vector, limit=5, placeable=False))
    assert asset_id not in ids(search_assets(connection, vector, limit=200))


@pytest.mark.parametrize(
    "change",
    [
        "description = description || ' Edited.'",
        "image_url = image_url || '?v=2'",
        "category = NULL",
        "features = features || ARRAY['adjustable shelves']",
        "mount_type = CASE WHEN mount_type = 'wall_secured' THEN 'freestanding' ELSE 'wall_secured' END",
    ],
)
def test_stale_or_ineligible_vectors_are_excluded(connection, change):
    asset_id, vector = stored_vector(connection, "a.category = 'sofa'")
    assert asset_id in ids(search_assets(connection, vector, limit=5))
    connection.execute(f"UPDATE pipeline.pipeline_assets_v2 SET {change} WHERE asset_id = %s", (asset_id,))
    assert asset_id not in ids(search_assets(connection, vector, limit=200))


def test_price_change_keeps_the_vector_current(connection):
    asset_id, vector = stored_vector(connection, "a.category = 'sofa' AND a.price IS NOT NULL")
    connection.execute("UPDATE pipeline.pipeline_assets_v2 SET price = price + 1 WHERE asset_id = %s", (asset_id,))
    assert asset_id in ids(search_assets(connection, vector, limit=5))


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 1001},
        {"currency": "usd"},
        {"max_price": 0},
        {"max_width_m": -1},
    ],
)
def test_invalid_arguments_are_rejected(connection, arguments):
    _, vector = stored_vector(connection, "TRUE")
    with pytest.raises(ValueError):
        search_assets(connection, vector, **arguments)


@pytest.fixture(scope="module")
def slot_vectors():
    vectors = embed_all([slot["text"] for slot in LIVING_ROOM_SLOTS])
    return dict(zip((slot["slot"] for slot in LIVING_ROOM_SLOTS), vectors))


@pytest.fixture(scope="module")
def query_vectors():
    return dict(zip((case["id"] for case in QUERY_CASES), embed_all([case["query"] for case in QUERY_CASES])))


@pytest.fixture(scope="module")
def catalog():
    dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not dsn:
        pytest.skip("LOCAL_CONNECTION_STRING is not set")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        return load_catalog(connection)


@pytest.mark.parametrize("slot", LIVING_ROOM_SLOTS, ids=[slot["slot"] for slot in LIVING_ROOM_SLOTS])
def test_slot_search_returns_the_nearest_matching_products(connection, catalog, slot_vectors, slot):
    vector = slot_vectors[slot["slot"]]
    results = search_assets(connection, vector, **slot_filters(slot))
    assert results
    assert all(slot_matches(row, slot) for row in results)
    assert same_nearest(results, nearest(catalog, vector, slot))


@pytest.mark.parametrize("slot", LIVING_ROOM_SLOTS, ids=[slot["slot"] for slot in LIVING_ROOM_SLOTS])
def test_slot_keeps_enough_after_the_post_search_check(connection, catalog, slot_vectors, slot):
    """Every product that passes the slot filters and the check is kept, up to the keep size."""
    vector = slot_vectors[slot["slot"]]
    results = search_assets(connection, vector, **slot_filters(slot))
    eligible = [row for _, row in nearest(catalog, vector, slot)]
    assert len(keep_slot(slot, results)) == len(keep_slot(slot, eligible))


def test_slot_searches_run_in_parallel(connection, slot_vectors):
    dsn = os.environ["LOCAL_CONNECTION_STRING"].strip()
    with ThreadPoolExecutor(max_workers=len(LIVING_ROOM_SLOTS)) as pool:
        parallel = list(pool.map(lambda slot: search_slot(dsn, slot, slot_vectors[slot["slot"]]), LIVING_ROOM_SLOTS))
    for slot, rows in zip(LIVING_ROOM_SLOTS, parallel):
        sequential = search_assets(connection, slot_vectors[slot["slot"]], **slot_filters(slot))
        assert [row["asset_id"] for row in rows] == [row["asset_id"] for row in sequential]


def test_slot_with_no_match_is_an_empty_gap(connection):
    _, vector = stored_vector(connection, "a.category = 'dresser'")
    gap = {"slot": "dresser", "kind": "requested", "categories": ["dresser"], "max_price": Decimal("5")}
    assert search_assets(connection, vector, **slot_filters(gap)) == []


@pytest.mark.parametrize("case", QUERY_CASES, ids=[case["id"] for case in QUERY_CASES])
def test_text_query_returns_relevant_products(connection, catalog, query_vectors, case):
    rows = search_assets(connection, query_vectors[case["id"]], limit=TOP, categories=case["categories"])
    score = score_query(case, rows, catalog)
    assert score["hits"] >= score["needed"], score


def test_text_query_results_have_no_duplicates(connection, query_vectors):
    repeated = {
        case["query"]: duplicates(search_assets(connection, query_vectors[case["id"]], limit=TOP, categories=case["categories"]))
        for case in QUERY_CASES
    }
    assert not any(repeated.values()), repeated
