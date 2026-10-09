"""Domain constants for asset selection, fit, and catalog policy."""

from app.rules.planner.taxonomy import (
    COUNT_GUIDANCE_ROLE_CATEGORIES,
    NON_SELLABLE_CATEGORIES,
    ROLE_CATEGORIES,
)

BUDGET_FLEX_PCT = 0.10  # allow up to +10% over stated budget

BUDGET_EXCLUDED_CATEGORIES = NON_SELLABLE_CATEGORIES

REQUIRED_CATEGORIES = {
    "anchor_seating": ROLE_CATEGORIES["anchor_seating"],
    "surface": {"coffee_table", "desk", "side_table"},
    "lighting": COUNT_GUIDANCE_ROLE_CATEGORIES["lighting"],
    "rug": {"rug"},
}

BLOCKING_REQUIRED_ROLES = {"anchor_seating", "surface"}

RECOMMENDED_CATEGORIES = {
    "media": {"tv_stand", "tv", "media_unit"},
    "accent_seating": {"accent_chair", "office_chair", "ottoman"},
}

FIT_ANCHOR_SEATING = REQUIRED_CATEGORIES["anchor_seating"]

FIT_ACCENT_SEATING = ROLE_CATEGORIES["accent_seating"] | {"chair"}

FIT_SURFACES = REQUIRED_CATEGORIES["surface"] | {"end_table", "writing_desk"}

FIT_LIGHTING = REQUIRED_CATEGORIES["lighting"]

FIT_RUGS = REQUIRED_CATEGORIES["rug"]

FIT_CATEGORY_LABELS = {
    "accent_chair": ("chair", "chairs"),
    "chair": ("chair", "chairs"),
    "coffee_table": ("coffee table", "coffee tables"),
    "desk": ("desk", "desks"),
    "floor_lamp": ("floor lamp", "floor lamps"),
    "lamp": ("lamp", "lamps"),
    "loveseat": ("loveseat", "loveseats"),
    "media_unit": ("media unit", "media units"),
    "office_chair": ("office chair", "office chairs"),
    "rug": ("rug", "rugs"),
    "sectional": ("sectional", "sectionals"),
    "side_table": ("side table", "side tables"),
    "sofa": ("couch", "couches"),
    "table_lamp": ("table lamp", "table lamps"),
    "tv": ("TV", "TVs"),
    "tv_stand": ("TV stand", "TV stands"),
}

SECTIONAL_CATEGORIES = {"sectional"}

TV_HOST_CATEGORIES = {"tv_stand", "media_unit", "media_console"}

TV_COMPATIBLE_SUPPORT_CATEGORIES = TV_HOST_CATEGORIES | {"console_table"}

TV_COMPATIBLE_SUPPORT_KEYWORDS = (
    "tv stand",
    "media console",
    "media unit",
    "console table",
)

TV_SUPPORT_INSET_M = 0.04

TV_SUPPORT_TOLERANCE_M = 0.01

LAYOUT_PREFLIGHT_PREFIX = "LAYOUT PREFLIGHT"

REQUESTED_ROLE_PREFIX = "REQUESTED ROLE"

LAYOUT_RUG_CATEGORIES = {"rug", "runner", "area_rug", "carpet"}

MEDIA_DISPLAY_CATEGORIES = {"tv", "television", "monitor", "display"}

DESK_CATEGORIES = {"desk", "writing_desk"}

TASK_CHAIR_CATEGORIES = {"office_chair", "task_chair", "desk_chair"}

DINING_TABLE_CATEGORIES = {"dining_table", "bar_table"}

DINING_CHAIR_CATEGORIES = {"dining_chair", "chair"}

STORAGE_CATEGORIES = {
    "bookcase",
    "bookshelf",
    "cabinet",
    "dresser",
    "media_unit",
    "sideboard",
    "storage_organizer",
    "storage_unit",
}

PERIMETER_STORAGE_CATEGORIES = {"bookcase", "cabinet", "sideboard"}

REQUESTED_ROLE_HARD_CATEGORIES = (
    FIT_ANCHOR_SEATING
    | FIT_SURFACES
    | FIT_LIGHTING
    | FIT_RUGS
    | TV_HOST_CATEGORIES
    | STORAGE_CATEGORIES
    | {"bed", "nightstand"}
)
