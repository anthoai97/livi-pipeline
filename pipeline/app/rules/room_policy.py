"""Room purpose shared by requests, planning, and catalog selection."""

from collections.abc import Mapping
import re
from typing import Any, Literal

RoomType = Literal["living_room", "bedroom", "dining_room", "studio"]
SUPPORTED_ROOM_TYPES = ("living_room", "bedroom", "dining_room", "studio")
STUDIO_FURNISHING_GUIDANCE = (
    "A studio holds three separate groups in one room: one complete standalone adult bed, a compact sofa or "
    "loveseat for sitting, and one dining table with two matching dining chairs; stated seat counts take precedence. "
    "A sofa never stands in for the bed, and dining chairs never stand in for the sofa. "
    "Choose smaller matching pieces before dropping a group. "
    "Bedside service, lighting and a sitting surface are suggested when space permits, without the accessories of "
    "three full rooms; a wardrobe, a TV with its support and a desk with a task chair are added only when requested. "
    "When space is short, keep the three groups and their access, drop optional accessories first, then the requested "
    "desk and chair, then the TV and its support, then the wardrobe, keeping each group whole. "
    "A TV stands on its stand; it is wall-mounted only when the brief asks. "
    "A TV serves the sofa unless the brief asks to watch from the bed or from both; the sofa and TV face each other, "
    "and for shared viewing the bed and sofa face the same TV from separate wall spans. "
    "Each rug, light, bedside piece and surface serves its own group. Reserve bed side and foot access, dining chair "
    "pull-out and clear entry walkways together. "
    "Plants and other finishing decor are optional; do not add clothing storage, dining storage, extra rugs or "
    "separate reading chairs as filler."
)
DINING_FURNISHING_GUIDANCE = (
    "A dining room centers on one standalone dining table with matching dining chairs as one usable group: four by "
    "default; stated seat counts take precedence. A space-first or fit-only brief may use a smaller usable group, "
    "never below a stated count. "
    "Use actual table and chair sizes, leaving room to sit, pull chairs out and pass behind them without blocking "
    "doors or walkways; choose a smaller table and chairs before dropping a requested function. "
    "Suggested, not required: one sideboard or dining cabinet when a usable wall and front access allow it, one "
    "pendant or chandelier over the table when its full model height and the ceiling allow it (prefer a shorter "
    "model; never assume a shorter hanger), and one piece or a matching pair of wall art. "
    "Minimal and space-first briefs, exclusions, the budget and stated seat counts come first; drop suggestions before "
    "the dining group or its access. The decor plant is still required. A rug, a mirror and a centerpiece are added "
    "only when requested, and a requested rug covers the table and the chairs as they move. "
    "Do not add a sofa, coffee table, bed, TV or desk to a generic dining room, or add furniture just to fill the "
    "floor or spend the budget."
)
BEDROOM_FURNISHING_GUIDANCE = (
    "A bedroom centers on one complete adult bed. A wardrobe, a TV with its support and a desk with a task chair are "
    "added only when requested; a generic bedroom brief requests none of them, and only a wardrobe, armoire or "
    "hanging-clothes request asks for a wardrobe. A TV stands on its stand; it is wall-mounted only when the brief "
    "asks. "
    "After the bed, plan useful bedside service (nightstands and lamps), clothing storage and clear walkways; these are "
    "suggested, not required. Then the requested wardrobe, the requested TV with its support, and the requested desk "
    "and chair, in that order. "
    "Use actual catalog sizes and the usable wall and access space, not the room area alone. When space is short, "
    "drop the whole desk and chair first, then the TV and its support, and the wardrobe last; never leave half a "
    "group, and a dresser or cabinet does not replace a requested wardrobe. Extra folded-clothes storage ranks below "
    "requested groups. "
    "Stated must-have items, priorities, exclusions and desk-only requests in the brief take precedence."
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
    """The fresh-room default (2 in a studio, else 4) yields to structured, user-stated seating counts."""
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
