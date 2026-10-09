from collections.abc import Mapping
from typing import Any

from .taxonomy import CANONICAL_CATEGORIES, normalize_category


def _normalize_prompt(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _normalize_prompt(item)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_category(value: Any) -> str:
    category = normalize_category(value)
    return category if category in CANONICAL_CATEGORIES else ""


# Per-item size limits in metres, and strict prepared-vocabulary attributes.
_SIZE_LIMIT_KEYS = (
    "min_width_m",
    "max_width_m",
    "min_depth_m",
    "max_depth_m",
    "min_height_m",
    "max_height_m",
)


def _clean_size_value(value: Any) -> float | None:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _clean_requested_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        category = _clean_category(
            item.get("canonical_category") or item.get("category")
        )
        if not category:
            continue
        try:
            count = int(item.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        if count <= 0:
            continue
        substitutes = [
            candidate
            for candidate in (
                _clean_category(candidate)
                for candidate in (item.get("acceptable_substitutes") or [])
            )
            if candidate and candidate != category
        ]
        entry = {
            "label": _normalize_prompt(item.get("label") or category),
            "canonical_category": category,
            "count": count,
            "exact": bool(item.get("exact")),
            "optional": bool(item.get("optional")),
            "acceptable_substitutes": substitutes,
        }
        descriptors = _clean_string_list(item.get("descriptors"))
        if descriptors:
            entry["descriptors"] = descriptors
        for key in _SIZE_LIMIT_KEYS:
            limit = _clean_size_value(item.get(key))
            if limit is not None:
                entry[key] = limit
        for key in ("colors", "styles", "materials"):
            values = [value.lower() for value in _clean_string_list(item.get(key))]
            if values:
                entry[key] = values
        relation = _normalize_prompt(item.get("relation") or item.get("support_relation"))
        if relation:
            entry["relation"] = relation
        cleaned.append(entry)
    return cleaned


def _clean_category_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        category = _clean_category(item)
        if category and category not in cleaned:
            cleaned.append(category)
    return cleaned


def _clean_price_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _clean_price_constraints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        category = _clean_category(item.get("category"))
        min_price = _clean_price_value(item.get("min_price"))
        max_price = _clean_price_value(item.get("max_price"))
        if min_price is None and max_price is None:
            continue
        if min_price is not None and max_price is not None and min_price > max_price:
            min_price, max_price = max_price, min_price
        relation = _normalize_prompt(item.get("relation")).lower()
        if relation not in {"under", "above", "between"}:
            if min_price is not None and max_price is not None:
                relation = "between"
            elif max_price is not None:
                relation = "under"
            else:
                relation = "above"
        cleaned.append(
            {
                "label": _normalize_prompt(item.get("label") or "price constraint"),
                "category": category or None,
                "relation": relation,
                "min_price": min_price,
                "max_price": max_price,
                "currency": _normalize_prompt(item.get("currency") or "USD") or "USD",
            }
        )
    return cleaned


_ATTRIBUTE_TYPES = {
    "brand",
    "color",
    "style",
    "material",
    "fabric",
    "shape",
    "size",
    "lifestyle",
    "purpose",
    "other",
}


def _clean_attribute_constraints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        category = _clean_category(item.get("category"))
        attribute_type = _normalize_prompt(
            item.get("attribute_type") or item.get("type") or "other"
        ).lower()
        if attribute_type not in _ATTRIBUTE_TYPES:
            attribute_type = "other"
        value_text = _normalize_prompt(item.get("value"))
        if not value_text:
            continue
        key = (category, attribute_type, value_text.lower())
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "category": category or None,
            "attribute_type": attribute_type,
            "value": value_text,
            "required": bool(item.get("required", True)),
        }
        source_label = _normalize_prompt(
            item.get("source_label") or item.get("label") or value_text
        )
        if source_label:
            entry["source_label"] = source_label
        cleaned.append(entry)
    return cleaned


def count_constraints_from_intent_packet(
    packet: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(packet, Mapping):
        return {}
    constraints: dict[str, dict[str, Any]] = {}
    for item in _clean_requested_items(packet.get("requested_items")):
        category = item["canonical_category"]
        entry = constraints.setdefault(
            category,
            {
                "count": 0,
                "exact": False,
                "optional": True,
                "source": "llm_intent",
            },
        )
        entry["count"] = int(entry["count"]) + int(item["count"])
        entry["exact"] = bool(entry["exact"]) or bool(item["exact"])
        # A stated quantity ("two armchairs") arrives as count > 1; bare
        # mentions ("a linen sofa") default to 1 and stay advisory.
        if int(item["count"]) > 1:
            entry["source"] = "counted"
        entry["optional"] = bool(entry["optional"]) and bool(item["optional"])
        substitutes = list(entry.get("acceptable_substitutes") or [])
        for substitute in item.get("acceptable_substitutes") or []:
            if substitute not in substitutes:
                substitutes.append(substitute)
        if substitutes:
            entry["acceptable_substitutes"] = substitutes
    return dict(sorted(constraints.items()))


def requested_counts_from_intent_packet(
    packet: Mapping[str, Any] | None,
) -> dict[str, int]:
    return {
        category: int(details["count"])
        for category, details in count_constraints_from_intent_packet(packet).items()
    }


def coerce_intent_packet(
    packet: Any,
    *,
    fallback_intent: Any = "",
    room_type: str | None = None,
) -> dict[str, Any]:
    """Clean a model intent packet into the fields the rules read.

    requested_items keep label, canonical_category, count, exact, optional,
    acceptable_substitutes, descriptors, relation, the size limits
    (min/max_width_m, min/max_depth_m, min/max_height_m) and strict
    colors/styles/materials. room_type, when given, is the request's room type
    and overrides the packet's.
    """
    if not isinstance(packet, Mapping) or not packet:
        return {}

    normalized_prompt = _normalize_prompt(
        packet.get("normalized_prompt") or fallback_intent
    )
    derived: dict[str, Any] = {
        "normalized_prompt": normalized_prompt,
        "room_type": None,
        "requested_categories": [],
        "excluded_categories": [],
        "avoid_categories": [],
        "style_hints": [],
        "functional_hints": [],
        "requested_items": [],
        "price_constraints": [],
        "asset_attribute_constraints": [],
        "room_goal": "",
        "requires_straight_circulation_path": False,
        "fit_flexibility": "balanced",
    }

    room_type = _normalize_prompt(room_type or packet.get("room_type"))
    if room_type:
        derived["room_type"] = room_type

    for key in ("style_hints", "functional_hints"):
        explicit = _clean_string_list(packet.get(key))
        if explicit:
            derived[key] = explicit

    for key in ("requested_categories", "excluded_categories", "avoid_categories"):
        explicit = _clean_category_list(packet.get(key))
        if explicit:
            derived[key] = explicit

    avoid = _clean_category_list(packet.get("avoid"))
    if avoid:
        derived["avoid_categories"] = avoid

    requested_items = _clean_requested_items(packet.get("requested_items"))
    if requested_items:
        derived["requested_items"] = requested_items
        requested_categories = list(derived.get("requested_categories") or [])
        for item in requested_items:
            category = item["canonical_category"]
            if category not in requested_categories:
                requested_categories.append(category)
        derived["requested_categories"] = requested_categories

    price_constraints = _clean_price_constraints(packet.get("price_constraints"))
    if price_constraints:
        derived["price_constraints"] = price_constraints

    attribute_constraints = _clean_attribute_constraints(
        packet.get("asset_attribute_constraints")
    )
    if attribute_constraints:
        derived["asset_attribute_constraints"] = attribute_constraints

    room_goal = _normalize_prompt(packet.get("room_goal"))
    if room_goal:
        derived["room_goal"] = room_goal
    derived["requires_straight_circulation_path"] = bool(
        packet.get("requires_straight_circulation_path")
    )
    fit_flexibility = _normalize_prompt(packet.get("fit_flexibility")).lower()
    if fit_flexibility in {"strict_counts", "balanced", "space_first", "flexible"}:
        derived["fit_flexibility"] = fit_flexibility
    interpretation_source = _normalize_prompt(packet.get("interpretation_source")).lower()
    if interpretation_source:
        derived["interpretation_source"] = interpretation_source
    return derived


def intent_prompt_text(packet: Mapping[str, Any] | None, fallback_intent: Any = "") -> str:
    if isinstance(packet, Mapping):
        normalized_prompt = _normalize_prompt(packet.get("normalized_prompt"))
        if normalized_prompt:
            return normalized_prompt
    return _normalize_prompt(fallback_intent)


def format_intent_packet_for_prompt(packet: Mapping[str, Any] | None) -> str:
    if not isinstance(packet, Mapping):
        return ""

    def _join(values: Any, empty: str) -> str:
        cleaned = _clean_string_list(values)
        return ", ".join(cleaned) if cleaned else empty

    room_type = _normalize_prompt(packet.get("room_type")) or "living_room"
    normalized_prompt = _normalize_prompt(packet.get("normalized_prompt")) or "None"
    room_goal = _normalize_prompt(packet.get("room_goal")) or "none detected"
    requires_path = bool(packet.get("requires_straight_circulation_path"))
    fit_flexibility = _normalize_prompt(packet.get("fit_flexibility")) or "balanced"

    return (
        "INTENT PACKET:\n"
        f"- normalized_prompt: {normalized_prompt}\n"
        f"- room_type: {room_type}\n"
        f"- room_goal: {room_goal}\n"
        f"- requested_categories: {_join(packet.get('requested_categories'), 'none detected')}\n"
        f"- excluded_categories: {_join(packet.get('excluded_categories'), 'none detected')}\n"
        f"- avoid_categories: {_join(packet.get('avoid_categories'), 'none detected')}\n"
        f"- style_hints: {_join(packet.get('style_hints'), 'none detected')}\n"
        f"- functional_hints: {_join(packet.get('functional_hints'), 'none detected')}\n"
        f"- requires_straight_circulation_path: {requires_path}\n"
        f"- requested_counts: {count_constraints_from_intent_packet(packet) or 'none detected'}\n"
        f"- price_constraints: {packet.get('price_constraints') or 'none detected'}\n"
        f"- asset_attribute_constraints: {packet.get('asset_attribute_constraints') or 'none detected'}\n"
        f"- fit_flexibility: {fit_flexibility}"
    )
