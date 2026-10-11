"""Room purpose shared by requests, planning, and catalog selection."""

from collections.abc import Mapping
import re
from typing import Any, Literal

from app.rules.layout.bedroom import BED_TWO_SIDED_MIN_WIDTH_M

RoomType = Literal["living_room", "bedroom", "dining_room", "studio"]
SUPPORTED_ROOM_TYPES = ("living_room", "bedroom", "dining_room", "studio")
STUDIO_DINING_MIN_AREA_SQM = 25.0
STUDIO_FURNISHING_GUIDANCE = (
    "A studio has a sleeping group, one complete standalone bed, and a sitting group, a compact sofa or loveseat; "
    "a sofa never stands in for the bed. "
    f"It adds a dining table with two matching dining chairs when the brief asks for one or the studio is "
    f"{STUDIO_DINING_MIN_AREA_SQM:.0f} m2 or more; stated seat counts take precedence, and dining chairs never stand in "
    "for the sofa. "
    "The bed needs a nightstand and a bedside lamp, and a medium or spacious studio a dresser or wardrobe, "
    "unless the brief excludes them or asks for minimal or space-first furniture. "
    "A coffee or side table, floor lamp, rug per group, bookcase, storage, overhead light and plant are optional; "
    "a wardrobe, a TV with its support and a desk with a task chair are added only when requested. "
    "A TV serves the sofa unless the brief asks to watch from the bed or from both. "
    "When space is short, drop optional pieces first, then the requested desk and chair, then the TV and its support, "
    "then the wardrobe, and keep the bed, sofa, dining group and their access."
)
DINING_FURNISHING_GUIDANCE = (
    "A dining room centers on one standalone dining table with matching dining chairs: four by default, "
    "up to six in a spacious room when the table seats them; stated seat counts take precedence. "
    "A medium or spacious dining room also needs a sideboard or cabinet and a pendant or chandelier over the table, "
    "unless the brief excludes them or asks for minimal or space-first furniture. "
    "A plant is required; wall art, a mirror and a rug are optional, and a bar cart or bench is added only when requested. "
    "Do not add a sofa, coffee table, bed, TV or desk unless the brief asks for one. "
    "Leave room to sit, pull chairs out and pass behind them, without blocking doors or walkways. "
    "When space is short, drop optional pieces first, then choose a smaller table and chairs; never cut the seat count."
)
BEDROOM_FURNISHING_GUIDANCE = (
    "A bedroom centers on one complete adult bed with a nightstand and a bedside lamp; "
    f"a bed {BED_TWO_SIDED_MIN_WIDTH_M:.1f} m wide or more gets a nightstand on each side unless the room is compact. "
    "A medium or spacious bedroom also needs a dresser or wardrobe, unless the brief excludes them or asks for "
    "minimal or space-first furniture. "
    "A rug and a plant are required; a bench, accent chair, floor lamp, mirror and wall art are optional. "
    "A wardrobe for hanging clothes, a TV with its support and a desk with a task chair are added only when requested, "
    "and a dresser does not replace a requested wardrobe. "
    "When space is short, drop optional pieces first, then the desk and chair, then the TV and its support, "
    "then the wardrobe, keeping each group whole. "
    "Stated must-have items, exclusions and priorities in the brief take precedence."
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


def dining_chair_count(intent_packet: Mapping[str, Any], room_scale: str) -> int:
    """The fresh-room default (2 in a studio, 6 in a spacious dining room, else 4) yields to
    structured, user-stated seating counts."""
    counts = [
        int(item.get("count") or 0)
        for item in intent_packet.get("requested_items") or []
        if isinstance(item, Mapping)
        and item.get("canonical_category") in {"dining_chair", "chair"}
        and (item.get("exact") or int(item.get("count") or 0) > 1)
    ]
    if counts:
        return sum(counts)
    if intent_packet.get("room_type") == "studio":
        return 2
    return 6 if room_scale == "spacious" else 4


def studio_dining_required(intent_packet: Mapping[str, Any], area_sqm: float) -> bool:
    """A studio needs its dining group when the brief asks for a dining table or chairs, or
    the studio is STUDIO_DINING_MIN_AREA_SQM or more; a smaller one may eat at a counter."""
    requested = set(intent_packet.get("requested_categories") or [])
    return bool(requested & {"dining_table", "dining_chair"}) or area_sqm >= STUDIO_DINING_MIN_AREA_SQM


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
