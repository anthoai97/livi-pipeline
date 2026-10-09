"""Retrieval checks against the local embedded catalog.

Filter tests search with stored product vectors, so they make no API calls, and
compare results with an independent Python check of the same rules. Data edits
run inside the test transaction and are rolled back.

Slot tests run one search per product slot of a living-room request, the way
the room pipeline searches. They embed each slot's search text with Gemini, so they need
GEMINI_API_KEY, and compare each result with a brute-force nearest search over
all stored vectors.

Run: .venv/bin/python -m pytest product-data/tests
"""

from __future__ import annotations

import json
import math
import os
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
load_dotenv(ROOT.parent / ".env")

from search_assets import embed_query, search_assets

# One living-room request: "a cozy cream boucle sofa under $1,500, no rug",
# with a total budget of USD 4,000. The room pipeline plans slots like these in code.
BUDGET_ALLOWANCE = Decimal("4400")  # 110% of the budget
FETCH = 30
KEEP = {"requested": 10, "required": 10, "optional": 6, "decor": 4}
LIVING_ROOM_SLOTS = [
    {"slot": "sofa", "kind": "requested", "text": "cream boucle sofa, cozy, modern",
     "categories": ["sofa", "loveseat", "sectional", "sectional_sofa", "sleeper_sofa", "sofa_with_chaise", "settee"],
     "max_price": Decimal("1500")},
    {"slot": "plant", "kind": "required", "text": "indoor plant, cozy, modern",
     "categories": ["planter", "plant_stand"], "design_only": True},
    {"slot": "coffee table", "kind": "optional", "text": "coffee table, cozy, modern", "categories": ["coffee_table"]},
    {"slot": "side table", "kind": "optional", "text": "side table, cozy, modern", "categories": ["side_table", "end_table"]},
    {"slot": "lamp", "kind": "optional", "text": "lamp, cozy, modern", "categories": ["table_lamp", "floor_lamp"]},
    {"slot": "accent chair", "kind": "optional", "text": "accent chair, cozy, modern",
     "categories": ["accent_chair", "arm_chair", "armchair", "lounge_chair", "swivel_chair"]},
    {"slot": "storage", "kind": "optional", "text": "storage, cozy, modern",
     "categories": ["bookcase", "bookshelf", "shelving_unit", "cabinet", "console_table", "sideboard"]},
    {"slot": "media", "kind": "optional", "text": "TV stand, cozy, modern",
     "categories": ["tv_stand", "media_console", "media_unit", "entertainment_unit"]},
    {"slot": "decor", "kind": "decor", "text": "decorative accent, cozy, modern",
     "categories": ["planter", "sculpture", "floor_mirror", "wall_mirror"]},
]


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
    """The search_assets arguments for one slot."""
    return {
        "limit": FETCH,
        "categories": slot["categories"],
        "max_price": min(slot.get("max_price", BUDGET_ALLOWANCE), BUDGET_ALLOWANCE),
        "known_price": True,
    }


def slot_matches(row: dict, slot: dict) -> bool:
    """The slot filters, written independently of search_assets."""
    limit = slot_filters(slot)["max_price"]
    return (
        row["category"] in slot["categories"]
        and placeable(row)
        and (row["is_purchasable"] is False or (row["price"] is not None and row["currency"] == "USD" and row["price"] <= limit))
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
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        pytest.skip("GEMINI_API_KEY is not set")
    from embed_assets import gemini_client

    client = gemini_client()
    with ThreadPoolExecutor(max_workers=len(LIVING_ROOM_SLOTS)) as pool:
        vectors = pool.map(lambda slot: embed_query(client, slot["text"]), LIVING_ROOM_SLOTS)
    return dict(zip((slot["slot"] for slot in LIVING_ROOM_SLOTS), vectors))


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
def test_slot_keeps_enough_after_the_post_search_check(connection, slot_vectors, slot):
    results = search_assets(connection, slot_vectors[slot["slot"]], **slot_filters(slot))
    assert len(keep_slot(slot, results)) == KEEP[slot["kind"]]


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
