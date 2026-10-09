"""Search prepared products through pipeline.asset_embeddings_v2.

Embeds a semantic query with the product embedding model, applies exact filters
to the prepared fields, and ranks by cosine distance. Returns complete prepared
records with similarity scores. Unknown values never satisfy a filter. Without
purchase=True, design-only items pass the price filters because they add nothing
to the purchase total. Vectors whose text or image URL no longer match the
prepared record are excluded. With per_category=True, the limit applies to each
category separately.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from google import genai
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from embed_assets import MODEL, embed, gemini_client

# Retrieval query format for text queries against gemini-embedding-2 documents.
QUERY_FORMAT = "task: search result | query: {}"


def embed_query(client: genai.Client, query: str) -> list[float]:
    text = " ".join(query.split())
    if not text:
        raise ValueError("query is empty")
    return embed(client, [QUERY_FORMAT.format(text)])


def search_assets(
    connection: psycopg.Connection,
    vector: list[float],
    *,
    limit: int = 20,
    categories: list[str] | None = None,
    purchase: bool = False,
    max_price: Decimal | float | None = None,
    currency: str = "USD",
    max_width_m: float | None = None,
    max_depth_m: float | None = None,
    max_height_m: float | None = None,
    placements: list[str] | None = None,
    colors: list[str] | None = None,
    styles: list[str] | None = None,
    materials: list[str] | None = None,
    placeable: bool = True,
    known_price: bool = False,
    per_category: bool = False,
    design_only: bool = False,
) -> list[dict]:
    """Return the nearest prepared records that pass every exact filter.

    placeable requires a 3D model, known placement, and all three dimensions.
    known_price keeps only design-only items and items priced in currency, so
    every result can be counted against a budget. per_category returns up to limit
    nearest records from each category, still ordered by distance overall.
    design_only keeps only non-purchasable records.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter code such as USD")
    for name, value in (
        ("max_price", max_price),
        ("max_width_m", max_width_m),
        ("max_depth_m", max_depth_m),
        ("max_height_m", max_height_m),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive")

    params = {
        "vector": vector,
        "limit": limit,
        "model": MODEL,
        "categories": categories,
        "max_price": max_price,
        "currency": currency,
        "width_m": max_width_m,
        "depth_m": max_depth_m,
        "height_m": max_height_m,
        "placements": placements,
        "colors": colors,
        "styles": styles,
        "materials": materials,
    }
    conditions = [
        "e.model = %(model)s",
        "e.embedding_text = pipeline.asset_embedding_text_v2(a)",
        "e.image_url = a.image_url",
    ]
    if categories:
        conditions.append("a.category = ANY(%(categories)s)")
    if purchase:
        conditions.append("a.is_purchasable")
    if design_only:
        conditions.append("a.is_purchasable = false")
    if max_price is not None:
        price = "(a.price <= %(max_price)s AND a.currency = %(currency)s)"
        conditions.append(price if purchase else f"({price} OR a.is_purchasable = false)")
    if known_price:
        conditions.append("((a.price IS NOT NULL AND a.currency = %(currency)s) OR a.is_purchasable = false)")
    for axis in ("width_m", "depth_m", "height_m"):
        if params[axis] is not None:
            conditions.append(f"a.{axis} <= %({axis})s")
    if placements:
        conditions.append("a.placement_type = ANY(%(placements)s)")
    for field in ("colors", "styles", "materials"):
        if params[field]:
            conditions.append(f"a.{field} @> %({field})s::text[]")
    if placeable:
        conditions.append(
            "a.model_url IS NOT NULL AND a.placement_type IS NOT NULL"
            " AND a.width_m IS NOT NULL AND a.depth_m IS NOT NULL AND a.height_m IS NOT NULL"
        )

    if not per_category:
        return connection.execute(
            f"""
            SELECT a.*, 1 - (e.embedding <=> %(vector)s::vector) AS similarity
            FROM pipeline.asset_embeddings_v2 e
            JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
            WHERE {" AND ".join(conditions)}
            ORDER BY e.embedding <=> %(vector)s::vector
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
    rows = connection.execute(
        f"""
        SELECT * FROM (
            SELECT a.*, 1 - (e.embedding <=> %(vector)s::vector) AS similarity,
                row_number() OVER (PARTITION BY a.category ORDER BY e.embedding <=> %(vector)s::vector) AS category_rank
            FROM pipeline.asset_embeddings_v2 e
            JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
            WHERE {" AND ".join(conditions)}
        ) ranked
        WHERE category_rank <= %(limit)s
        ORDER BY similarity DESC
        """,
        params,
    ).fetchall()
    for row in rows:
        del row["category_rank"]
    return rows


def main() -> None:
    load_dotenv(ROOT.parent / ".env")
    parser = argparse.ArgumentParser(description="Search prepared products by meaning and exact filters")
    parser.add_argument("query", help='semantic query, such as "modern cream sofa"')
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--category", action="append", help="repeat for several categories")
    parser.add_argument("--purchase", action="store_true", help="only purchasable products")
    parser.add_argument("--max-price", type=Decimal)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--max-width", type=float, help="metres")
    parser.add_argument("--max-depth", type=float, help="metres")
    parser.add_argument("--max-height", type=float, help="metres")
    parser.add_argument("--placement", action="append", choices=["floor", "surface", "wall", "ceiling"])
    parser.add_argument("--color", action="append")
    parser.add_argument("--style", action="append")
    parser.add_argument("--material", action="append")
    parser.add_argument("--include-unplaceable", action="store_true", help="allow products that cannot be placed")
    parser.add_argument("--known-price", action="store_true", help="only design-only or priced products")
    parser.add_argument("--per-category", action="store_true", help="apply --limit to each category")
    parser.add_argument("--design-only", action="store_true", help="only design-only products")
    args = parser.parse_args()

    local_dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not local_dsn:
        raise SystemExit("LOCAL_CONNECTION_STRING is required")
    client = gemini_client()

    started = time.perf_counter()
    vector = embed_query(client, args.query)
    embedded = time.perf_counter()
    with psycopg.connect(local_dsn, row_factory=dict_row) as connection:
        connected = time.perf_counter()
        results = search_assets(
            connection,
            vector,
            limit=args.limit,
            categories=args.category,
            purchase=args.purchase,
            max_price=args.max_price,
            currency=args.currency,
            max_width_m=args.max_width,
            max_depth_m=args.max_depth,
            max_height_m=args.max_height,
            placements=args.placement,
            colors=args.color,
            styles=args.style,
            materials=args.material,
            placeable=not args.include_unplaceable,
            known_price=args.known_price,
            per_category=args.per_category,
            design_only=args.design_only,
        )
        finished = time.perf_counter()

    print(
        f"results={len(results)} query_embedding_ms={(embedded - started) * 1000:.0f}"
        f" lookup_ms={(finished - connected) * 1000:.0f}"
    )
    for row in results:
        price = f"{row['price']} {row['currency']}" if row["price"] is not None else "no price"
        print(
            f"{row['similarity']:.3f} | {row['category']} | {row['title']} | {price}"
            f" | purchasable={row['is_purchasable']} | {row['placement_type']}"
            f" | {row['width_m']}x{row['depth_m']}x{row['height_m']} m | {','.join(row['colors'])}"
        )


if __name__ == "__main__":
    main()
