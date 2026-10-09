"""Deterministic physical policy for pre-placement fit checks.

These values are not language understanding. They are repeatable geometry and
count coefficients used after LLM intent has already been converted to canonical
categories. Keep user-wording mappings in `intent_understanding`/`taxonomy`, not
here.
"""

from __future__ import annotations

SECTIONAL_MIN_ROOM = (3.66, 4.27)  # sectionals need at least 12ft x 14ft

# width, depth, clearance, density weight
ROLE_PROFILES = {
    "sleeping_anchor": (1.6, 2.1, 0.6, 1.0),
    "bedside": (0.5, 0.4, 0.1, 1.0),
    "anchor_seating": (2.1, 0.95, 0.55, 1.0),
    "accent_seating": (0.75, 0.75, 0.45, 1.0),
    "surface": (0.9, 0.55, 0.35, 1.0),
    "media": (1.4, 0.45, 0.30, 1.0),
    "storage": (0.9, 0.4, 0.35, 1.0),
    "lighting": (0.35, 0.35, 0.20, 1.0),
    "tabletop": (0.25, 0.25, 0.05, 0.10),
    "rug": (2.0, 1.6, 0.0, 0.15),
    "wall_or_ceiling": (0.0, 0.0, 0.0, 0.0),
    "other": (0.6, 0.6, 0.30, 1.0),
}

ROOM_THRESHOLDS = {
    "compact": {"comfortable": 0.52, "overcrowded": 0.72, "sparse": 0.30},
    "medium": {"comfortable": 0.55, "overcrowded": 0.75, "sparse": 0.32},
    "spacious": {"comfortable": 0.60, "overcrowded": 0.82, "sparse": 0.25},
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

