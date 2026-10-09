"""Shared category taxonomy for planner and selection logic."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CANONICAL_CATEGORIES = (
    "sectional",
    "sofa",
    "loveseat",
    "sleeper_sofa",
    "sofa_bed",
    "chaise_lounge",
    "accent_chair",
    "dining_chair",
    "office_chair",
    "recliner",
    "swivel_chair",
    "coffee_table",
    "side_table",
    "console_table",
    "dining_table",
    "bar_table",
    "table",
    "desk",
    "tv_stand",
    "media_unit",
    "tv",
    "projector",
    "bed",
    "nightstand",
    "dresser",
    "wardrobe",
    "bookcase",
    "cabinet",
    "storage_unit",
    "sideboard",
    "ottoman",
    "bench",
    "stool",
    "rug",
    "lamp",
    "floor_lamp",
    "table_lamp",
    "floor_mirror",
    "wall_mirror",
    "wall_art",
    "painting",
    "pendant_light",
    "ceiling_light",
    "chandelier",
    "vase",
    "sculpture",
    "decor",
    "planter",
)

NON_SELLABLE_CATEGORIES = frozenset({"tv"})

CATEGORY_ALIASES = {
    "area_rug": "rug",
    "armoire": "wardrobe",
    "armoires": "wardrobe",
    "armchair": "accent_chair",
    "arm_chair": "accent_chair",
    "book_shelves": "bookcase",
    "bookshelf": "bookcase",
    "bookshelves": "bookcase",
    "benches": "bench",
    "carpet": "rug",
    "chair": "accent_chair",
    "console": "media_unit",
    "couch": "sofa",
    "couches": "sofa",
    "desk_lamp": "table_lamp",
    "end_table": "side_table",
    "entertainment_unit": "media_unit",
    "floor_lamps": "floor_lamp",
    "footstool": "ottoman",
    "lamp": "lamp",
    "lamps": "lamp",
    "lounge_chair": "accent_chair",
    "media_console": "media_unit",
    "media_storage": "media_unit",
    "mirror": "wall_mirror",
    "plant": "planter",
    "print": "wall_art",
    "pub_table": "bar_table",
    "runner": "rug",
    "sectional_sofa": "sectional",
    "shelf": "bookcase",
    "shelves": "bookcase",
    "standing_mirror": "floor_mirror",
    "storage": "storage_unit",
    "storage_organizer": "storage_unit",
    "storage_piece": "storage_unit",
    "storage_pieces": "storage_unit",
    "television": "tv",
    "television_stand": "tv_stand",
    "tv_console": "media_unit",
    "writing_desk": "desk",
    "wardrobes": "wardrobe",
}

# Canonical categories only: these sets are matched against the output of
# `normalize_category`, so an alias here is dead weight that also leaks into the
# feasibility digest's per-category caps under the wrong role.
ROLE_CATEGORIES = {
    "sleeping_anchor": {"bed"},
    "bedside": {"nightstand"},
    "anchor_seating": {
        "sofa",
        "sectional",
        "loveseat",
        "sleeper_sofa",
        "sofa_bed",
        "chaise_lounge",
    },
    "accent_seating": {
        "accent_chair",
        "dining_chair",
        "office_chair",
        "stool",
        "bench",
        "ottoman",
        "recliner",
        "swivel_chair",
    },
    "surface": {
        "coffee_table",
        "side_table",
        "dining_table",
        "desk",
        "table",
        "console_table",
    },
    "media": {"tv", "tv_stand", "media_unit", "projector"},
    "storage": {"bookcase", "cabinet", "storage_unit", "sideboard", "dresser", "wardrobe"},
    "lighting": {"floor_lamp", "lamp"},
    "tabletop": {"table_lamp", "vase", "sculpture", "decor"},
    "rug": {"rug"},
    "wall_or_ceiling": {
        "wall_art",
        "floor_mirror",
        "wall_mirror",
        "painting",
        "pendant_light",
        "ceiling_light",
        "chandelier",
    },
}

# Substring-matching keyword tuples for the geometry validators
# (matches_category_keywords semantics). Derived from anchor_seating with two
# deliberate deltas: chaise_lounge is anchor seating for planning but not a
# sofa for the sofa/table/media geometry rules; sectional_sofa is kept so the
# keyword set is unchanged (it is subsumed by "sectional" when matching).
SOFA_ROLE_KEYWORDS = tuple(
    sorted(ROLE_CATEGORIES["anchor_seating"] - {"chaise_lounge"})
) + ("sectional_sofa",)

# Chair keyword variants ("chair" subsumes every *_chair label); the
# stool/bench/ottoman accent categories are deliberately not reach-relevant
# seating for the validators.
SEATING_ROLE_KEYWORDS = SOFA_ROLE_KEYWORDS + (
    "accent_chair",
    "chair",
    "lounge_chair",
    "recliner",
    "swivel_chair",
    "arm_chair",
    "easy_chair",
)

DESK_ROLE_KEYWORDS = ("desk",)
DINING_CHAIR_ROLE_KEYWORDS = ("dining_chair",)
DINING_TABLE_ROLE_KEYWORDS = ("dining_table",)

COUNT_GUIDANCE_ROLE_CATEGORIES = {
    "sleeping_anchor": ROLE_CATEGORIES["sleeping_anchor"],
    "bedside": ROLE_CATEGORIES["bedside"],
    "anchor_seating": ROLE_CATEGORIES["anchor_seating"],
    "surface": {"coffee_table", "desk", "side_table"},
    "lighting": {"floor_lamp", "table_lamp", "lamp"},
    "rug": {"rug"},
    "media": {"tv_stand", "tv", "media_unit"},
    # Dining chairs belong to the dining cluster, not the lounge seating group.
    "accent_seating": ROLE_CATEGORIES["accent_seating"] - {"dining_chair"},
    "storage": ROLE_CATEGORIES["storage"],
}

COUNT_CONSTRAINT_EQUIVALENTS = {
    "lamp": {"floor_lamp", "lamp", "table_lamp"},
    "media_unit": {"media_unit", "tv_stand"},
    "painting": {"painting", "wall_art"},
    "wall_art": {"painting", "wall_art"},
    "storage_unit": {
        "bookcase",
        "cabinet",
        "dresser",
        "wardrobe",
        "sideboard",
        "storage_unit",
    },
}

ANCHOR_COUNT_EQUIVALENTS = ROLE_CATEGORIES["anchor_seating"]

_SIBLING_GROUPS = (ANCHOR_COUNT_EQUIVALENTS,) + tuple(
    COUNT_CONSTRAINT_EQUIVALENTS.values()
)


def sibling_categories(category: Any) -> set[str]:
    """Canonical category plus its interchangeable siblings (anchor seating,
    lamps, media, storage). Catalog-label aliasing is handled by
    `normalize_category`; this only widens across genuinely substitutable
    canonical categories."""
    normalized = normalize_category(category)
    if not normalized:
        return set()
    expanded = {normalized}
    for group in _SIBLING_GROUPS:
        if normalized in group:
            expanded |= group
    return expanded


def normalize_category(category: Any) -> str:
    """Canonical category for a catalog label: lowercase snake case, aliases, and plural forms."""
    normalized = (
        str(category or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    if not normalized:
        return ""
    if normalized in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[normalized]
    if normalized in CANONICAL_CATEGORIES:
        return normalized
    if normalized.endswith("s"):
        singular = normalized[:-1]
        if singular in CATEGORY_ALIASES:
            return CATEGORY_ALIASES[singular]
        if singular in CANONICAL_CATEGORIES:
            return singular
    return normalized


def selection_category(asset: Mapping[str, Any]) -> str:
    """Counting category for a selected asset. Catalog items filed under the
    generic `chair` category are split into dining/office chairs by their
    text before the normal `chair` -> `accent_chair` alias applies."""
    raw = str(asset.get("category") or "").strip().lower()
    if raw == "chair":
        text = " ".join(
            str(asset.get(key) or "").lower()
            for key in ("title", "description")
        )
        if "dining chair" in text:
            return "dining_chair"
        if any(
            term in text
            for term in ("desk chair", "office chair", "task chair")
        ):
            return "office_chair"
    return normalize_category(raw)
