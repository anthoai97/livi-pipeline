"""Room purpose shared by requests, planning, and catalog selection."""

from collections.abc import Mapping
import re
from typing import Any, Literal

RoomType = Literal["living_room", "bedroom", "dining_room", "studio"]
SUPPORTED_ROOM_TYPES = ("living_room", "bedroom", "dining_room", "studio")
STUDIO_FURNISHING_GUIDANCE = (
    "A studio combines three separate usable groups in one room: exactly one standalone complete adult bed, "
    "a compact sofa/loveseat for sitting, and one dining table with two matching dining chairs by default. "
    "Explicit seating counts take precedence. A sofa never substitutes for the bed, and dining chairs never "
    "substitute for the separate sitting group. Choose compact compatible assets before dropping a function. "
    "Allow useful bedside service, lighting and a sitting surface when space permits; do not combine all "
    "the accessory defaults of three full rooms. Wardrobes, TV and desk/task-chair setups are opt-in. "
    "Preserve the three core groups and circulation, remove optional accessories first, then prioritize "
    "requested wardrobe, TV with its support, and desk/task chair in that order unless the user says otherwise. "
    "Keep optional functional groups complete and explain omissions using measured fit evidence. "
    "If the three core groups cannot fit, report infeasibility rather than silently losing a group. "
    "A TV serves sitting by default; explicit bed or shared viewing requests change its viewing targets. "
    "Plan the sofa and TV/support facing each other with opposing headings; for shared viewing place bed and sofa "
    "in separate usable spans facing the same media group. "
    "Each rug, light, bedside and surface serves its own group; keep all other groups as access obstacles. "
    "Reserve bed side/foot access, dining-chair pull-out and clear entrance routes together. "
    "Plants and other finishing decor are optional. Do not require clothing storage, dining storage, multiple rugs or separate reading chairs as filler. "
    "Targeted existing-room edits preserve unrelated furniture and never reapply fresh-design defaults."
)
DINING_FURNISHING_GUIDANCE = (
    "Use exactly one standalone dining table with compatible dining chairs as one usable group. "
    "For a fresh dining room, provide four matching chairs when the user has not specified a seat count; "
    "explicit seating quantities take precedence. For an explicitly space-first or fit-only brief, "
    "a smaller usable seating group may replace the default four, with the reason explained; never "
    "reduce a mandatory user count. Use actual table and chair dimensions, allowing "
    "space to sit, pull chairs out and pass behind them without blocking doors or circulation. "
    "Prefer a smaller compatible table/chair set before dropping a requested function. "
    "For a normal fresh dining room, consider one sideboard/buffet or suitable dining-storage cabinet "
    "when usable wall space and front access permit, one pendant/chandelier over the table when "
    "the model dimensions and ceiling height permit, and modest wall art: one piece or a coherent pair "
    "on usable walls. These are conditional design suggestions, never mandatory item counts or quotas. "
    "For overhead lighting, use the full model height against the ceiling and tabletop heights; "
    "prefer a shorter fitting model and never assume an unknown hanger can be shortened. "
    "Honor explicit minimal-furniture and space-first briefs, exclusions, budget and mandatory seating "
    "first; omit optional additions before compromising the dining group or its access. Keep the "
    "normal decor-plant policy. Rugs, mirrors and tabletop centerpieces remain opt-in; a requested "
    "rug must cover the dining group including chair movement. "
    "Never add a sofa, coffee table, bed, media group or workstation to complete a generic dining room. "
    "Do not add furniture merely to fill floor area or spend the budget. Explain unavailable or "
    "infeasible requests instead of silently changing explicit seat counts. Existing-room edits "
    "preserve unrelated furniture; never reapply fresh-design chairs, storage, lighting or art defaults "
    "as additional edit requests."
)
BEDROOM_FURNISHING_GUIDANCE = (
    "Wardrobes, TV and desk/task-chair setups are opt-in for a fresh bedroom. "
    "Select a wardrobe only when the user requests a wardrobe/armoire or hanging-clothes storage; "
    "a generic bedroom brief does not request one. After the bed, useful bedside service and clear circulation, "
    "prioritize requested wardrobe, then requested TV with its media support, then requested desk with task chair. "
    "Use actual catalog dimensions and usable wall/access space, not room area alone. "
    "If space is insufficient, omit the entire desk/chair group first, then TV and its dedicated "
    "support, and omit the wardrobe only if it still cannot fit. Never leave half a functional "
    "group or replace a requested wardrobe with a dresser or generic cabinet. Extra folded-clothes "
    "storage has lower priority than requested functional groups. "
    "If a requested wardrobe cannot fit or is unavailable, explicitly explain that omission in "
    "gaps; never silently skip it. Explain other omitted groups as well. Explicit must-have items, "
    "different user priorities, exclusions and desk-only requests take precedence. "
    "Targeted chat edits preserve unrelated existing furniture; never remove it just to follow "
    "this fresh-design priority."
)
# Highest to lowest retention priority; used only for fit recommendations.
BEDROOM_FIT_GROUPS = (
    ("wardrobe",),
    ("tv", "tv_stand", "media_unit", "media_console", "console_table"),
    ("desk", "office_chair"),
)
DEFERRED_SLEEP_CATEGORIES = frozenset({
    "bed_frame", "bunk_bed", "headboard", "mattress", "bedding", "bedding_inserts",
})
_DEFERRED_BED_TYPE = re.compile(r"\b(?:day[\s_-]*beds?|bunk[\s_-]*beds?)\b", re.IGNORECASE)
_BUNDLED_BEDSIDE = re.compile(
    r"\b(?:integrated|built[\s_-]*in|attached|includes?|including|comes\s+with)\s+"
    r"(?:(?:floating|matching|wooden|two|a|pair\s+of)\s+){0,3}"
    r"(?:nightstands?|bedside\s+tables?)\b",
    re.IGNORECASE,
)


def normalize_room_type(value: Any = None) -> RoomType:
    room_type = str(value or "living_room").strip().lower().replace(" ", "_").replace("-", "_")
    if room_type not in SUPPORTED_ROOM_TYPES:
        raise ValueError(f"Unsupported room type: {value}")
    return room_type


def excluded_room_categories(room_type: str) -> set[str]:
    excluded = set(DEFERRED_SLEEP_CATEGORIES)
    if normalize_room_type(room_type) not in {"bedroom", "studio"}:
        excluded.add("bed")
        if normalize_room_type(room_type) == "dining_room":
            excluded.add("daybed")
    else:
        excluded.add("daybed")
    return excluded


def dining_chair_count(intent_packet: Mapping[str, Any]) -> int:
    """The fresh-room default yields to structured, user-stated seating counts."""
    counts = [
        int(item.get("count") or 0)
        for item in intent_packet.get("requested_items") or []
        if isinstance(item, Mapping)
        and item.get("canonical_category") in {"dining_chair", "chair"}
        and (item.get("exact") or int(item.get("count") or 0) > 1)
    ]
    return sum(counts) if counts else (2 if intent_packet.get("room_type") == "studio" else 4)


def studio_viewing_assets(assets: list[dict[str, Any]], intent_packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Carry structured viewing intent with the media group through saved edits."""
    if intent_packet.get("room_type") != "studio":
        return assets
    hints = set(intent_packet.get("functional_hints") or [])
    target = ("shared" if "tv_shared_viewing" in hints else "bed" if "tv_from_bed" in hints
              else "sitting" if "tv_from_sitting" in hints else None)
    return [
        {**asset, "viewing_target": target or asset.get("viewing_target") or "sitting"}
        if str(asset.get("category") or "") in {"tv", "tv_stand", "media_unit", "media_console", "console_table"}
        else asset
        for asset in assets
    ]


def is_eligible_bed_asset(asset: Mapping[str, Any]) -> bool:
    """Reject explicit nonstandard beds and bedside bundles, not headboard mentions.

    Catalog category and descriptions are evidence, not proof of completeness;
    exterior dimensions alone cannot identify mattress size or a daybed.
    """
    if str(asset.get("category") or "").strip().lower() != "bed":
        return False
    item_text = " ".join(str(asset.get(key) or "") for key in (
        "name", "title", "item_type", "product_type", "asset_type",
        "description", "asset_description",
    ))
    return (
        _DEFERRED_BED_TYPE.search(item_text) is None
        and _BUNDLED_BEDSIDE.search(item_text) is None
    )


def asset_is_eligible_for_room(asset: Mapping[str, Any], room_type: str) -> bool:
    category = str(asset.get("category") or "").strip().lower()
    if category in excluded_room_categories(room_type):
        return False
    return category != "bed" or is_eligible_bed_asset(asset)
