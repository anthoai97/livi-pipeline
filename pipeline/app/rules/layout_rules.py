"""Single source of truth for layout definitions and tiered rules.

All layout-producing and layout-evaluating LLM prompts (initial, fix, refine,
constraints, score) compose their rule blocks from this module. Keeping the
text here means rule changes propagate everywhere at once and prompts cannot
drift apart.

Tier model
----------
- P0  Architecture and local hard constraints. Hard fails. Never violate.
- P1  Room zoning, circulation, and designability. The room must support a sensible layout
      (anchor wall, focal wall, wall-aligned anchors).
- P2  Composition, function, and aesthetics. Applied after P0 and P1 are clean.

Use ``build_rules_block`` to assemble the slice each node needs.
"""

from app.rules.pipeline_shared import DOOR_CLEARANCE, WALL_MOUNT_Z, room_bounds, room_walls
from app.rules.planner.fit_policy import SECTIONAL_MIN_ROOM  # noqa: F401 (re-exported)
from app.rules.layout.bedroom import (
    BED_ACCESS_MIN_M,
    BED_HEADBOARD_MAX_GAP_M,
    BEDROOM_TV_MAX_FACING_DEG,
    BEDSIDE_HEAD_ZONE_M,
    BEDSIDE_MAX_GAP_M,
    STORAGE_FRONT_M,
)
from app.rules.layout.constants import (
    DINING_ACCESS_MIN_M,
    DINING_CHAIR_FACING_MAX_DELTA_DEG,
    DINING_CHAIR_GAP_M,
    DINING_CHAIR_TABLE_MAX_GAP_M,
    DINING_LIGHT_TABLE_MIN_GAP_M,
    FLOOR_LAMP_REACH_RANGE_M,
    TASK_CHAIR_DESK_MAX_GAP_M,
    TASK_CHAIR_FACING_MAX_DELTA_DEG,
)
from app.rules.layout.comfort import (
    SITTING_RUG_MIN_DEPTH_M,
    SITTING_RUG_MIN_WIDTH_RATIO,
    SOFA_MEDIA_ACCESS_MIN_M,
    TV_VIEW_WIDTH_RANGE,
)

# Circulation widths (meters). Match the reference spec: 36 in / 22 in.
PATH_MAJOR_M = 0.91
PATH_SECONDARY_M = 0.56
SEATING_CENTER_FIELD_M = 0.46  # 18 in radius around the conversation centroid
SOFA_COFFEE_TABLE_DISTANCE_RANGE_M = (0.46, 0.61)
SOFA_BACK_WALL_DISTANCE_RANGE_M = (0.08, 0.15)
WALL_FLUSH_MAX_GAP_M = 0.2


# ---------------------------------------------------------------------------
# Coordinate / rotation primer (used by every layout-producing prompt)
# ---------------------------------------------------------------------------

LAYOUT_SYSTEM_INSTRUCTION = """You are working only on a physical residential interior floor plan.
"Layout" means furniture placement in the supplied room geometry, never a web page, UI, CSS, responsive design, accessibility markup, or ARIA.
Treat supplied dimensions, coordinates, openings, deterministic geometry measurements, and validator findings as authoritative.
Perform only the requested spatial task. Preserve unrelated assets and never add, remove, rename, or redesign them unless the request explicitly requires it.
Follow the requested JSON schema or tool-call contract exactly, without prose outside that output."""


# Design rules the final review applies as text: comfort and taste, not geometry the checks enforce.
# Add a line here to teach the review a rule; add a room type key to give a new room its own rules.
DESIGN_RULES = {
    "all": (
        "Clear zones: each group (bed, sitting, dining, work) reads as one area, and zones do not crowd each other.",
        "A TV stands on its stand (wall-mounted only when the brief asks), faces its viewers, and its back does not face another zone.",
        "Large pieces stand against a wall or anchor a zone; nothing floats in the middle of the room without a purpose.",
        (f"A walkway at least {PATH_SECONDARY_M:.2f} m wide runs from each door to every zone: bed sides and foot, seat "
         "fronts, chair pull-outs and storage fronts."),
        "Nothing taller than a window sill (about 0.9 m), such as a bookcase, cabinet or wardrobe, stands in front of a window.",
        "A floor lamp stands right at the end of the seat or bed it lights, within arm's reach, not across the room.",
    ),
    "living_room": (
        (f"Leave {SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]:.2f}-{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[1]:.2f} m "
         "between the sofa and the coffee table, and about 0.45 m between an accent chair and the coffee table."),
        "Accent chairs face the coffee table and the TV or sofa, not a wall.",
    ),
    "bedroom": (
        "The bed's headboard is against a wall, with access from its sides and foot.",
    ),
    "studio": (
        "The bed's headboard is against a wall, with access from its sides and foot.",
        "Sleeping, sitting and dining each take their own part of the room.",
    ),
    "dining_room": (
        "The table sits where every chair can pull out, with a clear path around it.",
    ),
}


def design_rules_block(room_type: str) -> str:
    """The shared and room-specific DESIGN_RULES as a bulleted list."""
    return "\n".join(f"- {rule}" for rule in (*DESIGN_RULES["all"], *DESIGN_RULES.get(room_type, ())))


def coordinate_system_block(room_vertices: list[list[float]]) -> str:
    """Compact coordinate, wall list, and rotation primer."""
    min_x, min_y, max_x, max_y = room_bounds(boundary=room_vertices)
    walls = "; ".join(
        f"{wall.index} ({wall.label}) ({wall.start[0]:.2f}, {wall.start[1]:.2f}) -> ({wall.end[0]:.2f}, {wall.end[1]:.2f})"
        for wall in room_walls(room_vertices)
    )
    return (
        f"COORDINATES: origin (0,0) bottom-left, +X right, +Y up. "
        f"Bounds x=[{min_x:.2f}, {max_x:.2f}], y=[{min_y:.2f}, {max_y:.2f}]. "
        f"Position is the asset center. Keep the asset's projected half-extents inside every wall.\n"
        f"WALLS (index (name) start -> end, counter-clockwise, the room on the left of each): {walls}.\n"
        f"ROTATION rot_z (radians) uses standard yaw / atan2 semantics: "
        f"0 → +X, π/2 → +Y, π → -X, 3π/2 → -Y.\n"
        f"rot_z is the direction the asset FRONT faces. BACK = opposite side, LEFT/RIGHT = ±90° from FRONT.\n"
        f"ASSET DIMENSIONS (W×D×H): `depth` is the front-to-back extent (along rot_z); "
        f"`width` is the side-to-side extent (perpendicular to rot_z, parallel to the FRONT/BACK edges); "
        f"`height` is vertical. A typical sofa lists its long side as `width` and its shallow front-to-back as `depth`.\n"
        f"PREVIEW IMAGES: the colored arrow drawn on each asset points along rot_z — the FRONT direction."
    )


# ---------------------------------------------------------------------------
# Geometry definitions — included once, when the prompt needs the vocabulary
# ---------------------------------------------------------------------------


GEOMETRY_DEFINITIONS = f"""DEFINITIONS:
- Room boundary: closed finished-floor polygon. All assets stay inside it.
- Fixed obstacles: columns, fireplaces, built-ins, radiators, vents, stair openings.
- Openings: doors and windows cut into walls; doors always keep a clear zone, while windows stay clear only for assets that require window clearance.
- Doorway clear zone: rectangle in front of each doorway, full opening width by {DOOR_CLEARANCE:.2f}m deep, kept furniture-free.
- Major path: circulation band between groups, ≥{PATH_MAJOR_M:.2f}m clear.
- Secondary path: access band to a single piece, ≥{PATH_SECONDARY_M:.2f}m clear.
- Hard no-place zone: union of all fixed obstacles, doorway clear zones, protected paths, and any window footprint that the current asset must keep clear.
- Anchor wall: continuous wall length usable for the chosen anchor after subtracting doors, fireplaces, edge buffers, and any windows that the anchor cannot sit in front of.
- Focal wall: wall used for the visual focal point (TV, fireplace, large art); primary seating faces it.
- Conversation centroid: center of the primary seating cluster.
- Seating center field: circle of radius {SEATING_CENTER_FIELD_M:.2f}m around the conversation centroid; major routes never cross it.
- Inter-asset distance: always edge-to-edge — the shortest gap between the two assets' oriented bounding boxes (nearest face of A to nearest face of B). Never center-to-center. "Sofa ↔ coffee table 0.55m" means 0.55m of empty floor between the sofa's front face and the table's nearest face. Asset ↔ wall gaps are measured the same way (nearest asset face to the wall)."""


# ---------------------------------------------------------------------------
# Tiered rules
# ---------------------------------------------------------------------------


P0_RULES = f"""P0 — ARCHITECTURE & LOCAL HARD CONSTRAINTS (must never be violated):
- Every asset stays inside the room boundary.
- No asset overlaps a fixed obstacle or door.
- No asset sits inside a doorway clear zone ({DOOR_CLEARANCE:.2f}m in front of any door).
- Window rule: floor assets with z≤0.1 may sit under / in front of a window, including sofas; assets with z>0.1 and TV / tv_stand / media_unit pieces must keep windows clear.
- Z-axis: floor items z=0; wall-mounted items snap to a wall at z={WALL_MOUNT_Z:.2f}m (a TV stands on its stand and is wall-mounted only when the brief asks, at that height, above any furniture under it) and must keep their full height within the room's wall height; ceiling-mounted items hang so the asset top sits at the room's wall height; items resting on another asset (bowl on table, vase on shelf, lamp on side_table, pillow on sofa, etc.) declare `on_top_of` with the parent uid and leave z=0 — the runtime computes z from the parent's top surface (seat height for sofas/chairs/beds)."""


P1_RULES = f"""P1 — ROOM ZONING, CIRCULATION & DESIGNABILITY (the room must be designable):
- Major walking paths stay ≥{PATH_MAJOR_M:.2f}m wide.
- Secondary access paths stay ≥{PATH_SECONDARY_M:.2f}m wide.
- A continuous walkway ≥{PATH_SECONDARY_M:.2f}m wide joins the entry door to every other door and to each service area: bed foot and sides, sofa and lounge seat fronts, dining and desk chair pull-out, storage fronts (wardrobe, dresser, cabinet, sideboard, bookcase, storage unit). Tables are reached through their chairs.
- No required route crosses the seating center field.
- Straight protected paths between opposing doors stay fully clear except rugs/runners.
- The primary living zone uses at least one usable anchor wall long enough for the largest anchor seating piece (sofa, sectional, bed).
- The focal wall span used by a TV / tv_stand / media_unit is clear of doors and windows.
- Wall-aligned categories sit flush against a wall with their BACK face against the wall and their FRONT facing inward into the room; never diagonal: sofa, sectional, loveseat, sleeper sofa, daybed, chaise lounge, tv stand, media unit, bookcase, cabinet, sideboard, dresser, shelf, storage unit. Their `depth` runs perpendicular to the wall; their `width` runs parallel to it.
- For wall-aligned assets, use the inward-facing cardinal yaw for that wall: left wall → 0, right wall → π, bottom wall → π/2, top wall → 3π/2."""


P2_RULES = f"""P2 — COMPOSITION & FUNCTION (apply after P0 and P1 are clean):
- Conversation distances (all values are edge-to-edge gaps between bounding boxes, not center-to-center):
  * sofa front ↔ coffee table nearest face: {SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]:.2f}–{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[1]:.2f}m (reachable, legroom intact).
  * sofa front ↔ facing sofa or chair front: 1.22–2.74m (conversation zone).
  * sofa back face ↔ wall behind it: {SOFA_BACK_WALL_DISTANCE_RANGE_M[0]:.2f}–{SOFA_BACK_WALL_DISTANCE_RANGE_M[1]:.2f}m.
- Functional adjacency (edge-to-edge gaps):
  * nightstand beside bed; side table beside seating.
  * floor lamp 0.30–0.46m from a reading seat and within 0.30m of a wall, not floating mid-room.
  * table lamp must declare `on_top_of` a side table, nightstand, desk, or console.
  * table lamps and tabletop decor/display objects (bowls, vases, sculptures, trays, small decorative objects) must declare `on_top_of`, keep their full footprint inset on the support surface, and must not overlap a TV or any other object sharing that tabletop/console/shelf.
  * armchair side table within 0.15m of the chair arm.
- Seating-group cohesion: the coffee table and the chairs/loveseats that face it form one group with the sofa. When the coffee table is repositioned, translate every group chair and group side table by the same (dx, dy) so the conversation zone, sofa↔table gap, and chair-facing geometry are preserved. The sofa is the wall-anchored root of the group — if the table must move far enough to break the sofa↔table gap, relocate the entire group (sofa included) rather than leaving chairs stranded.
- Rectangular coffee tables should keep their long axis parallel to the sofa axis and stay centered on the primary seating axis.
- In sofa-led layouts, side tables serve sofa arms first and stay within easy reach of the seat they support; only use chair-service positions when sofa-arm positions do not fit cleanly.
- Rotation intent:
  * sofas and primary seating face the focal wall / TV / fireplace.
  * tv stands and media units face the primary seating.
  * in TV-focused rooms, TV / tv_stand / media_unit pieces center on the focal wall directly opposite the primary sofa whenever the wall span allows.
  * chairs that belong to a seating group (armchairs, accent chairs, lounge chairs paired with a sofa/sectional) face the shared coffee table at the group's center; if there is no coffee table, face the conversation centroid.
  * compute each group chair's own yaw as atan2(table_y-chair_y, table_x-chair_x). Its FRONT arrow should cross the associated tabletop footprint; aiming at another chair while both arrows miss the table does not satisfy this. Toe chairs inward when needed, using their rotated footprints to recheck clearance. For multiple seating groups, use each chair's own nearby table; task/dining chairs face their desk/dining table. Explicit user-facing directions take precedence; table-free conversation groups do not require an imaginary table.
  * desks face away from the wall behind them.
- Visual balance: prefer matched pairs flanking a larger anchor.
- Open living/dining zones keep at least 0.76m edge-to-edge clearance between the dining table footprint and living-zone seating or coffee tables.
- Anchor-first placement: place each anchor (sofa, bed, desk, tv) before the pieces that reference it (rug, coffee table, nightstand, media unit, side tables, lamps). Decor and plants last."""


_TIER_TEXT = {
    "P0": P0_RULES,
    "P1": P1_RULES,
    "P2": P2_RULES,
}

# Each room's layout rules: a header, then short lines numbered in the prompt.
_DINING_SEAT = (
    f"Each dining chair faces its table edge within {DINING_CHAIR_FACING_MAX_DELTA_DEG:.0f} degrees, about "
    f"{DINING_CHAIR_GAP_M:.2f} m from it (at most {DINING_CHAIR_TABLE_MAX_GAP_M:.2f} m), and its FRONT ray crosses the tabletop."
)
_DINING_PITCH = (
    "Each seat takes max(0.60 m, chair width + 0.05 m) of its table edge; three seats need a long edge, "
    "and round or oval tables space seats evenly around them."
)
_DESK = (
    f"A desk backs onto a wall; its chair faces the desk front within {TASK_CHAIR_FACING_MAX_DELTA_DEG:.0f} degrees, "
    f"at most {TASK_CHAIR_DESK_MAX_GAP_M:.2f} m from it, with {BED_ACCESS_MIN_M:.2f} m clear behind the chair to pull it out."
)
ROOM_LAYOUT_RULES = {
    "bedroom": ("BEDROOM RULES (take precedence over living-room guidance):", (
        ("The complete bed anchors the room. A sofa, coffee table, TV or conversation group is not required; "
         "living, dining and work rules apply only to selected groups."),
        "The bed's FRONT points to its foot and its BACK is the headboard; depth runs head to foot, width across the bed.",
        f"The headboard stands within {BED_HEADBOARD_MAX_GAP_M:.2f} m of a usable wall, facing into the room within 12 degrees.",
        (f"Keep {BED_ACCESS_MIN_M:.2f} m clear along the foot and along at least one side; prefer both sides clear, "
         "and keep both when the brief asks. A compact bedroom may have one clear side, never none."),
        (f"Nightstands stand beside the head within {BEDSIDE_MAX_GAP_M:.2f} m, never detached or at the foot; only "
         f"bedside pieces may use the top {BEDSIDE_HEAD_ZONE_M:.2f} m of a side strip. A bedside lamp declares "
         "on_top_of its nightstand."),
        f"Keep {STORAGE_FRONT_M:.2f} m clear in front of each dresser, wardrobe, cabinet or storage unit.",
        (f"A TV stands toward the bed's foot, facing the bed within {BEDROOM_TV_MAX_FACING_DEG:.0f} degrees, "
         "and its stand keeps the foot strip clear."),
        _DESK,
        ("Group bedside pieces and lighting with the bed, and storage, mirror or reading pieces into useful areas, "
         "not scattered on distant walls. Empty floor is not a defect; layout never adds pieces."),
        ("Use the full model half-extents with a 0.02-0.05 m gap from walls and neighbouring pieces; never round them "
         "down. For a nightstand parallel to the bed, the sideways distance between their centers exceeds bed width / 2 + "
         "nightstand width / 2."),
    )),
    "dining_room": ("DINING ROOM RULES (take precedence over living-room guidance):", (
        ("One standalone dining table anchors the dining chairs; no sofa, coffee table, media wall or bed is required. "
         "Place the table and chairs first, then storage and decor."),
        _DINING_SEAT,
        _DINING_PITCH + " For four chairs prefer two on each long side.",
        (f"Keep {DINING_ACCESS_MIN_M:.2f} m clear behind each chair to pull it out, inside the room and free of other "
         "furniture."),
        f"A sideboard or cabinet backs onto a usable wall with {STORAGE_FRONT_M:.2f} m clear in front.",
        (f"A pendant or chandelier centers over the table, at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f} m from the "
         "tabletop to its lowest point; never rescale the model."),
        "A rug, when selected, lies centered under the table and every seated chair, ideally reaching into the pull-out space.",
        ("Wall art hangs parallel to a usable wall, clear of openings, related to the table or sideboard. "
         "No floor lamp stands where a chair pulls out."),
    )),
    "studio": ("STUDIO RULES (sleeping, sitting and dining share one room):", (
        ("Place the bed, sofa and dining table first as separate anchors, reserving their access together, then "
         "their chairs, bedside pieces and accessories. The bed and sofa use different wall spans."),
        (f"The headboard stands within {BED_HEADBOARD_MAX_GAP_M:.2f} m of a usable wall, facing in within 12 degrees. "
         f"Keep {BED_ACCESS_MIN_M:.2f} m clear along the foot and at least one side. Nightstands stand beside the head "
         f"within {BEDSIDE_MAX_GAP_M:.2f} m."),
        _DINING_SEAT,
        _DINING_PITCH,
        (f"Keep {DINING_ACCESS_MIN_M:.2f} m clear behind each dining chair, free of the bed, sofa and other furniture. "
         "For two diners, compare opposing and adjacent seats on both table axes; no chair backs onto a wall."),
        ("Each rug, light and surface serves one group: a sitting rug its sofa and table, a dining rug its table and "
         f"chairs, a pendant its own table at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f} m above it."),
        ("A TV serves the sofa by default, or the bed or both when requested; the TV and each viewer face each other "
         "within 45 degrees. For shared viewing the bed and sofa face the same TV from separate wall spans."),
        "Prefer a wall-backed media group; a TV stand may stand free between groups as a divider, its back not facing another zone.",
        f"Keep {STORAGE_FRONT_M:.2f} m clear in front of storage. " + _DESK,
        "Walkways from the entry to each group stay continuous and cross no furniture or service band. A coffee table is optional.",
    )),
}
MEDIA_RUG_RULES = (
    f"A media console leaves at least {SOFA_MEDIA_ACCESS_MIN_M:.2f} m clear in the full-width band in front of a sofa.",
    (f"A TV's viewing distance is {TV_VIEW_WIDTH_RANGE[0]:.1f}-{TV_VIEW_WIDTH_RANGE[1]:.1f} x its width, for each of its "
     "sitting, bed or shared viewers. Estimate the seated viewer at sofa center minus 0.25 x depth along FRONT, the bed "
     "viewer at its head edge plus 0.40 m toward the foot, and the screen at TV center plus half its depth. These are "
     "comfort estimates, not hard limits."),
    ("A sitting rug reaches under the sofa front into the seating area: its near edge within 0.10 m of the sofa front, "
     "at least 0.30 m and 35% of its area in front of the sofa, at least "
     f"{SITTING_RUG_MIN_WIDTH_RATIO:.0%} of the sofa width wide and {SITTING_RUG_MIN_DEPTH_M:.2f} m deep, and off other groups' beds and tables."),
    "A centered bed rug balances its part under the bed with its part past the foot; bedside runners are fine.",
    "Rug and viewing preferences never move furniture into access bands or change the selected pieces.",
)


def _numbered(rules: tuple[str, ...] | list[str]) -> str:
    return "\n".join(f"{number}. {rule}" for number, rule in enumerate(rules, 1))


def build_rules_block(
    *,
    tiers: tuple[str, ...] = ("P0", "P1", "P2"),
    include_definitions: bool = True,
    room_type: str = "living_room",
) -> str:
    """Compose a rules block for a prompt.

    Args:
        tiers: Subset of tiers to include, in priority order.
        include_definitions: When True, prepend the geometry definitions block.
    """
    parts: list[str] = []
    if include_definitions:
        parts.append(GEOMETRY_DEFINITIONS)
    for tier in tiers:
        text = _TIER_TEXT.get(tier)
        if text:
            parts.append(text)
    if room_type in ROOM_LAYOUT_RULES:
        header, rules = ROOM_LAYOUT_RULES[room_type]
        parts.append(f"{header}\n{_numbered(rules)}")
    if "P2" in tiers:
        parts.append(f"MEDIA COMFORT AND RUG COMPOSITION:\n{_numbered(MEDIA_RUG_RULES)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Selection-stage guidance — what layout rules imply for asset picking
# ---------------------------------------------------------------------------


def build_selection_guidance_block(
    *,
    room_width: float,
    room_depth: float,
    room_type: str = "living_room",
) -> str:
    """Layout implications that shape which asset set is pickable.

    The asset selector never places anything, but its picks decide whether a
    layout is possible at all. This block translates the layout rules into
    numbered choices the selector can act on (anchor wall length, circulation
    budget, access bands, sofa–table pairings, etc.).
    """
    bed_access = (
        f"Reserve a usable headboard wall, {BED_ACCESS_MIN_M:.2f} m at the bed's foot and {BED_ACCESS_MIN_M:.2f} m on at "
        "least one side; prefer both sides."
    )
    dining_access = (
        f"Allow chair depth + {DINING_CHAIR_GAP_M:.2f} m table gap + {DINING_ACCESS_MIN_M:.2f} m pull-out on each occupied "
        "table edge, with doors and protected paths clear."
    )
    if room_type == "studio":
        return "STUDIO FIT FOR SELECTION:\n" + _numbered((
            ("Choose one complete standalone bed without built-in nightstands and a compact sofa or loveseat; "
             "a dining chair never counts as sitting."),
            bed_access,
            dining_access,
            ("Reserve every group's access and the entry walkways together from full catalog dimensions and the actual "
             "openings; one group fitting does not prove the studio fits."),
            "Rugs and coffee tables are optional.",
        ))
    if room_type == "bedroom":
        return "BEDROOM FIT FOR SELECTION:\n" + _numbered((
            ("Choose one complete bed by its full model width and depth, not an inferred mattress size; never a "
             "separate frame, mattress or bunk."),
            bed_access,
            ("Bedside lighting and clothing storage are suggested, not required: in a spacious bedroom prefer a bedside "
             "pair and consider a dressing or reading area; honor minimal, single-bedside and no-storage requests."),
            "Nightstands stand beside the head, and each lamp must fit its own support.",
            f"A dresser, wardrobe or cabinet needs {STORAGE_FRONT_M:.2f} m clear in front.",
            ("A TV and a desk with its task chair are opt-in, selected only when requested. A TV needs a view from the "
             "bed's foot and a support that keeps bed access clear."),
            f"A desk and task chair are one pair, with {BED_ACCESS_MIN_M:.2f} m behind the chair to pull it out.",
            "A sofa, coffee table or media group is not required.",
        ))
    if room_type == "dining_room":
        return "DINING FIT FOR SELECTION:\n" + _numbered((
            "Choose one standalone dining table and dining chairs of matching scale.",
            _DINING_PITCH,
            dining_access,
            f"A suggested sideboard or cabinet needs {STORAGE_FRONT_M:.2f} m clear in front.",
            (f"A suggested pendant or chandelier leaves at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f} m above the tabletop, measured "
             "with its full model height below the ceiling; never assume an adjustable hanger."),
            "A requested rug covers the table and the seated chairs.",
            "Choose a smaller table and chairs before cutting an optional seat; never cut a stated seat count.",
        ))
    min_w, min_d = SECTIONAL_MIN_ROOM
    sectional_ok = (room_width >= min_w and room_depth >= min_d) or (
        room_depth >= min_w and room_width >= min_d
    )
    return "LAYOUT FEASIBILITY FOR SELECTION (pick a set the layout stage can place):\n" + _numbered((
        ("Wall-aligned anchors (sofa, sectional, loveseat, bed, tv stand, media unit, bookcase, cabinet, sideboard, "
         "dresser) stand flush to a wall; their combined width must fit the usable wall length after doors, "
         "fireplaces, edge buffers and the windows they cannot stand in front of."),
        (f"Keep {PATH_MAJOR_M:.2f} m major paths between groups and {PATH_SECONDARY_M:.2f} m access to single pieces; "
         "plan about 20-30% of the floor for walkways."),
        "Sectionals fit this room." if sectional_ok else
        f"The room is below {min_w:.2f} x {min_d:.2f} m: skip sectionals and anchor on a loveseat or 2-seat sofa.",
        (f"Every sofa pairs with a coffee table {SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]:.2f}-"
         f"{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[1]:.2f} m in front (edge to edge) and a side table or floor lamp within reach."),
        ("In a TV-focused room, pick one media setup that can center on the wall opposite the sofa, on a span clear "
         "of doors and windows."),
        f"A reading chair pairs with a floor lamp {FLOOR_LAMP_REACH_RANGE_M[0]:.2f}-{FLOOR_LAMP_REACH_RANGE_M[1]:.2f} m away (edge to edge).",
        (f"The rug reaches under the sofa's front legs, spans at least {SITTING_RUG_MIN_WIDTH_RATIO:.0%} of the sofa width, "
         f"covers the coffee table zone and stays at least {WALL_FLUSH_MAX_GAP_M:.2f} m from the walls."),
    ))
