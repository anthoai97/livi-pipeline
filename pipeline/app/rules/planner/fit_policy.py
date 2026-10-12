"""Deterministic physical policy for pre-placement fit checks.

These values are not language understanding. They are repeatable geometry and
count coefficients used after LLM intent has already been converted to canonical
categories. Keep user-wording mappings in `intent_understanding`/`taxonomy`, not
here.
"""

from __future__ import annotations

SECTIONAL_MIN_ROOM = (3.66, 4.27)  # sectionals need at least 12ft x 14ft

# Default width and depth of a requested piece, and whether it covers floor (rugs, tabletop and mounted pieces do not).
ROLE_PROFILES = {
    "sleeping_anchor": (1.6, 2.1, True),
    "bedside": (0.5, 0.4, True),
    "anchor_seating": (2.1, 0.95, True),
    "accent_seating": (0.75, 0.75, True),
    "surface": (0.9, 0.55, True),
    "media": (1.4, 0.45, True),
    "storage": (0.9, 0.4, True),
    "lighting": (0.35, 0.35, True),
    "tabletop": (0.25, 0.25, False),
    "rug": (2.0, 1.6, False),
    "wall_or_ceiling": (0.0, 0.0, False),
    "other": (0.6, 0.6, True),
}

# Share of the usable floor (doors excluded) that floor furniture covers, as real footprint (width x depth; rugs,
# mounted, tabletop and supported pieces excluded): the comfortable target, the maximum, and the sparse floor below
# which a living room or bedroom is under-furnished. Dining rooms and studios have no floor.
ROOM_THRESHOLDS = {
    "living_room": {
        "compact": {"comfortable": 0.22, "overcrowded": 0.30, "sparse": 0.15},
        "medium": {"comfortable": 0.27, "overcrowded": 0.35, "sparse": 0.16},
        "spacious": {"comfortable": 0.30, "overcrowded": 0.38, "sparse": 0.125},
    },
    "bedroom": {
        "compact": {"comfortable": 0.42, "overcrowded": 0.50, "sparse": 0.15},
        "medium": {"comfortable": 0.35, "overcrowded": 0.43, "sparse": 0.16},
        "spacious": {"comfortable": 0.30, "overcrowded": 0.38, "sparse": 0.125},
    },
    "dining_room": {
        "compact": {"comfortable": 0.32, "overcrowded": 0.40, "sparse": 0.0},
        "medium": {"comfortable": 0.30, "overcrowded": 0.38, "sparse": 0.0},
        "spacious": {"comfortable": 0.28, "overcrowded": 0.36, "sparse": 0.0},
    },
    "studio": {
        "compact": {"comfortable": 0.35, "overcrowded": 0.43, "sparse": 0.0},
        "medium": {"comfortable": 0.32, "overcrowded": 0.40, "sparse": 0.0},
        "spacious": {"comfortable": 0.30, "overcrowded": 0.38, "sparse": 0.0},
    },
}

ROLE_CAPS = {
    "compact": {
        "anchor_seating": 1,
        "accent_seating": 2,
        "surface": 2,
        "media": 1,
        "storage": 1,
        "lighting": 2,
        "tabletop": 3,
        "rug": 1,
        "wall_or_ceiling": 3,
        "other": 2,
    },
    "medium": {
        "anchor_seating": 1,
        "accent_seating": 4,
        "surface": 4,
        "media": 2,
        "storage": 2,
        "lighting": 3,
        "tabletop": 5,
        "rug": 1,
        "wall_or_ceiling": 5,
        "other": 4,
    },
    "spacious": {
        "anchor_seating": 2,
        "accent_seating": 6,
        "surface": 6,
        "media": 2,
        "storage": 3,
        "lighting": 5,
        "tabletop": 8,
        "rug": 2,
        "wall_or_ceiling": 8,
        "other": 6,
    },
}

CATEGORY_CAPS = {
    "compact": {"sectional": 0, "sofa": 1, "loveseat": 1, "coffee_table": 1, "bed": 1, "nightstand": 2},
    "medium": {"sectional": 1, "sofa": 1, "loveseat": 1, "coffee_table": 1, "bed": 1, "nightstand": 2},
    "spacious": {"sectional": 1, "sofa": 2, "loveseat": 2, "coffee_table": 2, "bed": 1, "nightstand": 2},
}

REMOVAL_PRIORITY = {
    "sleeping_anchor": 0,
    "bedside": 45,
    "tabletop": 90,
    "wall_or_ceiling": 80,
    "lighting": 70,
    "storage": 60,
    "accent_seating": 50,
    "surface": 40,
    "other": 30,
    "media": 20,
    "anchor_seating": 10,
    "rug": 5,
}

PROTECTED_MIN_ROLES = {
    "sleeping_anchor",
    "anchor_seating",
    "surface",
    "media",
    "rug",
    "storage",
    "lighting",
}

