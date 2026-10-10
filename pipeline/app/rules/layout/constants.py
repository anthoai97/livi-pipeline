"""Thresholds and issue-tier constants for layout analysis."""

import math
from app.rules.layout.bedroom import BEDROOM_ISSUE_KEYS
from app.rules.layout.studio import STUDIO_ISSUE_KEYS

OVERLAP_EXEMPT_CATEGORIES = {"rug", "runner"}

CARDINAL_TOLERANCE = 0.2

WALL_MOUNT_WALL_THRESHOLD = 0.4

WALL_TV_CENTER_Z = 1.1  # seated eye level: a wall-mounted TV's screen center

WALL_TV_SUPPORT_GAP_M = 0.05  # a wall-mounted TV's bottom clears the furniture under it by this much

COFFEE_TABLE_ROLE_KEYWORDS = ("coffee_table",)

MEDIA_ROLE_KEYWORDS = ("tv", "television", "tv_stand", "media_unit", "entertainment")

MEDIA_DISPLAY_ROLE_KEYWORDS = ("tv", "television", "monitor")

MEDIA_SUPPORT_ROLE_KEYWORDS = ("tv_stand", "media_unit", "media_console", "console")

TABLETOP_DISPLAY_ROLE_KEYWORDS = (
    "bowl",
    "decor",
    "lamp",
    "object",
    "sculpture",
    "tray",
    "vase",
)

SIDE_TABLE_ROLE_KEYWORDS = ("side_table", "end_table")

LOUNGE_CHAIR_ROLE_KEYWORDS = (
    "accent_chair",
    "arm_chair",
    "chaise_lounge",
    "lounge_chair",
    "recliner",
    "swivel_chair",
)

TASK_CHAIR_ROLE_KEYWORDS = ("desk_chair", "office_chair", "task_chair")

COFFEE_TABLE_AXIS_TOLERANCE = math.pi / 12  # 15 degrees

MEDIA_CENTERLINE_MAX_OFFSET_M = 0.46  # ~18 in

MEDIA_SUPPORT_CENTERLINE_MAX_OFFSET_M = 0.3

MEDIA_SUPPORT_MAX_GAP_M = 0.35

MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG = 10.0

MEDIA_SEATING_MAX_DISTANCE_M = 4.2

SIDE_TABLE_SERVICE_REACH_MAX_M = 0.2

SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG = 135.0

FLOOR_LAMP_REACH_RANGE_M = (0.3, 0.46)

FLOOR_LAMP_WALL_MAX_GAP_M = 0.3

WINDOW_SEATING_CLEARANCE_M = 0.4

LIVING_SEAT_RUG_MAX_GAP_M = 0.25

LIVING_SEAT_COFFEE_TABLE_MAX_GAP_M = 0.65

LOUNGE_CHAIR_FACING_MAX_DELTA_DEG = 90.0

LOUNGE_CHAIR_TABLE_AIM_TOLERANCE_M = 0.15

DINING_CHAIR_GAP_M = 0.08

DINING_CHAIR_TABLE_MAX_GAP_M = 0.45

DINING_CHAIR_FACING_MAX_DELTA_DEG = 50.0

DINING_CHAIR_TABLE_AIM_TOLERANCE_M = 0.05

DINING_ACCESS_MIN_M = 0.56

DINING_LIGHT_TABLE_MIN_GAP_M = 0.76

DINING_CRITICAL_ISSUE_KEYS = (
    "dining_completeness_violations",
    "dining_seat_space_violations",
    "dining_access_violations",
    "dining_storage_access_violations",
    "dining_light_clearance_violations",
)

DINING_ISSUE_KEYS = (
    *DINING_CRITICAL_ISSUE_KEYS,
    "dining_light_alignment_violations",
    "dining_rug_violations",
)

TASK_CHAIR_DESK_MAX_GAP_M = 0.45

TASK_CHAIR_FACING_MAX_DELTA_DEG = 50.0

TABLETOP_SUPPORT_INSET_M = 0.04

TABLETOP_SUPPORT_TOLERANCE_M = 0.01

TABLETOP_SURFACE_Z_TOLERANCE_M = 0.08

PROTECTED_PATH_MIN_OVERLAP_SQM = 0.05

LIVING_DINING_CLEARANCE_M = 0.76

ISSUE_KEYS_BY_TIER = {
    "P0": (
        "overlaps",
        "boundary_violations",
        "door_violations",
        "obstacle_violations",
        "wall_mount_violations",
        "ceiling_mount_violations",
    ),
    "P1": (
        "wall_aligned_violations",
        "protected_path_violations",
    ),
    "P2": (
        *STUDIO_ISSUE_KEYS,
        *BEDROOM_ISSUE_KEYS,
        *DINING_ISSUE_KEYS,
        "sofa_coffee_table_violations",
        "sofa_wall_gap_violations",
        "media_focal_alignment_violations",
        "media_group_violations",
        "rug_composition_violations",
        "coffee_table_axis_violations",
        "living_group_cohesion_violations",
        "dining_group_violations",
        "living_dining_clearance_violations",
        "task_chair_orientation_violations",
        "side_table_reach_violations",
        "floor_lamp_reach_violations",
        "table_lamp_support_violations",
        "service_corridor_violations",
    ),
}

CRITICAL_P2_ISSUE_KEYS = (
    *STUDIO_ISSUE_KEYS,
    *BEDROOM_ISSUE_KEYS,
    *DINING_CRITICAL_ISSUE_KEYS,
    "sofa_coffee_table_violations",
    "living_group_cohesion_violations",
    "dining_group_violations",
    "living_dining_clearance_violations",
    "task_chair_orientation_violations",
)

CRITICAL_LIVING_GROUP_KINDS = {
    "sofa_console_access",
    "seat_detached_from_table",
    "seat_faces_away",
    "seat_misses_table",
}
