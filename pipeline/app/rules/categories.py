"""Asset category vocabulary and classification predicates."""

import re

# Categories whose support surface is the seat cushion, not the full asset top.
# A pillow declared on_top_of a sofa should rest on the seat (~0.42m),
# not on the backrest top (~0.89m).
SEAT_HEIGHT_CATEGORIES = (
    "sofa",
    "sectional",
    "sectional_sofa",
    "loveseat",
    "sleeper_sofa",
    "sofa_bed",
    "chaise_lounge",
    "daybed",
    "accent_chair",
    "chair",
    "lounge_chair",
    "recliner",
    "swivel_chair",
    "arm_chair",
    "rocking_chair",
    "glider",
    "ottoman",
    "pouf",
    "bench",
    "banquette",
    "settee",
    "bed",
)
SEAT_HEIGHT_M = 0.42


def support_top_z(parent_asset: dict, parent_z: float) -> float:
    """Z of the top support surface of `parent_asset` placed at `parent_z`."""
    category = normalize_asset_label(parent_asset.get("category", ""))
    height = float(parent_asset.get("height", 0.0) or 0.0)
    if category in SEAT_HEIGHT_CATEGORIES:
        return float(parent_z) + min(SEAT_HEIGHT_M, height or SEAT_HEIGHT_M)
    return float(parent_z) + height


# Categories that must always stay on the floor (z=0)
FLOOR_ONLY_CATEGORIES = (
    "rug",
    "runner",
    "sofa",
    "sectional",
    "loveseat",
    "sleeper_sofa",
    "sofa_bed",
    "chaise_lounge",
    "daybed",
    "bed",
    "accent_chair",
    "chair",
    "lounge_chair",
    "recliner",
    "swivel_chair",
    "ottoman",
    "bench",
    "banquette",
    "stool",
    "coffee_table",
    "dining_table",
    "bar_table",
    "console_table",
    "side_table",
    "desk",
    "nightstand",
    "tv_stand",
    "cabinet",
    "bookcase",
    "dresser",
    "wardrobe",
    "armoire",
    "shelf",
    "sideboard",
    "storage_unit",
    "media_unit",
    "headboard",
    "floor_lamp",
    "floor_mirror",
    "modular",
)

FLOOR_LAMP_CATEGORIES = (
    "floor_lamp",
    "floor_lamps",
)

TABLE_LAMP_CATEGORIES = (
    "desk_lamp",
    "table_lamp",
    "table_lamps",
)

TABLE_LAMP_SUPPORT_CATEGORIES = (
    "console_table",
    "desk",
    "end_table",
    "nightstand",
    "side_table",
    "side_tables",
)

TABLETOP_SUPPORT_CATEGORIES = (
    *TABLE_LAMP_SUPPORT_CATEGORIES,
    "book_shelf",
    "book_shelves",
    "bookcase",
    "bookshelf",
    "bookshelves",
    "cabinet",
    "coffee_table",
    "console",
    "dining_table",
    "dresser",
    "media_console",
    "media_unit",
    "shelf",
    "shelving_unit",
    "sideboard",
    "table",
    "tv_stand",
)

TABLETOP_PRIMARY_SUPPORT_CATEGORIES = (
    "coffee_table",
)

TABLETOP_SECONDARY_SUPPORT_CATEGORIES = (
    "console_table",
    "desk",
    "dining_table",
    "end_table",
    "nightstand",
    "side_table",
    "side_tables",
)

TABLETOP_STORAGE_SUPPORT_CATEGORIES = (
    "book_shelf",
    "book_shelves",
    "bookcase",
    "bookshelf",
    "bookshelves",
    "cabinet",
    "dresser",
    "shelf",
    "shelving_unit",
    "sideboard",
    "table",
)

TABLETOP_MEDIA_SUPPORT_CATEGORIES = (
    "console",
    "media_console",
    "media_unit",
    "tv_stand",
)

# Categories that must snap to a wall at height — never on the floor
WALL_MOUNTED_CATEGORIES = (
    "painting",
    "wall_art",
    "print",
    "sconce",
    "wall_mirror",
)

# Categories that hang from the ceiling — never on the floor.
# Stay in sync with wallH (ceiling height) in frontend/js/scene3d.js.
CEILING_MOUNTED_CATEGORIES = (
    "mount",
    "chandelier",
    "ceiling_lamp",
    "ceiling_light",
    "flush_mount_lamp",
    "pendant",
    "pendant_lamp",
    "pendant_light",
)

WALL_MOUNT_Z = 1.6  # meters (~63") — center height for wall-mounted fixtures
CEILING_HEIGHT_M = 2.8  # meters — room ceiling height; matches scene3d.js wallH

WALL_ALIGNED_CATEGORY_KEYWORDS = (
    "sofa",
    "sectional",
    "loveseat",
    "sleeper_sofa",
    "sofa_bed",
    "chaise_lounge",
    "daybed",
    "tv_stand",
    "media_unit",
    "entertainment",
    "bookcase",
    "bookshelf",
    "bookshelves",
    "book_shelf",
    "book_shelves",
    "cabinet",
    "sideboard",
    "storage_unit",
    "dresser",
    "wardrobe",
    "armoire",
    "shelf",
)

WINDOW_CLEARANCE_ROLE_KEYWORDS = (
    "tv",
    "television",
    "tv_stand",
    "media_unit",
    "entertainment",
)

_INSTANCE_UID_RE = re.compile(r"_\d+_\d+$")
_TRAILING_INSTANCE_RE = re.compile(r"_\d+$")


def normalize_asset_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


def base_asset_uid(uid: str) -> str:
    value = str(uid or "")
    return _TRAILING_INSTANCE_RE.sub("", value) if _INSTANCE_UID_RE.search(value) else value


def matches_category_keywords(
    category: str, uid: str, keywords: tuple[str, ...]
) -> bool:
    """Check if normalized category or uid contains any of the keywords."""
    labels = [normalize_asset_label(category), normalize_asset_label(uid)]
    return any(label and kw in label for label in labels for kw in keywords)


def is_wall_aligned_asset(category: str = "", uid: str = "") -> bool:
    return matches_category_keywords(category, uid, WALL_ALIGNED_CATEGORY_KEYWORDS)


def is_wall_mounted_asset(category: str = "", uid: str = "") -> bool:
    return matches_category_keywords(category, uid, WALL_MOUNTED_CATEGORIES)


def is_ceiling_mounted_asset(category: str = "", uid: str = "") -> bool:
    return matches_category_keywords(category, uid, CEILING_MOUNTED_CATEGORIES)


def is_floor_lamp_asset(category: str = "", uid: str = "") -> bool:
    return matches_category_keywords(category, uid, FLOOR_LAMP_CATEGORIES)


def is_floor_only_asset(category: str = "", uid: str = "") -> bool:
    normalized_category = normalize_asset_label(category)
    return (
        normalized_category in FLOOR_ONLY_CATEGORIES
        or is_floor_lamp_asset(normalized_category, uid)
    )


def is_table_lamp_asset(category: str = "", uid: str = "") -> bool:
    return matches_category_keywords(category, uid, TABLE_LAMP_CATEGORIES)


def is_table_lamp_support_asset(category: str = "", uid: str = "") -> bool:
    if is_table_lamp_asset(category, uid):
        return False
    return matches_category_keywords(category, uid, TABLE_LAMP_SUPPORT_CATEGORIES)


def is_tabletop_support_asset(category: str = "", uid: str = "") -> bool:
    if is_table_lamp_asset(category, uid):
        return False
    return matches_category_keywords(category, uid, TABLETOP_SUPPORT_CATEGORIES)


def tabletop_support_priority(category: str = "", uid: str = "") -> int:
    if matches_category_keywords(category, uid, TABLETOP_PRIMARY_SUPPORT_CATEGORIES):
        return 0
    if matches_category_keywords(category, uid, TABLETOP_SECONDARY_SUPPORT_CATEGORIES):
        return 1
    if matches_category_keywords(category, uid, TABLETOP_STORAGE_SUPPORT_CATEGORIES):
        return 2
    if matches_category_keywords(category, uid, TABLETOP_MEDIA_SUPPORT_CATEGORIES):
        return 3
    return 4


def requires_window_clearance(
    category: str = "",
    uid: str = "",
    *,
    z: float = 0.0,
) -> bool:
    """True when this asset must keep the window footprint clear."""
    return float(z or 0.0) > 0.1 or matches_category_keywords(
        category,
        uid,
        WINDOW_CLEARANCE_ROLE_KEYWORDS,
    )


def ceiling_mount_z(asset_height: float | None) -> float:
    """Z (bottom of asset) so the asset top sits flush with the ceiling."""
    return max(0.0, CEILING_HEIGHT_M - float(asset_height or 0.3))
