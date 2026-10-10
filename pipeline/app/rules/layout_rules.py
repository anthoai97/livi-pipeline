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

from app.rules.pipeline_shared import (
    CEILING_HEIGHT_M,
    DOOR_CLEARANCE,
    WALL_MOUNT_Z,
)
from app.rules.planner.fit_policy import SECTIONAL_MIN_ROOM  # noqa: F401 (re-exported)
from app.rules.room_policy import BEDROOM_FURNISHING_GUIDANCE, STUDIO_FURNISHING_GUIDANCE
from app.rules.layout.bedroom import BED_ACCESS_MIN_M, BED_HEADBOARD_MAX_GAP_M, BEDSIDE_HEAD_ZONE_M, BEDSIDE_MAX_GAP_M
from app.rules.layout.constants import DINING_ACCESS_MIN_M, DINING_LIGHT_TABLE_MIN_GAP_M
from app.rules.layout.comfort import SOFA_MEDIA_ACCESS_MIN_M, TV_VIEW_WIDTH_RANGE

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
        "A TV faces its viewers, and its back does not face another zone.",
        "Large pieces stand against a wall or anchor a zone; nothing floats in the middle of the room without a purpose.",
        "Walkways stay open from the door to each zone and between zones.",
        "Nothing taller than a window sill (about 0.9 m), such as a bookcase, cabinet or wardrobe, stands in front of a window.",
        "A floor lamp stands right at the end of the seat or bed it lights, within arm's reach, not across the room.",
    ),
    "living_room": (
        f"Leave the roomy end of {SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]:.2f}-{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[1]:.2f} m "
        "between the sofa and the coffee table, and about 0.45 m between an accent chair and the coffee table.",
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


def coordinate_system_block(room_width: float, room_depth: float) -> str:
    """Compact coordinate, wall index, and rotation primer."""
    return (
        f"COORDINATES: origin (0,0) bottom-left, +X right, +Y up. "
        f"Bounds x=[0, {room_width:.2f}], y=[0, {room_depth:.2f}]. "
        f"Position is the asset center. Keep the asset's projected half-extents inside every wall.\n"
        f"WALLS: 0=right (x={room_width:.2f}), 1=top (y={room_depth:.2f}), "
        f"2=left (x=0), 3=bottom (y=0).\n"
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
- Z-axis: floor items z=0; wall-mounted items snap to a wall at z={WALL_MOUNT_Z:.2f}m and must keep their full height within the {CEILING_HEIGHT_M:.2f}m wall; ceiling-mounted items hang so the asset top sits at z={CEILING_HEIGHT_M:.2f}m; items resting on another asset (bowl on table, vase on shelf, lamp on side_table, pillow on sofa, etc.) declare `on_top_of` with the parent uid and leave z=0 — the runtime computes z from the parent's top surface (seat height for sofas/chairs/beds)."""


P1_RULES = f"""P1 — ROOM ZONING, CIRCULATION & DESIGNABILITY (the room must be designable):
- Major walking paths stay ≥{PATH_MAJOR_M:.2f}m wide.
- Secondary access paths stay ≥{PATH_SECONDARY_M:.2f}m wide.
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
  * compute each group chair's own yaw as atan2(table_y-chair_y, table_x-chair_x). Its FRONT arrow should cross the associated tabletop footprint; aiming at another chair while both arrows miss the table does not satisfy this. Toe chairs inward when needed, using their rotated footprints to recheck clearance. For multiple seating groups, use each chair's own nearby table; task/dining chairs face their desk/dining table. Explicit user-facing directions and frozen edit scope take precedence; table-free conversation groups do not require an imaginary table.
  * desks face away from the wall behind them.
- Visual balance: prefer matched pairs flanking a larger anchor.
- Open living/dining zones keep at least 0.76m edge-to-edge clearance between the dining table footprint and living-zone seating or coffee tables.
- Anchor-first placement: place each anchor (sofa, bed, desk, tv) before the pieces that reference it (rug, coffee table, nightstand, media unit, side tables, lamps). Decor and plants last."""


_TIER_TEXT = {
    "P0": P0_RULES,
    "P1": P1_RULES,
    "P2": P2_RULES,
}


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
    if room_type == "bedroom":
        parts.append(f"""BEDROOM APPLICABILITY — takes precedence over living-only composition guidance:
- The complete bed is the sleeping anchor. Do not require a sofa, coffee table, TV, or conversation group. Living/dining/task rules apply only to actual selected groups.
- Compose the selected inventory as a furnished bedroom, not isolated objects on distant walls: group bedsides and lighting with the bed, and organize storage/mirror or selected reading pieces into useful areas. In spacious rooms use the available wall spans intentionally while preserving circulation; empty floor is not itself a defect. Do not add inventory during layout or override explicit sparse requests.
- Bed FRONT/yaw points toward its FOOT; BACK is the HEADBOARD. Depth is full head-to-foot model length, width is cross-bed width, not an inferred mattress size.
- Choose a usable wall for the headboard: full back edge within {BED_HEADBOARD_MAX_GAP_M:.2f}m and facing inward within 12 degrees of its normal. Do not violate openings to reach a wall.
- Keep at least one continuous side-access strip and the full foot-access strip at least {BED_ACCESS_MIN_M:.2f}m clear. Prefer both sides clear; honor explicit two-sided access requests. A compact bedroom may have single-side access, but not zero-side access.
- Only associated bedside furniture may occupy the upper {BEDSIDE_HEAD_ZONE_M:.2f}m head zone of a side strip; this is not an exemption for other furniture. Put nightstands beside the head, within {BEDSIDE_MAX_GAP_M:.2f}m edge gap. No detached or foot-end nightstands.
- Use the full unrounded model half-extents, with a positive 0.02–0.05m safety gap from walls and adjacent furniture. A rounded center can otherwise put a bed through a wall or nightstand. For parallel bed/nightstand orientations, lateral center separation must exceed bed.width/2 + nightstand.width/2; rotate this relationship with the bed. Do not round these extents down.
- Keep a {BED_ACCESS_MIN_M:.2f}m service band across each dresser/cabinet/wardrobe/storage front. This is access evidence, not a known drawer-extension dimension.
- Bedside lamps declare on_top_of their fitted nightstand/support; keep existing physical support and child-follow-parent rules. Use complete bed assets only, never assemble separate mattresses, frames, or bedding.
- A selected bedroom TV serves the bed: place it toward the FOOT, preferably centered opposite the headboard, with the bed front aimed toward the screen and the screen front aimed back toward the bed (both within 45 degrees). Use a clear wall span and the selected physical media support; a TV does not require a sofa or coffee table. Keep the full bed foot-access strip clear of its stand.
- A selected work setup is one desk/task-chair group: desk BACK toward the wall and FRONT toward the seated user, chair on the desk's FRONT side within its width, facing the desktop within 50 degrees and no more than 0.45m edge gap. Reserve another {BED_ACCESS_MIN_M:.2f}m behind the chair's full footprint for pull-out and access; neither bed/storage access nor doors/protected paths may be consumed. Check rotated footprints, not just chair centers. Do not add an unselected chair or desk during layout.
- Preserve the selected rug, mandatory decor plant, exact inventory, door/window/protected-path rules, and frozen edit scope. Model-chosen poses are validated, never silently repaired.""")
    elif room_type == "dining_room":
        parts.append(f"""DINING ROOM APPLICABILITY — takes precedence over living-only guidance:
- One standalone dining table anchors the selected dining chairs. No sofa, coffee table, media wall, bed, or wall-backed table is required. Place the table/chair group first; wall storage and decor follow. Preserve the exact selected inventory and frozen edit scope.
- Chair FRONT faces its associated table within 50 degrees, with a positive 0.08–0.20m edge gap (maximum 0.45m). Use model dimensions, never center distance. On rectangular tables, chairs along each edge face perpendicular to that edge; distribute them evenly with at least max(0.60m, chair width + 0.05m) seat pitch. Use opposing sides before crowding corners. On round/oval tables distribute seats around the perimeter and check their full rotated footprints.
- Every chair's FRONT ray must cross its tabletop (maximum miss 0.05m). Check the table's actual rotated long axis: three seats require a long edge; never line up three chairs across a narrow table end. Aim toward usable tabletop, not merely within an angle of its center.
- Validate capacity on the edges actually occupied. Each rectangular-table seat needs its full max(0.60m, chair width + 0.05m) segment within the tabletop edge, without sharing another diner's segment. For four chairs prefer two on each long side. Two chairs across a roughly 1m short end do not fit even if both front arrows hit the table. Determine the long edge from width/depth AND yaw, not the field name. End seating is valid when it has sufficient space or the user requests it; near-square and curved tables use their own balanced arrangements.
- Reserve {DINING_ACCESS_MIN_M:.2f}m clear behind every chair's full footprint for pull-out. This is additional to the chair depth and chair/table gap. Keep these bands inside the room and free of all other furniture. They may share empty circulation space. Keep door zones and protected routes clear in the seated layout; do not route entry through the tabletop.
- Selected sideboards/cabinets face inward with their backs at a usable wall and a {DINING_ACCESS_MIN_M:.2f}m clear front service band. Do not infer drawer extension from model bounds.
- Rugs are optional. If selected, center a sufficiently large rug under the table and all seated chairs; preferably extend it into chair pull-out space. An undersized decorative rug is a soft composition issue, not justification to remove selected furniture.
- Selected paintings mount parallel to a usable wall, FRONT inward, clear of openings, and relate visually to the dining table or sideboard. Selected pendant/chandelier lighting centers above the table and follows ceiling-height rules. Leave at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f}m from tabletop to the light's lowest model point. Do not shorten or rescale the model to make it fit. Multiple requested pendants may distribute evenly along the table. Do not place floor lamps where a chair pulls out.
- Reserve the complete table/chair/access envelope before placing optional extras. Consider rotating or repositioning the group or sideboard before sacrificing seating. Never change selected inventory during layout: report an infeasible optional item to selection feedback instead of squeezing diners onto the wrong table edges. Any selected centerpiece must leave each seated person's tabletop area usable.
- Check actual table/chair proportions and metadata; full chair height is not seat height, and table bounds do not establish leg clearance. Keep a positive safety gap when using rounded coordinates.""")
    if room_type == "studio":
        parts.append(f"""STUDIO APPLICABILITY — sleeping, sitting and dining coexist in one room:
- A floor-standing studio media support may stand between groups instead of against a wall. Preserve a usable freestanding TV/support pose, with cardinal heading, physical support, and clear circulation. This overrides the generic media-wall rule; wall-mounted displays still require a usable wall.
- Prefer a wall-backed media group when a usable wall span supports good viewing and circulation. Compare closer wall-backed arrangements before choosing a floating console. A TV is front-viewed: its back should not become the focal object of another zone. Use freestanding media only as a deliberate zone edge/divider, with a considered exposed back and clear circulation around it. Do not place it arbitrarily in the room center or force it against a distant wall at the expense of viewing comfort. Preserve usable existing placements during targeted edits.
- Reserve the full dining footprint including chair pull-out before filling the remaining wall spans with sleeping and sitting. For two diners, opposing seats are preferred when they fit; adjacent occupied edges are also valid when they preserve access. Compare both configurations and both table axes. Do not force opposing seats into a narrow strip or put a chair's back against a wall. If the dining group occupies one side of the room, bed/sofa/media must leave its service bands clear.
- Preserve every selected group and exact inventory. Place the bed, sofa and dining table as independent anchors first, reserving their access envelopes together; then their associated chairs, bedsides, surfaces and accessories. Use different usable wall spans for bed and sofa where needed, not one shared anchor position.
- Bed FRONT points toward the foot, BACK is the headboard. Keep the headboard within {BED_HEADBOARD_MAX_GAP_M:.2f}m of a usable wall, facing inward within 12 degrees. Reserve {BED_ACCESS_MIN_M:.2f}m continuous foot access and at least one side-access strip. Only a bedside serving the head may occupy the upper {BEDSIDE_HEAD_ZONE_M:.2f}m of a side strip; other groups remain obstacles. Nightstands sit beside the head within {BEDSIDE_MAX_GAP_M:.2f}m.
- Dining chairs face the actual table edge within 50 degrees; front rays cross the tabletop with 0.08–0.20m edge gaps (maximum 0.45m). Reserve max(0.60m, chair width + 0.05m) per seat on the occupied edge; prefer opposing long edges for rectangular tables. Never put multiple seats on a narrow end that lacks capacity. Reserve {DINING_ACCESS_MIN_M:.2f}m behind each full chair footprint for pull-out, free of beds, sofas and all other furniture. Round/oval tables use balanced perimeter seats.
- Every selected rug, light and surface belongs to a functional group. A sleeping rug need not reach the sofa or dining chairs; a dining rug covers its table/chairs, and a sitting rug serves its sofa/table. A dining pendant centers above its own table with at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f}m from tabletop to full model bottom. Other group lighting need not be above the dining table.
- TV viewing_target is sitting by default, bed when requested, or shared. Plan the TV/support and each intended viewer with opposing headings, facing each other within 45 degrees, and use the screen-size comfort facts below. Perpendicular headings only work on an exact diagonal; move the complete media group to a usable span instead. For shared viewing, place bed and sofa in separate spans facing the same media group. Keep screen/support aligned and physically supported, without blocking bed access. Do not force a bed-viewing TV to serve the sofa.
- Selected wardrobes/storage need {BED_ACCESS_MIN_M:.2f}m front service space. A requested workstation is one desk/task-chair pair, chair at the desk FRONT within 0.45m gap and facing it within 50 degrees, plus {BED_ACCESS_MIN_M:.2f}m behind the chair for pull-out.
- Keep entrance-to-sleeping, sitting and dining routes continuous; no route crosses furniture or consumes a required service band. All groups remain obstacles in every access check. Optional accessories must not steal core access. A coffee table is not mandatory when a useful sitting surface is absent or intentionally omitted for fit. Paintings remain parallel to usable walls, FRONT inward, clear of openings.
- Never silently remove a core group to make layout pass. Report measured infeasibility to selection/fit negotiation. Targeted edits preserve unrelated groups and saved viewing targets.""")
    if "P2" in tiers:
        parts.append(f"""MEDIA COMFORT AND RUG COMPOSITION:
- A media console must leave at least {SOFA_MEDIA_ACCESS_MIN_M:.2f}m in the full-width service band directly in front of a sofa. Measure rotated footprints, not center distance; side consoles outside the band are different. This is a functional requirement. Coffee-table gaps use their own existing rule. Move only editable items and preserve other access bands.
- Honor each TV's sitting/bed/shared viewing target. Prefer an estimated viewing distance of {TV_VIEW_WIDTH_RANGE[0]:.1f}–{TV_VIEW_WIDTH_RANGE[1]:.1f} times TV width. Estimate the seated viewer at sofa center minus 0.25*depth along FRONT; the bed viewer at its head edge plus 0.40m toward the foot; the screen at TV center plus half depth along FRONT. These are comfort heuristics, not measured eye positions or hard limits. For shared viewing check both viewers separately. Plan a usable media divider or nearer viewing group when a wall-to-wall arrangement is too distant; do not sacrifice circulation or frozen scope to meet a preference.
- A sitting area rug should engage the sofa front and extend into the seating/table area, not sit mainly underneath or behind the sofa. Prefer at least 0.30m beyond the sofa front and 35% of rug area in front of it, with no more than 0.10m gap from the sofa front to the rug's near edge. Its lateral span should cover at least 90% of sofa width and its sofa-local depth should be at least 0.90m; a small isolated mat is a soft size issue. Keep sitting rugs off other groups' beds and dining tables. Full-sofa coverage is unnecessary. These preferences must not move furniture into access bands or change selected inventory.
- For a centered sleeping area rug, balance under-bed depth with extension beyond the foot. Avoid placing most of the rug beyond the foot with less underlap than extension. Partial under-bed layouts and intentional bedside runners are valid. Respect each rug's selected functional group; never pull a sleeping rug toward dining or the sofa merely to satisfy another group's composition. Rug advice yields to access, bounds and frozen scope.""")
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
    layout is possible at all. This block translates the P0/P1 rules into
    choices the selector can act on (anchor wall length, circulation budget,
    sofa–table pairings, etc.).
    """
    if room_type == "studio":
        return (
            f"STUDIO FIT FOR SELECTION: {STUDIO_FURNISHING_GUIDANCE} "
            f"Reserve bed side/foot access of {BED_ACCESS_MIN_M:.2f}m, dining chair depth + 0.08m table gap "
            f"+ {DINING_ACCESS_MIN_M:.2f}m pull-out on occupied edges, and sitting/entry circulation together. "
            "Use full catalog dimensions and actual openings; a single group's fit is not proof the whole studio fits. "
            "Choose a complete standalone bed without embedded bedside furniture and a compact sofa/loveseat. "
            "A dining chair is never a substitute for sitting seating. Rugs and coffee tables are optional."
        )
    if room_type == "bedroom":
        return (
            "BEDROOM FIT FOR SELECTION: choose one complete bed using its full model width/depth, "
            "not an inferred mattress size. Reserve a usable headboard wall, at least "
            f"{BED_ACCESS_MIN_M:.2f}m side access on one side and {BED_ACCESS_MIN_M:.2f}m foot access; "
            "prefer both sides accessible. Keep nightstands beside the head, and verify lamps "
            "fit their exact supports. Plan useful bedside lighting and clothing storage; in a spacious normal bedroom, "
            "prefer balanced bedside service and consider a coherent dressing or reading area when useful. "
            "Honor explicit minimal, single-bedside and no-storage requests. Sparse density is only a guardrail, "
            "not proof of a complete design. Allow dresser/cabinet/wardrobe front access, required rug/plant, "
            "openings and protected routes. A sofa/coffee table/media group is not required. "
            "TV and desk/task-chair setups are opt-in: select them when requested, not as default bedroom filler. "
            f"{BEDROOM_FURNISHING_GUIDANCE} "
            "For TV, reserve a view from the bed foot toward the screen and fit the selected media support without blocking bed access. "
            "For a working setup, select a desk and suitable task chair as a usable pair, reserving the chair footprint "
            f"plus {BED_ACCESS_MIN_M:.2f}m pull-out space behind it. Honor explicit desk-only requests and exclusions. "
            "Never substitute a separate frame, mattress or bunk for a complete adult bed."
        )
    if room_type == "dining_room":
        return (
            "DINING FIT FOR SELECTION: choose a standalone dining table and dining chairs of compatible scale. "
            "For rectangular tables reserve at least max(0.60m, chair width + 0.05m) per seat along each occupied edge. "
            "Round/oval tables need evenly spaced seats and room for their rotated footprints. "
            f"Allow chair depth + 0.08m table gap + {DINING_ACCESS_MIN_M:.2f}m pull-out on each occupied side, "
            "plus clear doors and protected routes. Full model height is not seat height. "
            f"Selected sideboards/cabinets need {DINING_ACCESS_MIN_M:.2f}m front access; "
            f"overhead lights need at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f}m between tabletop and model bottom, "
            "using the full model height with its top at ceiling height. Never assume adjustable hangers. "
            "Requested rugs should cover the table and seated chairs. Select compact compatible pieces before "
            "reducing any optional seating count; never reduce a mandatory count."
        )
    min_w, min_d = SECTIONAL_MIN_ROOM
    sectional_ok = (room_width >= min_w and room_depth >= min_d) or (
        room_depth >= min_w and room_width >= min_d
    )
    sectional_note = (
        "- Sectionals fit this room."
        if sectional_ok
        else (
            f"- Room is below {min_w:.2f}×{min_d:.2f}m — skip sectionals; "
            "use a loveseat or 2-seat sofa as the anchor."
        )
    )
    return (
        "LAYOUT FEASIBILITY FOR SELECTION (pick a set the layout stage can actually place):\n"
        "- Wall-aligned anchors (sofa/sectional/loveseat/bed/tv_stand/media_unit/bookcase/"
        "cabinet/sideboard/dresser) must sit flush to a wall. Their combined width must not "
        "exceed the total usable wall length after doors, fireplaces, edge buffers, and any "
        "windows that the anchor cannot sit in front of are subtracted.\n"
        f"- Circulation reserves {PATH_MAJOR_M:.2f}m-wide major paths between furniture groups "
        f"and {PATH_SECONDARY_M:.2f}m-wide access bands to single pieces. Plan ~20–30% of the "
        "floor for walkways — do not over-fill.\n"
        f"{sectional_note}\n"
        "- Every sofa/sectional pairs with a coffee table sized to sit "
        f"{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]:.2f}–{SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[1]:.2f}m "
        "in front (edge-to-edge, sofa front face to table's nearest face), plus at least one "
        "side table or floor lamp within arm's reach.\n"
        "- If the room is TV-focused, pick a single clean media setup that can center on the focal "
        "wall opposite the primary sofa instead of forcing off-axis media placement.\n"
        "- A TV requires a tv_stand or media_unit, and that TV wall span must stay clear of "
        "doors and windows. A reading chair pairs with a floor lamp "
        "0.30–0.46m away (edge-to-edge).\n"
        "- The primary rug must reach under the front legs of the sofa and cover the coffee-"
        f"table zone, keeping ≥{WALL_FLUSH_MAX_GAP_M:.2f}m clearance from walls (rug edge to wall)."
    )
