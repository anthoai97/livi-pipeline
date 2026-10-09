"""Catalog identity, price, category, and hydration helpers."""

import re
from typing import Any
from app.rules.placement_mode import placement_mode_for_type
from app.rules.planner.fit_policy import SECTIONAL_MIN_ROOM
from app.rules.planner.taxonomy import NON_SELLABLE_CATEGORIES, normalize_category, sibling_categories
from app.rules.selection.constants import SECTIONAL_CATEGORIES


def catalog_asset(record: dict[str, Any]) -> dict[str, Any]:
    """Add the rule fields to a pipeline.pipeline_assets_v2 record.

    width/depth/height are the record's metres, placement_mode comes from
    placement_type, price is what counts toward the budget (0 when not
    purchasable), and is_decor_item marks pipeline.decor_items rows.
    """
    purchasable = bool(record.get("is_purchasable"))
    return {
        **record,
        "width": float(record.get("width_m") or 0.0),
        "depth": float(record.get("depth_m") or 0.0),
        "height": float(record.get("height_m") or 0.0),
        "placement_mode": placement_mode_for_type(record.get("placement_type")),
        "price": float(record["price"]) if purchasable and record.get("price") is not None else 0.0,
        "is_decor_item": record.get("source_table") == "pipeline.decor_items",
    }

def _asset_price_float(asset: dict | None) -> float | None:
    if not asset:
        return None
    price = asset.get("price") if asset.get("price") is not None else asset.get("cost")
    if price in (None, ""):
        return None
    if isinstance(price, str):
        price = price.strip().replace("$", "").replace(",", "")
    try:
        return float(price)
    except (TypeError, ValueError):
        return None

def _asset_score_float(asset: dict) -> float:
    try:
        return float(asset.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0

def _price_constraint_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    # Catalog prices are positive. A zero here is the intent extractor's
    # placeholder for a qualitative constraint such as "cheaper", not a
    # literal request for free furniture.
    return price if price > 0 else None

def _price_constraint_category_matches(category: str, target: str) -> bool:
    category_terms = _expanded_price_categories(category)
    target_terms = _expanded_price_categories(target)
    if not category_terms or not target_terms:
        return False
    return bool(category_terms & target_terms)

def _expanded_price_categories(category: str) -> set[str]:
    return sibling_categories(category)

_GENERIC_BRAND_SUFFIXES = {
    "co",
    "collection",
    "company",
    "furniture",
    "home",
    "inc",
    "llc",
    "ltd",
}

def _normalize_brand(value: Any) -> str:
    words = re.findall(
        r"[a-z0-9]+",
        str(value or "").casefold().replace("'", "").replace("’", ""),
    )
    while len(words) > 1 and words[-1] in _GENERIC_BRAND_SUFFIXES:
        words.pop()
    return "".join(words)

def _asset_brand(asset: dict[str, Any]) -> str:
    return str(
        asset.get("company_name")
        or asset.get("brand")
        or asset.get("source")
        or ""
    ).strip()

def _brand_preferences_for_category(
    intent_packet: dict[str, Any],
    target_category: str,
) -> list[str]:
    constraints = intent_packet.get("asset_attribute_constraints")
    if not isinstance(constraints, list):
        return []
    target = normalize_category(target_category)
    preferences: list[str] = []
    seen: set[str] = set()
    for constraint in constraints:
        if (
            not isinstance(constraint, dict)
            or str(constraint.get("attribute_type") or "").strip().lower()
            != "brand"
        ):
            continue
        category = normalize_category(constraint.get("category"))
        if category:
            if not target or not _price_constraint_category_matches(
                category,
                target,
            ):
                continue
        value = str(constraint.get("value") or "").strip()
        normalized = _normalize_brand(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        preferences.append(value)
    return preferences

def _asset_matches_brand_preferences(
    asset: dict[str, Any],
    preferences: list[str],
) -> bool:
    brand = _normalize_brand(_asset_brand(asset))
    return bool(brand) and brand in {
        normalized
        for preference in preferences
        if (normalized := _normalize_brand(preference))
    }

def _recommendation_price_constraints(
    intent_packet: dict[str, Any],
    target_category: str,
) -> list[dict[str, Any]]:
    raw_constraints = intent_packet.get("price_constraints")
    if not isinstance(raw_constraints, list):
        return []
    target = normalize_category(target_category)
    general: list[dict[str, Any]] = []
    targeted: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    for constraint in raw_constraints:
        if not isinstance(constraint, dict):
            continue
        min_price = _price_constraint_value(constraint.get("min_price"))
        max_price = _price_constraint_value(constraint.get("max_price"))
        if min_price is None and max_price is None:
            continue
        category = normalize_category(constraint.get("category"))
        cleaned = {
            "category": category or None,
            "relation": str(constraint.get("relation") or "").strip() or None,
            "min_price": min_price,
            "max_price": max_price,
            "currency": str(constraint.get("currency") or "USD").strip() or "USD",
        }
        if category and target and _price_constraint_category_matches(category, target):
            targeted.append(cleaned)
        elif not category:
            general.append(cleaned)
        elif not target:
            unscoped.append(cleaned)
    if targeted:
        return targeted + general
    if target:
        return general
    return unscoped + general

def _asset_satisfies_price_constraints(
    asset: dict[str, Any],
    constraints: list[dict[str, Any]],
) -> bool:
    if not constraints:
        return True
    price = _asset_price_float(asset)
    if price is None:
        return False
    asset_category = normalize_category(asset.get("category"))
    applied = False
    for constraint in constraints:
        category = normalize_category(constraint.get("category"))
        if category and not _price_constraint_category_matches(category, asset_category):
            continue
        applied = True
        min_price = _price_constraint_value(constraint.get("min_price"))
        max_price = _price_constraint_value(constraint.get("max_price"))
        if min_price is not None and price < min_price:
            return False
        if max_price is not None and price > max_price:
            return False
    return applied

def _category_matches(left: Any, right: Any) -> bool:
    if not str(left or "").strip() or not str(right or "").strip():
        return False
    return (
        _normalize_asset_text(left) == _normalize_asset_text(right)
        or normalize_category(left) == normalize_category(right)
    )

def _asset_category_matches(asset: dict, category: str) -> bool:
    return _category_matches(asset.get("category"), category)

def _compute_total_cost(assets: list[dict]) -> float:
    """Sum prices excluding non-sellable categories (e.g. TV)."""
    total = 0.0
    for asset in assets:
        if normalize_category(asset.get("category")) in NON_SELLABLE_CATEGORIES:
            continue
        price = asset.get("price") if asset.get("price") is not None else asset.get("cost")
        if isinstance(price, str):
            price = price.strip().replace("$", "").replace(",", "")
        try:
            total += float(price or 0)
        except (TypeError, ValueError):
            continue
    return total

def _compute_physical_footprint(assets: list[dict]) -> float:
    footprint = 0.0
    for asset in assets:
        uid = str(asset.get("uid") or asset.get("instance_key") or "").lower()
        category = str(asset.get("category") or "").lower()
        if "rug" in uid or "carpet" in uid or "rug" in category or "carpet" in category:
            continue
        footprint += float(asset.get("width") or 0.0) * float(asset.get("depth") or 0.0)
    return round(footprint, 4)

def _asset_uid(asset: dict[str, Any]) -> str:
    return str(asset.get("uid") or asset.get("instance_key") or asset.get("name") or "")

def _asset_dim(asset: dict[str, Any], key: str) -> float:
    try:
        return max(0.0, float(asset.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0

def _asset_width_depth(asset: dict[str, Any]) -> tuple[float, float]:
    return _asset_dim(asset, "width"), _asset_dim(asset, "depth")

def _asset_matches_any(
    asset: dict[str, Any],
    categories: set[str],
    *,
    keywords: tuple[str, ...] = (),
) -> bool:
    category = normalize_category(asset.get("category"))
    if category in categories:
        return True
    raw_category = _normalize_asset_text(asset.get("category"))
    raw_uid = _normalize_asset_text(_asset_uid(asset))
    return any(
        keyword in raw_category or keyword in raw_uid
        for keyword in keywords
    )

def _base_uid(uid: str, catalog: dict) -> str:
    """Strip instance suffix (e.g., accent_chair_5_1 -> accent_chair_5)."""
    if uid in catalog:
        return uid
    base = re.sub(r"_\d+$", "", uid)
    return base if base in catalog else uid

def _canonical_selection_uid(uid: Any, catalog: dict) -> str:
    """Normalize minor LLM UID formatting drift only when it resolves to catalog data."""
    raw = str(uid or "").strip()
    if not raw or raw in catalog:
        return raw
    stripped = raw.strip(" \t\r\n\"'`[](){}<>.,;:")
    if stripped in catalog:
        return stripped
    base = _base_uid(stripped, catalog)
    if base in catalog:
        return base
    return raw

def _normalize_asset_text(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())).strip()

def _coerce_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for category, count in value.items():
        try:
            amount = int(count)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            key = normalize_category(category) or str(category or "").strip().lower()
            if key:
                result[key] = amount
    return dict(sorted(result.items()))

def _catalog_asset_for_selected(
    asset: dict[str, Any],
    assets_by_uid: dict[str, dict],
) -> dict[str, Any]:
    uid = str(asset.get("uid") or "")
    base_uid = _base_uid(uid, assets_by_uid)
    return {**assets_by_uid.get(base_uid, {}), **asset}

def filter_oversized_seating(
    assets: list[dict], room_width: float, room_depth: float
) -> list[dict]:
    """Exclude sectionals from rooms smaller than SECTIONAL_MIN_ROOM (from asset_selection/revision.py)."""
    min_w, min_d = SECTIONAL_MIN_ROOM
    if (room_width >= min_w and room_depth >= min_d) or (
        room_depth >= min_w and room_width >= min_d
    ):
        return assets
    return [
        a for a in assets if str(a.get("category") or "").lower() not in SECTIONAL_CATEGORIES
    ]
