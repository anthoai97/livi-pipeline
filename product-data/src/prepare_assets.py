"""Prepare the next raw products into local pipeline.pipeline_assets_v2.

Reads catalog.assets and pipeline.decor_items from REMOTE_CONNECTION_STRING.
An LLM normalizes title, category, brand, description, colors, styles,
materials, and placement. A second LLM call fills only fields that are
still empty, and only when the source text or image supports them.
The importer copies identity, URLs, measured dimensions, price, currency,
and purchase status from the source. It skips a row without all three
dimensions, and drops dead image, model, and product links.

Writes each row to LOCAL_CONNECTION_STRING as it finishes and skips rows
already prepared there, so a rerun continues. Does not modify the remote database.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, get_args

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from gemini_usage import GeminiUsage
from llm_proxy import ProxyClient
SCHEMA_PATH = ROOT / "sql" / "001_pipeline_assets_v2.sql"
ASSET_NAMESPACE = uuid.UUID("6f1b9c2e-4a7d-4e1a-9c3b-8d2e5f7a1b64")
S3_BUCKET = "livinit-storage-prod"
S3_HTTPS = f"https://{S3_BUCKET}.s3.us-east-2.amazonaws.com"
PLACEMENTS = {"floor", "surface", "wall", "ceiling"}
WALL_CATEGORIES = {
    "wall_art",
    "wall_mirror",
    "wall_sconce",
    "sconce",
    "canvas_print",
    "canvas",
    "print",
}
CEILING_CATEGORIES = {"chandelier", "pendant", "pendant_light", "flush_mount_lamp"}
SURFACE_CATEGORIES = {
    "table_lamp",
    "vase",
    "sculpture",
    "decorative_bowl",
    "bowl",
    "candle_holders",
    "candleholder",
    "bookends",
    "picture_frame",
    "tray",
    "pitcher",
}
CAMPAIGN_PREFIXES = ("brand-", "price-", "not-", "type-")
BUNDLE_RE = re.compile(r"\b(\d+\s*[- ]?\s*piece|set)\b", re.IGNORECASE)
SET_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
}
IDENTICAL_SET_RE = re.compile(
    r"\bset of (\d+|two|three|four|five|six|seven|eight|nine|ten|twelve)\b|\b(\d+)\s*[- ]?pack\b",
    re.IGNORECASE,
)
USER_AGENT = "Mozilla/5.0 (compatible; livinit-pipeline/1.0)"
Color = Literal[
    "black", "white", "ivory", "cream", "beige", "tan", "brown", "gray",
    "charcoal", "silver", "gold", "brass", "bronze", "copper", "red",
    "burgundy", "orange", "yellow", "green", "olive", "blue", "navy",
    "teal", "purple", "pink", "multicolor", "clear",
]
Style = Literal[
    "modern", "contemporary", "mid-century modern", "minimalist",
    "scandinavian", "japandi", "industrial", "rustic", "farmhouse",
    "traditional", "transitional", "bohemian", "coastal", "glam",
    "art deco", "mediterranean", "vintage",
]
Material = Literal[
    "wood", "engineered wood", "bamboo", "rattan", "wicker", "jute",
    "metal", "glass", "mirror", "marble", "stone", "concrete", "ceramic",
    "porcelain", "terracotta", "plastic", "acrylic", "resin", "fabric",
    "boucle fabric", "velvet", "linen", "cotton", "wool", "leather",
    "faux leather", "foam", "paper",
]
ATTRIBUTE_VOCABULARY = {
    "colors": set(get_args(Color)),
    "styles": set(get_args(Style)),
    "materials": set(get_args(Material)),
}

class ProductFacts(BaseModel):
    """Fields Gemini extracts. Source facts such as price and size stay outside this schema."""

    title: str = Field(description="Short factual product name. Do not invent a model or collection name.")
    category: str | None = Field(description="One category from the supplied vocabulary, or null if uncertain.")
    brand: str | None = Field(description="Named product brand. The retailer is not the brand. Null if absent.")
    description: str = Field(description="One to three factual sentences. No promotional claims.")
    colors: list[Color] = Field(description="Colors of this variant only. Empty if unknown.")
    styles: list[Style] = Field(description="Design styles when the design is clear. Empty if uncertain.")
    materials: list[Material] = Field(description="Broad visible or stated materials. Empty if unknown.")
    placement_type: Literal["floor", "surface", "wall", "ceiling"] | None = Field(
        description="floor, surface, wall, or ceiling. Null if uncertain."
    )


def asset_id_for(source_table: str, source_id: str) -> uuid.UUID:
    return uuid.uuid5(ASSET_NAMESPACE, f"{source_table}:{source_id}")


def public_url(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    prefix = f"s3://{S3_BUCKET}/"
    if text.startswith(prefix):
        return f"{S3_HTTPS}/{text[len(prefix):]}"
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return None


def live_url(url: str | None, strict: bool = True) -> str | None:
    """Return the URL when it responds. Our S3 bucket is trusted. Retailer pages
    often block bots, so non-strict checks reject only 404, 410, and network failures."""
    if not url or url.startswith(S3_HTTPS):
        return url
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20):
                return url
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405, 501}:
                continue
            return None if strict or exc.code in {404, 410} else url
        except (OSError, ValueError, http.client.HTTPException):
            return None
    return None


def source_text(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


def identical_set_count(*parts: str | None) -> int | None:
    match = IDENTICAL_SET_RE.search(source_text(*parts))
    if not match:
        return None
    token = next(group for group in match.groups() if group).lower()
    count = int(token) if token.isdigit() else SET_WORDS[token]
    return count if count > 1 else None


def unclear_bundle(*parts: str | None) -> bool:
    if identical_set_count(*parts):
        return False
    return bool(BUNDLE_RE.search(source_text(*parts)))


def unit_price(price: Decimal | None, *parts: str | None) -> Decimal | None:
    """Use one item's price. Divide an explicit identical-set price; drop an unclear bundle."""
    if price is None:
        return None
    count = identical_set_count(*parts)
    if count:
        return (price / Decimal(count)).quantize(Decimal("0.01"))
    if unclear_bundle(*parts):
        return None
    return price


def positive_decimal(value) -> Decimal | None:
    """Parse a source price such as 1299, "$1,299.00", or "USD 1299"."""
    if value is None:
        return None
    text = re.sub(r"[^\d.\-]", "", str(value))
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if number <= 0:
        return None
    return number.quantize(Decimal("0.01"))


def positive_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return round(number, 4)


def clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.replace("\x00", " ").split())
    return text or None


def norm_list(values, allowed: set[str] | None = None) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,;/|]", values)
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        item = " ".join(value.strip().lower().split())
        if not item or item in seen or item in {"unknown", "n/a", "none", "null"}:
            continue
        if allowed is not None and item not in allowed:
            continue
        seen.add(item)
        cleaned.append(item)
    return sorted(cleaned)


def is_campaign_tag(value: str) -> bool:
    text = value.strip().lower()
    if any(text.startswith(prefix) for prefix in CAMPAIGN_PREFIXES):
        return True
    if re.fullmatch(r"[a-z]{3,9}\d{2,4}", text):
        return True
    return False


def useful_style_hints(raw_style: str | None, style_tags) -> list[str]:
    hints = []
    if raw_style and not is_campaign_tag(raw_style):
        hints.append(raw_style)
    if isinstance(style_tags, list):
        for tag in style_tags:
            if isinstance(tag, str) and not is_campaign_tag(tag):
                hints.append(tag)
    return norm_list(hints)


def fallback_placement(category: str | None, label: str | None) -> str | None:
    text = f"{category or ''} {label or ''}".lower()
    if category in WALL_CATEGORIES or "wall mirror" in text or "wall art" in text:
        return "wall"
    if category in CEILING_CATEGORIES:
        return "ceiling"
    if category in SURFACE_CATEGORIES or "on a shelf" in text:
        return "surface"
    if category:
        return "floor"
    if any(word in text for word in ("plant", "tree", "bonsai")):
        return "floor"
    return None


def fallback_category(raw_category: str | None, label: str | None, vocabulary: set[str]) -> str | None:
    candidate = (raw_category or "").strip().lower().replace(" ", "_").replace("-", "_")
    if candidate in vocabulary:
        return candidate
    text = (label or "").lower()
    guesses = (
        ("wall mirror", "wall_mirror"),
        ("mirror", "floor_mirror"),
        ("plant", "planter"),
        ("tree", "planter"),
        ("bonsai", "planter"),
        ("figurine", "sculpture"),
        ("sculpture", "sculpture"),
        ("ornament", "sculpture"),
    )
    for needle, category in guesses:
        if needle in text and category in vocabulary:
            return category
    return None


def fallback_title(name: str | None, description: str | None, category: str | None) -> str:
    short_description = clean_text(description)
    if short_description and len(short_description) <= 80 and short_description.count(".") == 0:
        return short_description
    raw_name = clean_text(name)
    if raw_name and not re.search(r"[_-]\d+$", raw_name):
        return raw_name
    if category:
        return category.replace("_", " ")
    return raw_name or short_description or "Untitled item"


def load_categories(connection: psycopg.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT lower(category) AS category
        FROM catalog.assets
        WHERE category IS NOT NULL AND category <> '' AND NOT is_deleted
        """
    ).fetchall()
    return {row["category"] for row in rows}


def fetch_assets(connection: psycopg.Connection, limit: int, done: list[uuid.UUID]) -> list[dict]:
    return connection.execute(
        """
        SELECT
            asset_id::text AS source_id,
            name,
            category,
            description,
            tags,
            model_url,
            coalesce(product_url, meta->>'product_url') AS product_url,
            color,
            style,
            source AS retailer,
            image_url,
            meta->>'currency' AS currency,
            meta->>'price' AS price,
            meta->>'color_primary' AS color_primary,
            meta->>'material_primary' AS material_primary,
            meta->>'mount' AS mount,
            meta->'style_tags' AS style_tags,
            meta->'assetMetadata'->'boundingBox'->>'x' AS width_m,
            meta->'assetMetadata'->'boundingBox'->>'y' AS depth_m,
            meta->'assetMetadata'->'boundingBox'->>'z' AS height_m
        FROM catalog.assets,
            -- Some rows store metadata as a JSON-encoded string.
            LATERAL (SELECT CASE WHEN jsonb_typeof(metadata) = 'string'
                THEN (metadata #>> '{}')::jsonb ELSE metadata END AS meta) decoded
        WHERE NOT is_deleted
          AND link_status IS DISTINCT FROM 'dead'
          AND coalesce(model_url, '') <> ''
          AND meta @? '$.assetMetadata.boundingBox ? (@.x.double() > 0 && @.y.double() > 0 && @.z.double() > 0)'
          AND asset_id <> ALL(%s::uuid[])
        ORDER BY asset_id
        LIMIT %s
        """,
        (done, limit),
    ).fetchall()


def fetch_decor(connection: psycopg.Connection, limit: int, done: list[uuid.UUID]) -> list[dict]:
    return connection.execute(
        """
        SELECT
            model_id::text AS source_id,
            name,
            label,
            description,
            width::text AS width_m,
            depth::text AS depth_m,
            height::text AS height_m,
            glb_url AS model_url,
            product_image_url AS image_url
        FROM pipeline.decor_items
        WHERE NOT is_deleted
          AND coalesce(glb_url, '') <> ''
          AND width > 0 AND depth > 0 AND height > 0
          AND model_id <> ALL(%s::uuid[])
        ORDER BY model_id
        LIMIT %s
        """,
        (done, limit),
    ).fetchall()


def source_record(row: dict, source_table: str) -> dict:
    image_url = live_url(public_url(row.get("image_url")))
    model_url = live_url(public_url(row.get("model_url")))
    product_url = (
        live_url(public_url(row.get("product_url")), strict=False)
        if source_table == "catalog.assets"
        else None
    )
    price = positive_decimal(row.get("price")) if source_table == "catalog.assets" else None
    currency = None
    if source_table == "catalog.assets":
        raw_currency = clean_text(row.get("currency"))
        if raw_currency and re.fullmatch(r"[A-Za-z]{3}", raw_currency):
            currency = raw_currency.upper()
        price = unit_price(price, row.get("name"), row.get("description"))
        if price is None or currency is None:
            price = currency = None
    if source_table == "catalog.assets":
        purchasable = True if price is not None or product_url else None
    else:
        purchasable = False
    return {
        "asset_id": asset_id_for(source_table, row["source_id"]),
        "source_table": source_table,
        "source_id": uuid.UUID(row["source_id"]),
        "raw_name": clean_text(row.get("name")),
        "raw_description": clean_text(row.get("description")),
        "raw_category": clean_text(row.get("category")),
        "raw_label": clean_text(row.get("label")),
        "raw_color": clean_text(row.get("color_primary") or row.get("color")),
        "raw_material": clean_text(row.get("material_primary")),
        "raw_tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
        "style_hints": useful_style_hints(row.get("style"), row.get("style_tags")),
        "retailer": clean_text(row.get("retailer")),
        "mount": clean_text(row.get("mount")),
        "width_m": positive_float(row.get("width_m")),
        "depth_m": positive_float(row.get("depth_m")),
        "height_m": positive_float(row.get("height_m")),
        "is_purchasable": purchasable,
        "price": price,
        "currency": currency,
        "image_url": image_url,
        "product_url": product_url,
        "model_url": model_url,
    }


def llm_image(record: dict) -> str | None:
    """Image URL the model accepts. The proxy rejects AVIF."""
    url = record.get("image_url")
    return None if not url or url.lower().endswith(".avif") else url


def llm_input(record: dict) -> dict:
    return {
        "raw_name": record["raw_name"],
        "raw_description": record["raw_description"],
        "raw_category": record["raw_category"],
        "raw_label": record["raw_label"],
        "color_hint": record["raw_color"],
        "material_hint": record["raw_material"],
        "tags": record["raw_tags"],
        "style_hints": record["style_hints"],
        "retailer": record["retailer"],
        "mount": record["mount"],
        "image_provided": False,
    }


def apply_llm(record: dict, extracted: dict, vocabulary: set[str]) -> dict:
    if "category" in extracted and extracted.get("category") is None:
        category = None
    else:
        category = fallback_category(
            clean_text(extracted.get("category")) or record["raw_category"],
            record["raw_label"],
            vocabulary,
        )
    brand = clean_text(extracted.get("brand"))
    retailer = (record["retailer"] or "").casefold()
    if brand is None or brand.casefold() in {retailer, "unknown", "n/a", "none"}:
        brand = None
    title = clean_text(extracted.get("title")) or fallback_title(
        record["raw_name"], record["raw_description"], category
    )
    description = clean_text(extracted.get("description"))
    if description and description.casefold() in {"unknown", "n/a", "none"}:
        description = None
    if "placement_type" in extracted and extracted.get("placement_type") is None:
        placement = None
    else:
        placement = clean_text(extracted.get("placement_type"))
        placement = placement.lower() if placement else None
        if placement not in PLACEMENTS:
            placement = fallback_placement(category, record["raw_label"])
    prepared = {
        **{key: record[key] for key in (
            "asset_id",
            "source_table",
            "source_id",
            "width_m",
            "depth_m",
            "height_m",
            "is_purchasable",
            "price",
            "currency",
            "image_url",
            "product_url",
            "model_url",
        )},
        "title": title[:180],
        "category": category,
        "brand": brand[:120] if brand else None,
        "description": description[:1200] if description else None,
        "colors": norm_list(extracted.get("colors"), ATTRIBUTE_VOCABULARY["colors"])
        or norm_list(record["raw_color"], ATTRIBUTE_VOCABULARY["colors"]),
        "styles": norm_list(extracted.get("styles"), ATTRIBUTE_VOCABULARY["styles"]),
        "materials": norm_list(extracted.get("materials"), ATTRIBUTE_VOCABULARY["materials"])
        or norm_list(record["raw_material"], ATTRIBUTE_VOCABULARY["materials"]),
        "placement_type": placement,
        "llm_used": True,
    }
    return prepared


def fallback_record(record: dict, vocabulary: set[str]) -> dict:
    category = fallback_category(record["raw_category"], record["raw_label"], vocabulary)
    title = fallback_title(record["raw_name"], record["raw_description"], category)
    description = record["raw_description"]
    if description and len(description) > 400:
        description = None
    return {
        **{key: record[key] for key in (
            "asset_id",
            "source_table",
            "source_id",
            "width_m",
            "depth_m",
            "height_m",
            "is_purchasable",
            "price",
            "currency",
            "image_url",
            "product_url",
            "model_url",
        )},
        "title": title[:180],
        "category": category,
        "brand": None,
        "description": description,
        "colors": norm_list(record["raw_color"], ATTRIBUTE_VOCABULARY["colors"]),
        "styles": norm_list(record["style_hints"], ATTRIBUTE_VOCABULARY["styles"]),
        "materials": norm_list(record["raw_material"], ATTRIBUTE_VOCABULARY["materials"]),
        "placement_type": fallback_placement(category, record["raw_label"]),
        "llm_used": False,
    }


def product_schema() -> dict:
    schema = ProductFacts.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}))
    return schema


def call_json(
    client: ProxyClient,
    text: str,
    usage: GeminiUsage,
    *,
    step: str,
    record: dict,
) -> dict:
    """One chat completion through the local proxy, using the Codex Pro subscription."""
    content: list | str = text
    image_url = llm_image(record)
    if image_url:
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    facts = client.complete(
        content,
        product_schema(),
        name="product_facts",
        on_response=lambda payload: usage.record_chat(
            payload,
            step=step,
            requested_model=client.model,
            source_table=record["source_table"],
            source_id=str(record["source_id"]),
        ),
    )
    return ProductFacts.model_validate(facts).model_dump()


def gap_fields(prepared: dict) -> list[str]:
    """Fields a second LLM call may fill. Measured facts stay untouched."""
    gaps = []
    if not prepared.get("title"):
        gaps.append("title")
    if not prepared.get("category"):
        gaps.append("category")
    if not prepared.get("description"):
        gaps.append("description")
    if not prepared.get("colors"):
        gaps.append("colors")
    if not prepared.get("styles"):
        gaps.append("styles")
    if not prepared.get("materials"):
        gaps.append("materials")
    if not prepared.get("placement_type"):
        gaps.append("placement_type")
    return gaps


def merge_gaps(prepared: dict, filled: dict, gaps: list[str], vocabulary: set[str]) -> list[str]:
    """Copy only supported values into fields that are still empty."""
    changed = []
    for field in gaps:
        if prepared.get(field):
            continue
        if field in ATTRIBUTE_VOCABULARY:
            values = norm_list(filled.get(field), ATTRIBUTE_VOCABULARY[field])
            if values:
                prepared[field] = values
                changed.append(field)
            continue
        if field == "category":
            category = fallback_category(clean_text(filled.get("category")), None, vocabulary)
            if category:
                prepared["category"] = category
                changed.append(field)
            continue
        if field == "placement_type":
            placement = clean_text(filled.get("placement_type"))
            placement = placement.lower() if placement else None
            if placement in PLACEMENTS:
                prepared["placement_type"] = placement
                changed.append(field)
            continue
        text = clean_text(filled.get(field))
        if text and text.casefold() not in {"unknown", "n/a", "none"}:
            limit = 180 if field == "title" else 1200
            prepared[field] = text[:limit]
            changed.append(field)
    return changed


def fill_missing(
    client: ProxyClient,
    record: dict,
    prepared: dict,
    vocabulary: set[str],
    usage: GeminiUsage,
) -> dict:
    gaps = gap_fields(prepared)
    prepared["gaps_requested"] = gaps
    prepared["gaps_filled"] = []
    if not gaps:
        return prepared
    current = {
        "title": prepared.get("title"),
        "category": prepared.get("category"),
        "description": prepared.get("description"),
        "colors": prepared.get("colors"),
        "styles": prepared.get("styles"),
        "materials": prepared.get("materials"),
        "placement_type": prepared.get("placement_type"),
    }
    payload = llm_input(record)
    payload["image_provided"] = bool(llm_image(record))
    text = (
        "Fill the empty product fields from the source and image. "
        "Leave a value null or a list empty when the source does not support it.\n"
        "Fill only: " + ", ".join(gaps) + "\n"
        + ("Categories: " + ", ".join(sorted(vocabulary)) + "\n" if "category" in gaps else "")
        + "Current record:\n" + json.dumps(current, ensure_ascii=True) + "\n"
        "Source:\n" + json.dumps(payload, ensure_ascii=True)
    )
    try:
        filled = call_json(client, text, usage, step="fill_missing", record=record)
    except Exception as exc:
        print(
            f"gap fill failed for {record['source_table']} {record['source_id']}: {exc}",
            file=sys.stderr,
        )
        return prepared
    prepared["gaps_filled"] = merge_gaps(prepared, filled, gaps, vocabulary)
    return prepared


def extract_record(
    client: ProxyClient,
    row: dict,
    source_table: str,
    vocabulary: set[str],
    usage: GeminiUsage,
) -> dict | None:
    """Prepare one source row. Return None when it lacks full dimensions."""
    record = source_record(row, source_table)
    missing = [field for field in ("width_m", "depth_m", "height_m") if not record[field]]
    if missing:
        print(f"skip {source_table} {row['source_id']}: missing {','.join(missing)}", file=sys.stderr)
        return None
    payload = llm_input(record)
    payload["image_provided"] = bool(llm_image(record))
    text = (
        "Extract the product record from this source and its image.\n"
        "Categories: " + ", ".join(sorted(vocabulary)) + "\n"
        "Source:\n" + json.dumps(payload, ensure_ascii=True)
    )
    try:
        prepared = apply_llm(
            record,
            call_json(client, text, usage, step="extract", record=record),
            vocabulary,
        )
    except Exception as exc:
        print(f"llm failed for {record['source_table']} {record['source_id']}: {exc}", file=sys.stderr)
        prepared = fallback_record(record, vocabulary)
    return fill_missing(client, record, prepared, vocabulary, usage)


def ensure_local_schema(connection: psycopg.Connection) -> None:
    statements = [
        statement.strip()
        for statement in SCHEMA_PATH.read_text().split(";")
        if statement.strip()
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def upsert_records(connection: psycopg.Connection, records: list[dict]) -> None:
    with connection.cursor() as cursor:
        for record in records:
            cursor.execute(
                """
                INSERT INTO pipeline.pipeline_assets_v2 (
                    asset_id, source_table, source_id, title, category, brand,
                    description, colors, styles, materials, width_m, depth_m,
                    height_m, placement_type, is_purchasable, price, currency,
                    image_url, product_url, model_url
                )
                VALUES (
                    %(asset_id)s, %(source_table)s, %(source_id)s, %(title)s,
                    %(category)s, %(brand)s, %(description)s, %(colors)s,
                    %(styles)s, %(materials)s, %(width_m)s, %(depth_m)s,
                    %(height_m)s, %(placement_type)s, %(is_purchasable)s,
                    %(price)s, %(currency)s, %(image_url)s, %(product_url)s,
                    %(model_url)s
                )
                ON CONFLICT (source_table, source_id) DO UPDATE SET
                    asset_id = EXCLUDED.asset_id,
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    brand = EXCLUDED.brand,
                    description = EXCLUDED.description,
                    colors = EXCLUDED.colors,
                    styles = EXCLUDED.styles,
                    materials = EXCLUDED.materials,
                    width_m = EXCLUDED.width_m,
                    depth_m = EXCLUDED.depth_m,
                    height_m = EXCLUDED.height_m,
                    placement_type = EXCLUDED.placement_type,
                    is_purchasable = EXCLUDED.is_purchasable,
                    price = EXCLUDED.price,
                    currency = EXCLUDED.currency,
                    image_url = EXCLUDED.image_url,
                    product_url = EXCLUDED.product_url,
                    model_url = EXCLUDED.model_url,
                    prepared_at = now()
                """,
                record,
            )
    connection.commit()


def summarize(connection: psycopg.Connection) -> None:
    row = connection.execute(
        """
        SELECT
            count(*) AS rows,
            count(*) FILTER (WHERE source_table = 'catalog.assets') AS assets,
            count(*) FILTER (WHERE source_table = 'pipeline.decor_items') AS decor,
            count(*) FILTER (
                WHERE coalesce(title, '') <> ''
                  AND category IS NOT NULL
                  AND coalesce(description, '') <> ''
                  AND coalesce(image_url, '') <> ''
            ) AS ready,
            count(*) FILTER (WHERE width_m IS NULL OR depth_m IS NULL OR height_m IS NULL) AS missing_dimension,
            count(*) FILTER (WHERE is_purchasable) AS purchasable
        FROM pipeline.pipeline_assets_v2
        """
    ).fetchone()
    print(
        "local pipeline.pipeline_assets_v2:"
        f" rows={row['rows']} assets={row['assets']} decor={row['decor']}"
        f" ready={row['ready']} missing_dimension={row['missing_dimension']}"
        f" purchasable={row['purchasable']}"
    )
    samples = connection.execute(
        """
        SELECT source_table, title, category, colors, styles, materials,
               placement_type, width_m, depth_m, height_m, price, currency,
               is_purchasable
        FROM pipeline.pipeline_assets_v2
        ORDER BY source_table, title
        LIMIT 6
        """
    ).fetchall()
    for sample in samples:
        print(
            f"- {sample['source_table']} | {sample['title']} | {sample['category']}"
            f" | {sample['placement_type']} | {sample['colors']} | {sample['materials']}"
            f" | {sample['width_m']}x{sample['depth_m']}x{sample['height_m']} m"
            f" | {sample['price']} {sample['currency']} | purchasable={sample['is_purchasable']}"
        )


def main() -> None:
    load_dotenv(ROOT.parent / ".env")
    parser = argparse.ArgumentParser(description="Prepare raw products into pipeline.pipeline_assets_v2")
    parser.add_argument("--assets", type=int, default=70, help="new catalog.assets rows to prepare")
    parser.add_argument("--decor", type=int, default=30, help="new pipeline.decor_items rows to prepare")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.assets < 0 or args.decor < 0 or args.assets + args.decor == 0:
        raise SystemExit("choose a positive number of rows")

    remote_dsn = os.environ.get("REMOTE_CONNECTION_STRING", "").strip()
    local_dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not remote_dsn or not local_dsn:
        raise SystemExit("REMOTE_CONNECTION_STRING and LOCAL_CONNECTION_STRING are required")
    client = ProxyClient()

    done: dict[str, list[uuid.UUID]] = {"catalog.assets": [], "pipeline.decor_items": []}
    with psycopg.connect(local_dsn, row_factory=dict_row) as local:
        ensure_local_schema(local)
        for row in local.execute("SELECT source_table, source_id FROM pipeline.pipeline_assets_v2"):
            done[row["source_table"]].append(row["source_id"])

    with psycopg.connect(remote_dsn, row_factory=dict_row) as remote:
        vocabulary = load_categories(remote)
        rows = [
            (row, "catalog.assets")
            for row in fetch_assets(remote, args.assets, done["catalog.assets"])
        ]
        rows.extend(
            (row, "pipeline.decor_items")
            for row in fetch_decor(remote, args.decor, done["pipeline.decor_items"])
        )
    if len(rows) != args.assets + args.decor:
        print(
            f"fetched {len(rows)} rows, requested {args.assets + args.decor}",
            file=sys.stderr,
        )

    usage = GeminiUsage(ROOT / "logs" / "model-usage.jsonl")
    prepared: list[dict] = []
    with (
        psycopg.connect(local_dsn, row_factory=dict_row) as local,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        futures = [
            pool.submit(extract_record, client, row, source_table, vocabulary, usage)
            for row, source_table in rows
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            item = future.result()
            if item is None:
                continue
            upsert_records(local, [item])
            prepared.append(item)
            filled = ",".join(item.get("gaps_filled") or []) or "-"
            print(
                f"[{index}/{len(futures)}] {item['source_table']} {item['category']} {item['title']}"
                f" gaps={filled}"
            )

        summarize(local)
    llm_count = sum(1 for item in prepared if item["llm_used"])
    gap_calls = sum(1 for item in prepared if item.get("gaps_requested"))
    gap_filled = sum(1 for item in prepared if item.get("gaps_filled"))
    report = usage.summary()
    totals = report["totals"]
    print(
        f"prepared {len(prepared)} rows with model {client.model};"
        f" skipped={len(rows) - len(prepared)}"
        f" llm={llm_count} fallback={len(prepared) - llm_count}"
        f" gap_calls={gap_calls} gap_filled={gap_filled}"
    )
    print(
        "proxy usage:"
        f" calls={totals['calls']}"
        f" input_tokens={totals['input_tokens']}"
        f" output_tokens={totals['output_tokens']}"
        f" thoughts_tokens={totals['thoughts_tokens']}"
        f" cost_usd={totals['cost_usd']:.6f}"
        f" billing=codex-pro-subscription"
        f" log={usage.log_path}"
        f" summary={usage.summary_path}"
    )


if __name__ == "__main__":
    main()
