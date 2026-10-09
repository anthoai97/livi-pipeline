from collections.abc import Mapping
from typing import Any
from app.rules.room_policy import BEDROOM_FURNISHING_GUIDANCE, DINING_FURNISHING_GUIDANCE, STUDIO_FURNISHING_GUIDANCE, dining_chair_count, normalize_room_type, excluded_room_categories

from ._util import clean_list as _clean_list, room_scale as _room_scale, round4 as _round
from .fit_policy import (
    CATEGORY_CAPS,
    ROLE_CAPS,
    ROOM_THRESHOLDS,
    SECTIONAL_MIN_ROOM,
)
from .taxonomy import ROLE_CATEGORIES as TAXONOMY_ROLE_CATEGORIES, sibling_categories

ROLE_CATEGORIES = {
    "sleeping_anchor": TAXONOMY_ROLE_CATEGORIES["sleeping_anchor"],
    "bedside": TAXONOMY_ROLE_CATEGORIES["bedside"],
    "anchor_seating": TAXONOMY_ROLE_CATEGORIES["anchor_seating"],
    "surface": TAXONOMY_ROLE_CATEGORIES["surface"],
    "lighting": TAXONOMY_ROLE_CATEGORIES["lighting"],
    "media": TAXONOMY_ROLE_CATEGORIES["media"],
    "storage": TAXONOMY_ROLE_CATEGORIES["storage"],
    "tabletop": TAXONOMY_ROLE_CATEGORIES["tabletop"],
    "wall_or_ceiling": TAXONOMY_ROLE_CATEGORIES["wall_or_ceiling"],
    "accent": (
        TAXONOMY_ROLE_CATEGORIES["accent_seating"]
        | TAXONOMY_ROLE_CATEGORIES["rug"]
        | {"planter"}
    ),
}


def _format_count_mapping(values: Any, *, limit: int) -> str:
    if not isinstance(values, Mapping):
        return "none"
    parts = [
        f"{key}={int(value)}"
        for key, value in sorted(values.items())
        if isinstance(value, (int, float)) and int(value) > 0
    ]
    return ", ".join(parts[:limit]) or "none"


def _opening_pressure_level(door_count: int, window_count: int, anchor_blocked_ratio: float) -> str:
    total = door_count + window_count
    if door_count >= 2 or total >= 4 or anchor_blocked_ratio >= 0.3:
        return "high"
    if total >= 2 or anchor_blocked_ratio >= 0.15:
        return "medium"
    return "low"


def _anchor_capacity(longest_clear_span: float) -> str:
    if longest_clear_span < 1.8:
        return "constrained"
    if longest_clear_span < 2.4:
        return "moderate"
    return "generous"


def _category_role(category: str) -> str:
    if category in TAXONOMY_ROLE_CATEGORIES["rug"]:
        return "rug"
    for role, categories in ROLE_CATEGORIES.items():
        if category in categories:
            return "accent_seating" if role == "accent" else role
    return "other"


def _max_counts_by_category(
    *,
    room_scale: str,
    blocked_categories: set[str],
    opening_pressure: str,
) -> dict[str, int]:
    categories = sorted(
        set().union(*ROLE_CATEGORIES.values())
    )
    caps: dict[str, int] = {}
    for category in categories:
        if category in blocked_categories:
            caps[category] = 0
            continue
        role = _category_role(category)
        cap = CATEGORY_CAPS.get(room_scale, {}).get(
            category,
            ROLE_CAPS.get(room_scale, {}).get(role, 2),
        )
        if opening_pressure == "high" and role in {"storage", "media"}:
            cap = min(cap, 1)
        caps[category] = int(cap)
    return caps


def _density_budget(*, room_scale: str, available_furnishing_area_sqm: float) -> dict[str, Any]:
    thresholds = ROOM_THRESHOLDS.get(room_scale, ROOM_THRESHOLDS["medium"])
    return {
        "comfortable_max_score": thresholds["comfortable"],
        "overcrowded_min_score": thresholds["overcrowded"],
        "sparse_below_score": thresholds["sparse"],
        "comfortable_load_sqm": _round(available_furnishing_area_sqm * thresholds["comfortable"]),
        "overcrowded_load_sqm": _round(available_furnishing_area_sqm * thresholds["overcrowded"]),
        "sparse_below_load_sqm": _round(available_furnishing_area_sqm * thresholds["sparse"]),
    }


def _room_completion_guidance(
    *,
    room_scale: str,
    opening_pressure: str,
    optional_categories: list[str],
) -> dict[str, Any]:
    if room_scale == "compact":
        strategy = "essential_core"
        zone_count = 1
        notes = ["Keep furniture count low and prioritize circulation."]
    elif room_scale == "medium":
        strategy = "balanced_core"
        zone_count = 1
        notes = ["Use a complete seating group with a few supporting accents."]
    else:
        strategy = "full_room"
        zone_count = 1
        notes = ["Use one complete seating group and avoid separate room zones."]

    if opening_pressure == "high":
        notes.append("Openings reduce usable wall frontage; avoid wall-heavy additions.")

    return {
        "strategy": strategy,
        "zone_count": zone_count,
        "preferred_optional_categories": optional_categories[:8],
        "notes": notes,
    }


def _group(role: str, categories: set[str], excluded: set[str], blocked: set[str]) -> dict[str, Any] | None:
    active = sorted(category for category in categories if category not in excluded and category not in blocked)
    if not active:
        return None
    return {
        "role": role,
        "categories": active,
    }


def build_feasibility_digest(
    *,
    room_facts: Mapping[str, Any],
    intent_packet: Mapping[str, Any],
) -> dict[str, Any]:
    room_type = normalize_room_type(intent_packet.get("room_type"))
    bedroom = room_type == "bedroom"
    dining = room_type == "dining_room"
    studio = room_type == "studio"
    summary = room_facts.get("summary") or {}
    opening_counts = summary.get("opening_counts") or {}
    area_sqm = float(summary.get("area") or room_facts.get("area") or 0.0)
    door_count = int(opening_counts.get("doors") or 0)
    window_count = int(opening_counts.get("windows") or 0)
    room_scale = _room_scale(area_sqm)

    blocked_zones = room_facts.get("blocked_zones") or []
    door_clearance_area_sqm = sum(
        float(zone.get("width") or 0.0) * float(zone.get("depth") or 0.0)
        for zone in blocked_zones
        if zone.get("reason") == "door_clearance"
    )
    available_furnishing_area_sqm = max(0.0, area_sqm - door_clearance_area_sqm)

    anchor_walls = room_facts.get("candidate_anchor_walls") or []
    anchor_wall = anchor_walls[0] if anchor_walls else {}
    best_anchor_wall = str(anchor_wall.get("wall") or "")
    largest_clear_wall_span = float(anchor_wall.get("usable_length") or 0.0)
    anchor_blocked_ratio = float(anchor_wall.get("blocked_ratio") or 0.0)
    opening_pressure = _opening_pressure_level(door_count, window_count, anchor_blocked_ratio)

    requested = set(_clean_list(intent_packet.get("requested_categories")))
    excluded = set(_clean_list(intent_packet.get("excluded_categories")))
    functional_hints = set(_clean_list(intent_packet.get("functional_hints")))
    style_hints = set(_clean_list(intent_packet.get("style_hints")))

    blocked_categories: set[str] = set(excluded)
    if bedroom or dining or studio:
        blocked_categories.update(excluded_room_categories(room_type))
    discouraged_categories: set[str] = set()
    optional_categories: list[str] = []
    notes: list[str] = []
    risks: list[str] = []

    min_sectional_w, min_sectional_d = SECTIONAL_MIN_ROOM
    sectional_supported = (
        (room_facts.get("width", 0) >= min_sectional_w and room_facts.get("depth", 0) >= min_sectional_d)
        or (room_facts.get("depth", 0) >= min_sectional_w and room_facts.get("width", 0) >= min_sectional_d)
    )
    if not sectional_supported and not dining:
        blocked_categories.add("sectional")
        notes.append("Room is below the preferred sectional footprint; keep anchor seating smaller.")

    if opening_pressure == "high":
        discouraged_categories.update({"media_unit", "tv_stand", "bookcase", "cabinet", "storage_unit"})
        risks.append("High opening pressure reduces clean wall frontage and makes wall-heavy layouts harder.")
    elif opening_pressure == "medium":
        discouraged_categories.update({"media_unit"})

    if largest_clear_wall_span < 2.0 and not dining:
        discouraged_categories.update({"sofa", "sectional"})
        risks.append("Best anchor wall is short; oversized anchor seating may force awkward placement.")

    required_roles: list[str] = []
    required_groups: list[dict[str, Any]] = []

    anchor_role = "sleeping_anchor" if bedroom or studio else "dining_table" if dining else "anchor_seating"
    anchor_categories = {"dining_table"} if dining else ROLE_CATEGORIES[anchor_role]
    anchor_group = _group(anchor_role, anchor_categories, excluded, blocked_categories)
    if anchor_group:
        required_roles.append(anchor_role)
        required_groups.append(anchor_group)
    if studio:
        for role, categories in (("anchor_seating", {"sofa", "loveseat"}), ("dining_table", {"dining_table"})):
            group = _group(role, categories, excluded, blocked_categories)
            if group:
                required_roles.append(role)
                required_groups.append(group)
    if dining or studio:
        dining_seating = _group("dining_seating", {"dining_chair"}, excluded, blocked_categories)
        if dining_seating:
            required_roles.append("dining_seating")
            required_groups.append(dining_seating)

    surface_requested = not dining and not studio and (
        bool(requested & ROLE_CATEGORIES["surface"])
        or "conversation" in functional_hints
        or ("tv_focused" in functional_hints and not bedroom)
        or (bool(anchor_group) and not bedroom)
    )
    if surface_requested:
        surface_group = _group("surface", ROLE_CATEGORIES["surface"], excluded, blocked_categories)
        if surface_group:
            required_roles.append("surface")
            required_groups.append(surface_group)

    media_requested = bool(requested & ROLE_CATEGORIES["media"]) or "tv_focused" in functional_hints
    if media_requested:
        media_group = _group("media", ROLE_CATEGORIES["media"], excluded, blocked_categories)
        if media_group:
            required_roles.append("media")
            required_groups.append(media_group)
        elif requested & ROLE_CATEGORIES["media"]:
            risks.append("TV/media intent is present, but the current room constraints make media support difficult.")

    lighting_requested = bool(requested & ROLE_CATEGORIES["lighting"]) or "reading" in functional_hints
    if lighting_requested:
        lighting_group = _group("lighting", ROLE_CATEGORIES["lighting"], excluded, blocked_categories)
        if lighting_group:
            required_roles.append("lighting")
            required_groups.append(lighting_group)

    if room_scale == "compact":
        optional_categories.extend(["rug", "floor_lamp", "ottoman"])
        discouraged_categories.update({"bookcase", "cabinet"})
    elif room_scale == "medium":
        optional_categories.extend(["rug", "accent_chair", "floor_lamp", "planter"])
    else:
        optional_categories.extend(
            ["rug", "accent_chair", "floor_lamp", "planter", "bookcase", "cabinet", "storage_unit"]
        )

    if "reading" in functional_hints:
        optional_categories.extend(["accent_chair", "side_table", "floor_lamp"])
    if "storage" in functional_hints:
        optional_categories.extend(["bookcase", "cabinet", "storage_unit"])
    if {"cozy", "luxury"} & style_hints:
        optional_categories.extend(["rug", "planter", "floor_lamp"])
    if bedroom:
        optional_categories = ["nightstand", "dresser", "table_lamp", "rug", "planter"]
        if "wardrobe" in requested:
            optional_categories.append("wardrobe")
        if room_scale == "spacious":
            optional_categories.extend(["bench", "accent_chair", "floor_lamp", "floor_mirror"])
    if dining:
        optional_categories = sorted(requested - {"dining_table", "dining_chair"})
        optional_categories.insert(0, "planter")
        if intent_packet.get("fit_flexibility") != "space_first" and "minimal_furniture" not in functional_hints:
            excluded_optional = set().union(*(sibling_categories(category) for category in excluded))
            optional_categories.extend(
                category for category in (
                    "sideboard", "cabinet", "pendant_light", "chandelier", "painting", "wall_art",
                ) if category not in excluded_optional
            )
    if studio:
        optional_categories = ["nightstand", "table_lamp", "side_table", "planter"]
        optional_categories.extend(sorted(requested - {"bed", "sofa", "loveseat", "dining_table", "dining_chair"}))

    optional_categories = [
        category
        for category in _clean_list(optional_categories)
        if category not in blocked_categories and category not in excluded
    ]
    discouraged_categories -= blocked_categories
    discouraged_categories -= excluded
    max_counts_by_category = _max_counts_by_category(
        room_scale=room_scale,
        blocked_categories=blocked_categories,
        opening_pressure=opening_pressure,
    )
    if not bedroom and not studio:
        max_counts_by_category.pop("bed", None)
        max_counts_by_category.pop("nightstand", None)
    if dining or studio:
        max_counts_by_category["dining_table"] = 1
        max_counts_by_category["dining_chair"] = dining_chair_count(intent_packet)
    density_budget = _density_budget(
        room_scale=room_scale,
        available_furnishing_area_sqm=available_furnishing_area_sqm,
    )
    if dining or studio:
        # Dining completeness is table capacity and usable seats, not floor fill.
        density_budget["sparse_below_load_sqm"] = 0.0
    room_completion_guidance = _room_completion_guidance(
        room_scale=room_scale,
        opening_pressure=opening_pressure,
        optional_categories=optional_categories,
    )
    if bedroom:
        room_completion_guidance["notes"] = [
            "Use one complete adult bed as the sleeping anchor and design a complete bedroom around its everyday functions: useful bedside surfaces, lighting and clothing storage.",
            "For a normal spacious bedroom, prefer a coordinated bedside pair when both sides are usable, and purposeful clothing storage. Consider a dressing or reading area when it improves the room's composition; explain the purpose of each addition.",
            BEDROOM_FURNISHING_GUIDANCE,
            "The density floor only catches extreme sparsity; a bed can satisfy most of it without making the bedroom complete. Assess the whole room's usable zones before stopping.",
            "These are design choices, not fixed counts. Explicit single-bedside, no-storage, sparse or essentials-only requests take priority. In compact rooms, prioritize access and the most useful bedside support; do not force a pair or a secondary zone.",
            "Wardrobes/armoires are clothing-storage assets. Dressers store folded clothes; generic cabinets and bookcases do not establish hanging-clothes capability. Claim hanging storage only when the selected product's metadata confirms it, and explain unavailable functions.",
            "Preserve bed-side and foot access, opening clearance, and storage-front access.",
        ]
    if dining:
        room_completion_guidance["notes"] = [DINING_FURNISHING_GUIDANCE]
    if studio:
        room_completion_guidance.update(strategy="three_function_groups", zone_count=3,
                                        notes=[STUDIO_FURNISHING_GUIDANCE])

    if best_anchor_wall:
        notes.append(
            f"Best anchor wall is `{best_anchor_wall}` with {largest_clear_wall_span:.2f}m usable span."
        )
    if available_furnishing_area_sqm < 12.0:
        risks.append("Available furnishing area is tight; prioritize fewer, more essential pieces.")
    if media_requested and opening_pressure != "low":
        risks.append("Media layouts may compete with openings and circulation.")

    return {
        "version": 1,
        "room_type": room_type,
        "room_scale": room_scale,
        "room_area_sqm": _round(area_sqm),
        "door_clearance_area_sqm": _round(door_clearance_area_sqm),
        "available_furnishing_area_sqm": _round(available_furnishing_area_sqm),
        "anchor_wall": {
            "wall": best_anchor_wall or None,
            "usable_length": _round(largest_clear_wall_span),
            "blocked_ratio": _round(anchor_blocked_ratio),
            "capacity": _anchor_capacity(largest_clear_wall_span),
        },
        "opening_pressure": {
            "level": opening_pressure,
            "door_count": door_count,
            "window_count": window_count,
            "total_openings": door_count + window_count,
        },
        "required_roles": required_roles,
        "required_category_groups": required_groups,
        "optional_categories": optional_categories,
        "max_counts_by_category": max_counts_by_category,
        "density_budget": density_budget,
        "room_completion_guidance": room_completion_guidance,
        "blocked_categories": sorted(blocked_categories),
        "discouraged_categories": sorted(discouraged_categories),
        "notes": notes,
        "risks": risks,
    }


def format_feasibility_digest_for_prompt(digest: Mapping[str, Any] | None) -> str:
    if not isinstance(digest, Mapping):
        return ""

    anchor = digest.get("anchor_wall") or {}
    opening_pressure = digest.get("opening_pressure") or {}
    required_roles = ", ".join(_clean_list(digest.get("required_roles"))) or "none"
    optional_categories = ", ".join(_clean_list(digest.get("optional_categories"))[:8]) or "none"
    blocked_categories = ", ".join(_clean_list(digest.get("blocked_categories"))) or "none"
    discouraged_categories = ", ".join(_clean_list(digest.get("discouraged_categories"))) or "none"
    notes = " | ".join(_clean_list(digest.get("notes"))[:3]) or "none"
    risks = " | ".join(_clean_list(digest.get("risks"))[:3]) or "none"
    max_counts = _format_count_mapping(digest.get("max_counts_by_category"), limit=12)
    density_budget = digest.get("density_budget") or {}
    completion = digest.get("room_completion_guidance") or {}
    completion_notes = " | ".join(_clean_list(completion.get("notes"))[:2]) or "none"

    group_lines = []
    for group in digest.get("required_category_groups") or []:
        if not isinstance(group, Mapping):
            continue
        categories = ", ".join(_clean_list(group.get("categories"))) or "none"
        role = str(group.get("role") or "role")
        group_lines.append(f"- {role}: {categories}")
    groups_text = "\n".join(group_lines) or "- none"

    return (
        "FEASIBILITY DIGEST:\n"
        f"- room_scale: {digest.get('room_scale')}\n"
        f"- available_furnishing_area_sqm: {float(digest.get('available_furnishing_area_sqm') or 0.0):.2f}\n"
        f"- anchor_wall: {anchor.get('wall') or 'none'} "
        f"({float(anchor.get('usable_length') or 0.0):.2f}m, "
        f"{anchor.get('capacity') or 'unknown'})\n"
        f"- opening_pressure: {opening_pressure.get('level') or 'unknown'}\n"
        f"- required_roles: {required_roles}\n"
        f"- optional_categories: {optional_categories}\n"
        f"- max_counts_by_category: {max_counts}\n"
        f"- density_budget: floor>={float(density_budget.get('sparse_below_load_sqm') or 0.0):.2f} sqm, "
        f"comfortable<={float(density_budget.get('comfortable_load_sqm') or 0.0):.2f} sqm, "
        f"overcrowded>={float(density_budget.get('overcrowded_load_sqm') or 0.0):.2f} sqm\n"
        f"- completion_strategy: {completion.get('strategy') or 'unknown'} "
        f"({int(completion.get('zone_count') or 0)} zone(s)); {completion_notes}\n"
        f"- blocked_categories: {blocked_categories}\n"
        f"- discouraged_categories: {discouraged_categories}\n"
        f"- notes: {notes}\n"
        f"- risks: {risks}\n"
        "REQUIRED CATEGORY GROUPS:\n"
        f"{groups_text}"
    )
