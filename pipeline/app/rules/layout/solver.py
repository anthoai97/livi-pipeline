"""Code layout solver: groups, templates, candidates, beam search, accessories, checker score.

The selected instances are split into groups by their seed-guidance roles
(app.rules.planner.seed_guidance): a bed with its bedside pieces, a sofa with its
coffee table, accent seats, side tables and sitting rug, a media support with its
TV, a dining table with its chairs and ceiling lights, a desk with its chair, and
each other wall or floor piece alone. A group's templates hold quarter-turn poses
from the ported rules: the canonical living-group poses, the dining fit envelopes
and chair offsets, and the seed's bedside, side-table and desk-chair targets. They
also name the service bands their members need clear (bed sides and foot, chair
pull-out, desk-chair pull-out, sofa front, storage fronts where the room measures
them), built like comfort's sofa band.

Wall groups take the seed's wall candidates for the group's footprint box, facing
into the room. The sofa keeps the wall gap sofa_wall_gap accepts, so a TV reaches
its preferred viewing range by the choice of wall pair, or by a media group pulled
off its wall as far as the wall-flush check allows. The dining group goes on the
legacy repack grid in both orientations; a media group (a support with its TV, a
floor-standing TV, or a wall-mounted TV with any support flush under it) on the
walls, preferring the one its viewer faces. In a
studio, a TV on a support the studio checks let stand free also gets divider
candidates on its viewer's axis, inside its viewing range; comfort marks a
floating support, so a wall-backed group in range still wins. Furniture taller
than the screen's bottom (not the viewer or a coffee table) costs more when it stands
in the strip between the viewer and the screen, before or after the TV. Floor lamps take
the legacy service-slot targets beside each lounge seat (dining chairs only in a
room without one), ranked by the floor lamp reach rule (when none passes the
checks, the accessory pass places them). Single pieces, and accent seats the
chosen sitting template left out, take the seed's own candidates for their
placement mode.

Groups are placed in order: sleeping and sitting (largest first), the left-out
seats, media, dining, work, floor lamps, wall pieces, floor pieces. A candidate
must first pass the seed's cheap checks (inside the room, clear of placed
furniture, door clearances, protected paths, windows for TV items) plus the
reserved bands and the living-dining gap. The best SHORTLIST candidates of each
partial layout are measured with analyze_layout, and the best BEAM_WIDTH partial
layouts continue, the best of each zone plan first: the wall each sleeping,
sitting, media, or work group backs onto (or a divider TV) and the room quadrant
of the dining table. A dining table prefers the room center, except in a studio
or living room, where it prefers a wall.

Each complete group layout then gets its accessories (table
lamps, tabletop items, wall art, other rugs and ceiling lights, floor TVs) from
the seed generator, with each table lamp first paired with a free bedside piece,
side table or, for a desk lamp, desk. It is normalized the way the place stage
measures layouts and scored with layout_issue_score, then
layout_issue_magnitudes. The best wins; the others are reported as alternatives,
from which the place stage may pick a tie. A group with no candidate that passes the checks takes
its least-violating pose, and an accessory the seed skips takes a free spot, so
the layout is always complete; their instance keys are reported as unplaceable.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from app.rules.categories import (
    ceiling_mount_z,
    is_table_lamp_support_asset,
    requires_window_clearance,
    support_top_z,
)
from app.rules.geometry.candidates import (
    _dedupe_candidates,
    _dynamic_preferred_walls,
    _grid_candidates,
    _guided_candidates,
    _placement_candidates,
    _support_surface_fits,
    _wall_candidates,
)
from app.rules.geometry.generation import generate_deterministic_layout_with_report
from app.rules.geometry.living_group import (
    canonical_living_group_poses,
    oriented_living_group_bounds,
)
from app.rules.geometry.placement import _search_placement
from app.rules.geometry.primitives import (
    _SEED_MARGIN,
    _blocker_polygons,
    asset_polygon,
    frange,
    is_rug,
    media_display_fits_support_dimensions,
    normalize_rotation,
    projected_half_extents,
    room_polygon,
)
from app.rules.layout.analysis import analyze_layout
from app.rules.layout.bedroom import BED_ACCESS_MIN_M, is_bedside_asset
from app.rules.layout.comfort import TV_VIEW_WIDTH_RANGE
from app.rules.layout.constants import (
    COFFEE_TABLE_ROLE_KEYWORDS,
    CRITICAL_LIVING_GROUP_KINDS,
    FLOOR_LAMP_REACH_RANGE_M,
    FLOOR_LAMP_WALL_MAX_GAP_M,
    LIVING_DINING_CLEARANCE_M,
    MEDIA_CENTERLINE_MAX_OFFSET_M,
    PROTECTED_PATH_MIN_OVERLAP_SQM,
    WINDOW_SEATING_CLEARANCE_M,
)
from app.rules.layout.dining import (
    dining_fit_envelopes,
    dining_placement_facts,
)
from app.rules.layout.metrics import (
    FloorAsset,
    layout_issue_magnitudes,
    layout_issue_score,
)
from app.rules.layout.normalization import _service_slot_candidates, normalize_layout
from app.rules.layout.relations import (
    _angle_delta_deg,
    _angle_to,
    _back_wall_name,
    _facing_axis_and_sign,
    _wall_mount_z,
    _window_seating_clearance_poly,
    floor_assets,
)
from app.rules.layout.studio import SITTING_CATEGORIES, is_freestanding_studio_media_support
from app.rules.layout.validation_functional import (
    compute_living_group_cohesion_violations,
    compute_sofa_wall_gap_violations,
)
from app.rules.layout_rules import (
    SOFA_BACK_WALL_DISTANCE_RANGE_M,
    SOFA_COFFEE_TABLE_DISTANCE_RANGE_M,
    WALL_FLUSH_MAX_GAP_M,
)
from app.rules.pipeline_shared import is_table_lamp_asset, matches_category_keywords
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.seed_guidance import build_seed_guidance, build_seed_layout_plan
from app.rules.planner.taxonomy import SEATING_ROLE_KEYWORDS, normalize_category
from app.rules.protected_paths import protected_path_polygons

Record = dict[str, Any]
Pose = tuple[float, float, float]  # x, y, yaw

BEAM_WIDTH = 4  # partial layouts kept after each group
SHORTLIST = 6  # checked candidates per partial layout measured with analyze_layout
LAMP_SHORTLIST = 2  # the same for a floor lamp, whose preference already ranks its reach rule
DINING_OPTIONS = 3  # dining_fit_envelopes chair arrangements tried: the two most balanced and the smallest
DINING_SQUARE_RATIO = 0.8  # a table this near square (or round) seats its chairs on all four edges
DINING_UNBALANCED_COST = 0.5  # template cost of a chair arrangement other than the most balanced
# The middle and low end of the sofa-to-table range, as legacy repack_living_group tries.
SOFA_TABLE_GAPS_M = (sum(SOFA_COFFEE_TABLE_DISTANCE_RANGE_M) / 2, SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0] + 0.01)
SOFA_WALL_GAP_M = sum(SOFA_BACK_WALL_DISTANCE_RANGE_M) / 2
ACCESS_M = BED_ACCESS_MIN_M  # every service band; the bed, dining, storage, and sofa-console checks all use 0.56 m
MEDIA_PULL_M = WALL_FLUSH_MAX_GAP_M - _SEED_MARGIN - 0.01  # a pulled media group stays inside the wall-flush gap
ZONE_KINDS = ("sleeping", "sitting", "media", "dining", "work")  # the functional groups a zone plan names
DINING_WALL_COST = 0.1  # per metre from a studio or living-room dining group to its nearest wall
_WALL_MODES = {"anchor_wall", "focal_wall", "support_wall", "wall", "desk_wall"}  # seed modes that take wall candidates
_STORAGE_FRONTS = {  # storage whose front band the room's checker measures (bedroom.py, dining.py)
    "bedroom": {"dresser", "cabinet", "wardrobe", "storage_unit", "sideboard"},
    "studio": {"dresser", "cabinet", "wardrobe", "storage_unit", "sideboard"},
    "dining_room": {"sideboard", "cabinet", "storage_unit"},
}


@dataclass(frozen=True)
class _Template:
    name: str
    poses: dict[str, Pose]  # in the group frame: wall groups face +y with the wall behind y = 0
    bands: tuple[tuple[str, str], ...] = ()  # (uid, front | back | left | right) bands other furniture leaves clear
    penalty: float = 0.0


@dataclass(frozen=True)
class _Group:
    kind: str  # sleeping, sitting, seat, media, dining, work, lamp, wall, free
    templates: tuple[_Template, ...]


@dataclass
class _Partial:
    layout: Record = field(default_factory=dict)
    furniture: list[Polygon] = field(default_factory=list)
    bands: list[Polygon] = field(default_factory=list)
    living: list[Polygon] = field(default_factory=list)
    dining: list[Polygon] = field(default_factory=list)
    seats: list[Polygon] = field(default_factory=list)  # what the floor lamp reach check measures from
    tall: list[Polygon] = field(default_factory=list)  # placed footprints that would block the TV (_blocks_view)
    sightline: Polygon | None = None  # set with the media group: the strip from the viewer to the screen
    preference: float = 0.0
    score: tuple[int, int, int] = (0, 0, 0)
    unplaceable: tuple[str, ...] = ()
    key: tuple = ()  # the zone plan: (kind, back wall | "divider" | dining table quadrant) per placed zone


@dataclass
class _Room:
    instances: list[Record]
    assets: dict[str, Record]
    room: Record
    area: tuple[float, float]
    polygon: Polygon
    cover: Polygon
    doors: list[Polygon]
    windows: list[Polygon]
    seating_windows: list[Polygon]  # the zones sofa_wall_gap keeps seating out of
    seats: set[str]
    paths: list[Polygon]
    guidance: Record
    plan: Record
    supports: dict[str, str] = field(default_factory=dict)  # TV uid -> the media support it stands on
    mounted: dict[str, str] = field(default_factory=dict)  # wall TV uid -> the media support under it, or ""
    rug: str | None = None  # the sitting group's rug
    # viewer, bed, tv, low, high, inset: the TV distance the viewer's pose should reach; screen_z: the screen's
    # bottom, which taller floor furniture hides (None for a floor TV)
    view: Record | None = None
    stats: Record = field(default_factory=lambda: {"candidates": 0, "scored": 0})

    def dims(self, uid: str) -> tuple[float, float]:
        asset = self.assets[uid]
        return float(asset.get("width", 0.5) or 0.5), float(asset.get("depth", 0.5) or 0.5)

    def floor(self, uid: str) -> bool:
        """Furniture with a floor footprint: not a rug, a mounted or tabletop item, or a supported TV."""
        asset = self.assets[uid]
        return (not is_rug(uid, asset) and uid not in self.supports
                and placement_mode_for_asset(asset) not in {"wall_mounted", "ceiling_mounted", "tabletop"})

    def mount_z(self, uid: str) -> float:
        """The bottom z of a wall-mounted item, above the support under a wall TV."""
        support = self.mounted.get(uid)
        return _wall_mount_z(self.assets[uid], float(self.assets[support].get("height") or 0) if support else 0.0)

    def analyze(self, layout: Record) -> Record:
        room = self.room
        self.stats["scored"] += 1
        return analyze_layout(layout, self.instances, room["room_vertices"], room["room_doors"], self.area,
                              room_windows=room["room_windows"], protected_paths=room["protected_paths"],
                              room_type=room["room_type"])


def solve_layout(instances: list[Record], room: Record, intent: Record) -> tuple[Record, Record]:
    """Place every selected instance with the code solver.

    instances: the selected instance records (uid = instance_key). room:
    build_room_context output. intent: the cleaned intent packet.
    Returns (layout, report): layout is keyed by instance key and normalized like
    the place stage's measure; report holds `unplaceable` (instance keys placed
    without a pose that passes the checks), `score` (layout_issue_score of the
    layout), `candidates` (group candidates checked), `scored` (layouts measured
    with analyze_layout), `elapsed` (seconds), and `alternatives`: the other
    complete beam layouts, best first, each as {layout, score, unplaceable};
    a layout identical to one already listed is left out.
    """
    started = time.monotonic()
    ctx = _room(instances, room, intent)
    beam = [_Partial()]
    for group in _groups(ctx):
        children = [child for parent in beam for child in _expand(ctx, parent, group)]
        fewest = min((len(child.unplaceable) for child in children), default=0)
        beam = _pick([child for child in children if len(child.unplaceable) == fewest], BEAM_WIDTH,
                     key=lambda child: (child.score, child.preference)) or beam
    complete = []
    for partial in beam:
        layout, skipped = _accessorize(ctx, partial)
        layout = normalize_layout(layout, instances, room["room_vertices"], room_windows=None, rehome_service_items=False)
        issues = ctx.analyze(layout)
        unplaceable = sorted({*partial.unplaceable, *skipped})
        score = (layout_issue_score(issues), layout_issue_magnitudes(issues), len(unplaceable), partial.preference)
        complete.append((score, layout, unplaceable))
    complete.sort(key=lambda row: row[0])  # stable: a tie keeps beam order
    (score, layout, unplaceable), *others = complete
    alternatives: list[Record] = []
    for other_score, other, other_unplaceable in others:
        if other != layout and all(other != alternative["layout"] for alternative in alternatives):
            alternatives.append({"layout": other, "score": list(other_score[0]), "unplaceable": other_unplaceable})
    return layout, {"unplaceable": unplaceable, "score": list(score[0]), **ctx.stats,
                    "elapsed": round(time.monotonic() - started, 3), "alternatives": alternatives}


def _room(instances: list[Record], room: Record, intent: Record) -> _Room:
    area = tuple(room["room_area"])
    polygon = room_polygon(area, room["room_vertices"])
    seating = {str(asset["uid"]): normalize_category(asset.get("category")) for asset in instances
               if matches_category_keywords(asset.get("category", ""), str(asset["uid"]), SEATING_ROLE_KEYWORDS)}
    # A floor lamp serves lounge seats; dining chairs count only in a room without one.
    lounge = {uid for uid, category in seating.items() if category != "dining_chair"}
    guidance = build_seed_guidance(room_facts=room["facts"], intent_packet=intent,
                                   feasibility_digest=room["digest"], selected_assets=instances)
    return _Room(
        instances=instances,
        assets={str(asset["uid"]): asset for asset in instances},
        room=room,
        area=area,
        polygon=polygon,
        cover=polygon.buffer(1e-6),
        doors=_blocker_polygons(room["room_doors"], None, boundary=room["room_vertices"], room_area=area),
        windows=_blocker_polygons(None, room["room_windows"], boundary=room["room_vertices"], room_area=area),
        seating_windows=[_window_seating_clearance_poly(window, room["room_vertices"]) for window in room["room_windows"]],
        seats=lounge or set(seating),
        paths=protected_path_polygons(room["protected_paths"]),
        guidance=guidance,
        plan=build_seed_layout_plan(instances, guidance),
    )


# --- groups and templates ----------------------------------------------------


def _groups(ctx: _Room) -> list[_Group]:
    """The search groups in placement order; sets ctx.supports and ctx.view. Items left out are accessories."""
    roles = ctx.guidance["role_uids"]
    anchors = ctx.plan.get("anchor_by_uid") or {}
    room_type = ctx.room["room_type"]
    studio = room_type == "studio"
    taken: set[str] = set()

    def pick(role: str, anchor: str | None = None) -> list[str]:
        return [uid for uid in roles.get(role) or [] if uid not in taken
                and (not studio or anchor is None or anchors.get(uid) == anchor)]

    def take(*uids: str | None) -> None:
        taken.update(uid for uid in uids if uid)

    category = {uid: normalize_category(asset.get("category")) for uid, asset in ctx.assets.items()}
    bed = next(iter(pick("sleeping_anchor")), None)
    sofa = next(iter(pick("anchor")), None)
    take(bed, sofa)
    bedsides = [uid for uid in pick("bedside", bed) + pick("side_surface", bed)
                if bed and (category[uid] == "nightstand" or is_bedside_asset(ctx.assets[uid]))][:2]
    take(*bedsides)
    tables = pick("center_surface", sofa) if sofa else []
    table = next((uid for uid in tables if matches_category_keywords(category[uid], uid, COFFEE_TABLE_ROLE_KEYWORDS)),
                 next(iter(tables), None))
    take(table)
    seats = (pick("conversation_support", sofa) + pick("anchor")) if sofa and table else []
    take(*seats)
    sides = pick("side_surface", sofa)[:2] if sofa else []
    take(*sides)
    rug = next((uid for uid in pick("rug") if (anchors.get(uid) == sofa if studio else room_type == "living_room")),
               None) if sofa else None
    take(rug)
    ctx.rug = rug

    media = [uid for uid in pick("media") if category[uid] not in {"tv", "projector"}][:1]
    tvs = [uid for uid in pick("media") if category[uid] == "tv" and media
           and placement_mode_for_asset(ctx.assets[uid]) == "tabletop"
           and str(ctx.assets[uid].get("paired_support_uid") or media[0]) == media[0]
           and media_display_fits_support_dimensions(*ctx.dims(uid), *ctx.dims(media[0]))][:1]
    ctx.supports = {tv: media[0] for tv in tvs}
    floor_tv = None if tvs else next((uid for uid in pick("media") if category[uid] == "tv" and ctx.floor(uid)), None)
    wall_tv = None if tvs or floor_tv else next((uid for uid in pick("media") if category[uid] == "tv"
                                                and placement_mode_for_asset(ctx.assets[uid]) == "wall_mounted"), None)
    ctx.mounted = {wall_tv: next(iter(media), "")} if wall_tv else {}
    screen = (*media, *tvs) if tvs else (*media, wall_tv) if wall_tv else (floor_tv,) if floor_tv else tuple(media)
    # Outside studios, a floor TV goes after the media support it does not stand on, which media focal alignment centers.
    split = bool(floor_tv and media) and not studio
    take(*screen, *(media if split else ()))
    tv = next(iter(tvs), floor_tv or wall_tv)
    target = next((ctx.assets[uid].get("viewing_target") for uid in (tv, *media) if uid and ctx.assets[uid].get("viewing_target")), None)
    viewer = (bed if target == "bed" or not sofa else sofa) if studio else (sofa or bed)
    if tv and viewer:
        low, high = (ctx.dims(tv)[0] * factor for factor in TV_VIEW_WIDTH_RANGE)
        # The screen point sits half the TV depth in front of the TV center (comfort).
        screen_inset = (ctx.dims(media[0])[1] + ctx.dims(tv)[1]) / 2 if tvs else ctx.dims(tv)[1]
        screen_z = float(ctx.assets[media[0]].get("height") or 0) if tvs else ctx.mount_z(tv) if wall_tv else None
        ctx.view = {"viewer": viewer, "bed": viewer == bed, "tv": tv, "screen_z": screen_z,
                    "low": low, "high": high, "inset": _SEED_MARGIN + screen_inset}

    dining_table = next(iter(pick("dining_table")), None)
    chairs = pick("dining_chair", dining_table) if dining_table else []
    lights = [uid for uid, asset in ctx.assets.items() if dining_table and uid not in taken
              and placement_mode_for_asset(asset) == "ceiling_mounted"
              and (not studio or asset.get("functional_group") == "dining")]
    take(dining_table, *chairs, *lights)
    desk = next(iter(pick("desk")), None)
    office_chair = next(iter(pick("office_chair")), None) if desk else None
    take(desk, office_chair)

    anchored = [
        group for group in (
            _Group("sleeping", _sleeping_templates(ctx, bed, bedsides)) if bed else None,
            _Group("sitting", _sitting_templates(ctx, sofa, table, seats, sides, rug, bool(screen))) if sofa else None,
        ) if group and group.templates
    ]
    groups = sorted(anchored, key=lambda group: -_footprint_area(ctx, group.templates[0]))
    for members in ((tuple(media), screen) if split else (screen,) if screen else ()):
        # Flush, or pulled toward the viewer (only worth its cost when the flush TV is past its range).
        # A wall TV and the support under it stay flush, each on the wall line.
        groups.append(_Group("media", tuple(
            _Template(name, {uid: (0.0, ctx.dims(uid if wall_tv else members[0])[1] / 2 + pull, math.pi / 2) for uid in members},
                      penalty=penalty)
            for name, pull, penalty in (("media", 0.0, 0.0), ("media pulled", MEDIA_PULL_M, 0.05)) if not (wall_tv and pull))))
    if dining_table:
        groups.append(_Group("dining", _dining_templates(ctx, dining_table, chairs, lights)))
    if desk:
        groups.append(_Group("work", _work_templates(ctx, desk, office_chair)))
    fronts = _STORAGE_FRONTS.get(room_type, set())
    singles = []
    for uid in sorted(ctx.assets, key=lambda uid: -math.prod(ctx.dims(uid))):
        if uid in taken or not ctx.floor(uid):
            continue
        mode = ctx.plan["placement_mode_by_uid"].get(uid)
        kind = "lamp" if mode == "beside_seat" else "wall" if mode in _WALL_MODES else "free"
        bands = ((uid, "front"),) if category[uid] in fronts else ()
        singles.append(_Group(kind, (_Template(uid, {uid: (0.0, 0.0, 0.0)}, bands),)))
    seat_groups = [_Group("seat", (_Template(uid, {uid: (0.0, 0.0, 0.0)}),)) for uid in seats]
    sitting = next((n + 1 for n, group in enumerate(groups) if group.kind == "sitting"), 0)
    return (groups[:sitting] + seat_groups + groups[sitting:]
            + sorted(singles, key=lambda group: ("lamp", "wall", "free").index(group.kind)))


def _footprint_area(ctx: _Room, template: _Template) -> float:
    return sum(math.prod(ctx.dims(uid)) for uid in template.poses if ctx.floor(uid))


def _overlapping(ctx: _Room, poses: dict[str, Pose]) -> bool:
    polygons = [asset_polygon([x, y, 0.0], yaw, *ctx.dims(uid)) for uid, (x, y, yaw) in poses.items() if ctx.floor(uid)]
    return any(a.intersects(b) for n, a in enumerate(polygons) for b in polygons[n + 1:])


def _sleeping_templates(ctx: _Room, bed: str, bedsides: list[str]) -> tuple[_Template, ...]:
    """Headboard on the wall line; bedside pieces at the seed's beside_bed_head targets.

    The bed needs its foot band and at least one side band clear; a one-sided
    template costs a little more, so both sides stay clear when the room allows.
    """
    width, depth = ctx.dims(bed)
    sides = [(1, -1)] if len(bedsides) != 1 else [(1,), (-1,)]
    templates = []
    for signs in sides:
        poses = {bed: (0.0, depth / 2, math.pi / 2)}
        for uid, sign in zip(bedsides, signs, strict=False):
            # Seed beside_bed_head: head-aligned, 0.05 m beside the bed, lateral (-sin, cos) of the bed yaw.
            poses[uid] = (-sign * (width / 2 + ctx.dims(uid)[0] / 2 + 0.05), ctx.dims(uid)[1] / 2, math.pi / 2)
        for name, penalty, bands in (("both", 0.0, ("left", "right")), ("left", 0.5, ("left",)), ("right", 0.5, ("right",))):
            templates.append(_Template(f"bed {signs} {name}", poses, ((bed, "front"), *((bed, side) for side in bands)), penalty))
    return tuple(templates)


def _sitting_templates(
    ctx: _Room, sofa: str, table: str | None, seats: list[str], sides: list[str], rug: str | None, media: bool
) -> tuple[_Template, ...]:
    """Canonical living-group arrangements with the sofa SOFA_WALL_GAP_M off the wall line.

    Side tables take the seed's beside_anchor targets. The sitting rug starts
    0.2 m under the sofa front and runs ahead of it, along the sofa (the seed
    centers it on the sofa, which comfort's rug rules flag as behind the sofa).
    With media, seats facing the sofa cost more, so they do not block the TV.
    """
    dims = {uid: ctx.dims(uid) for uid in (sofa, table, *seats) if uid}
    arrangements = (
        canonical_living_group_poses(sofa_uid=sofa, coffee_table_uid=table, accent_seat_uids=seats,
                                     dimensions=dims, sofa_table_gaps=list(SOFA_TABLE_GAPS_M))
        if table else [("sofa", {sofa: (0.0, dims[sofa][1] / 2, math.pi / 2)})]
    )
    if table and seats:
        # Side seats may also sit nearer the sofa end of the table side; their front ray still meets the table.
        half = {name: projected_half_extents(*dims[table], relative[table][2])[1] for name, relative in arrangements}
        arrangements += [
            (f"{name} slid", {uid: (x, y - half[name] / 2 if yaw in {0.0, math.pi} and uid in seats else y, yaw)
                              for uid, (x, y, yaw) in relative.items()})
            for name, relative in arrangements if any(relative[uid][2] in {0.0, math.pi} for uid in seats)
        ]
        arrangements += [("core", {uid: pose for uid, pose in relative.items() if uid in {sofa, table}})
                         for name, relative in arrangements if name == "chairs_opposite"]
    sofa_width, sofa_depth = dims[sofa]
    back = SOFA_WALL_GAP_M - _SEED_MARGIN
    templates = []
    for name, relative in arrangements:
        poses = {uid: (x, y + back, yaw) for uid, (x, y, yaw) in relative.items()}
        sofa_x, sofa_y, _ = poses[sofa]
        for n, uid in enumerate(sides):  # seed beside_anchor: 0.08 m beside the sofa, at its center
            poses[uid] = (sofa_x + (1 if n % 2 else -1) * (sofa_width / 2 + ctx.dims(uid)[0] / 2 + 0.08), sofa_y, math.pi / 2)
        if rug:
            rug_width, rug_depth = ctx.dims(rug)
            yaw = math.pi / 2 if rug_width >= rug_depth else 0.0
            poses[rug] = (sofa_x, sofa_y + sofa_depth / 2 - 0.2 + min(rug_width, rug_depth) / 2, yaw)
        if _overlapping(ctx, poses):
            continue
        facing = sum(1 for uid in seats if uid in relative and relative[uid][2] == -math.pi / 2)
        # Without its seats (they then take the seed's around-anchor candidates), only when nothing else fits.
        penalty = 1.0 if name == "core" else (0.3 * facing if media else 0.0) + (0.05 if name.endswith(" slid") else 0.0)
        templates.append(_Template(name, poses, ((sofa, "front"),), penalty))
    return tuple(templates)


def _dining_templates(ctx: _Room, table: str, chairs: list[str], lights: list[str]) -> tuple[_Template, ...]:
    """dining_fit_envelopes arrangements, the most balanced first, chairs at dining_placement_facts offsets.

    A rectangular table splits its chairs evenly across its two long edges, using
    the ends for an odd chair or when the long edges are full; a near-square or
    round one spreads them evenly over all four edges. The next most balanced
    arrangement and the smallest envelope follow at a cost, for rooms the balanced
    one does not fit. Chairs on one edge sit at even spacing, centered on it (one
    chair at the center, two at the quarter points, as legacy
    _arrange_dining_chairs_around_tables), and face the table; each keeps its
    pull-out band clear. Ceiling lights hang over the table center.
    """
    assets = [ctx.assets[uid] for uid in (table, *chairs)]
    offsets = {row["chair"]: row["center_offset_from_table_m"] for row in (dining_placement_facts(assets) or [{"chairs": []}])[0]["chairs"]}
    width, depth = ctx.dims(table)
    lengths = (width, width, depth, depth)  # front, back, left, right, as dining_fit_envelopes counts them
    square = min(width, depth) >= DINING_SQUARE_RATIO * max(width, depth)

    def imbalance(option: Record) -> tuple:
        front, back, left, right = option["chairs_per_edge"]
        if square:
            return max(option["chairs_per_edge"]) - min(option["chairs_per_edge"]), abs(front - back) + abs(left - right)
        sides, ends = ((front, back), (left, right)) if width >= depth else ((left, right), (front, back))
        return abs(sides[0] - sides[1]), abs(ends[0] - ends[1]), sum(ends)

    options = dining_fit_envelopes(ctx.assets[table], [ctx.assets[uid] for uid in chairs])  # smallest envelope first
    chosen = sorted(options, key=imbalance)[:DINING_OPTIONS - 1]  # stable: ties keep the smaller envelope
    chosen += [option for option in options[:1] if option not in chosen]
    templates = []
    for rank, option in enumerate(chosen or [{"chairs_per_edge": [0, 0, 0, 0]}]):
        poses = {table: (0.0, 0.0, math.pi / 2), **{uid: (0.0, 0.0, math.pi / 2) for uid in lights}}
        queue = list(chairs)
        for edge, count, length in zip(("front", "back", "left", "right"), option["chairs_per_edge"], lengths, strict=True):
            for n in range(count):
                uid = queue.pop(0)
                along = (n + 0.5) * length / count - length / 2
                normal = offsets[uid]["front_or_back" if edge in {"front", "back"} else "left_or_right"]
                poses[uid] = {"front": (along, normal, 3 * math.pi / 2), "back": (along, -normal, math.pi / 2),
                              "left": (-normal, along, 0.0), "right": (normal, along, math.pi)}[edge]
        templates.append(_Template(f"dining {option['chairs_per_edge']}", poses, tuple((uid, "back") for uid in chairs),
                                   DINING_UNBALANCED_COST if rank else 0.0))
    return tuple(templates)


def _work_templates(ctx: _Room, desk: str, chair: str | None) -> tuple[_Template, ...]:
    """Desk on the wall line; its chair at the seed's front_of_desk target (0.35 m), facing the desk."""
    depth = ctx.dims(desk)[1]
    poses = {desk: (0.0, depth / 2, math.pi / 2)}
    if chair:
        poses[chair] = (0.0, depth + 0.35 + ctx.dims(chair)[1] / 2, 3 * math.pi / 2)
    return (_Template("desk", poses, ((chair, "back"),) if chair else ()),)


# --- candidates and checks ---------------------------------------------------


def _turn(x: float, y: float, quarter_turns: int) -> tuple[float, float]:
    """Rotate like oriented_living_group_bounds."""
    angle = quarter_turns * math.pi / 2
    cos, sin = round(math.cos(angle)), round(math.sin(angle))
    return x * cos - y * sin, x * sin + y * cos


def _placements(ctx: _Room, parent: _Partial, group: _Group, template: _Template) -> list[tuple[tuple, dict[str, Pose]]]:
    """World poses for each candidate position of a template, with a diversity key."""
    dims = {uid: ctx.dims(uid) for uid in template.poses}
    result = []
    if group.kind == "dining":
        step = max(0.25, min(ctx.area) / 14.0)  # legacy repack_living_group grid step
        min_x, min_y, max_x, max_y = ctx.polygon.bounds
        for quarter in (0, 1):
            oriented, _ = oriented_living_group_bounds(template.poses, dims, quarter)
            local = unary_union([*(asset_polygon([x, y, 0.0], yaw, *dims[uid]) for uid, (x, y, yaw) in oriented.items()),
                                 *_bands(ctx, template, oriented)])
            x0, y0, x1, y1 = local.bounds
            center = ((min_x + max_x - x0 - x1) / 2, (min_y + max_y - y0 - y1) / 2)
            xs = sorted(frange(min_x - x0, max_x - x1, step), key=lambda value: abs(value - center[0]))
            ys = sorted(frange(min_y - y0, max_y - y1, step), key=lambda value: abs(value - center[1]))
            for ox in xs:
                for oy in ys:
                    poses = {uid: (x + ox, y + oy, yaw) for uid, (x, y, yaw) in oriented.items()}
                    result.append(((quarter, template.name, round(ox), round(oy)), poses))
        return result
    if group.kind in {"wall", "free", "seat", "lamp"}:
        uid = template.name
        mode = ctx.plan["placement_mode_by_uid"].get(uid)
        guided = _guided_candidates(uid, ctx.assets[uid], ctx.area, parent.layout, ctx.assets, ctx.plan)
        candidates = _dedupe_candidates(
            _lamp_slots(ctx, parent, uid) + guided if group.kind == "lamp"
            else guided + _placement_candidates(ctx.assets[uid], ctx.area, 0, preferred_walls=_dynamic_preferred_walls(
                uid, mode, parent.layout, ctx.assets, ctx.plan), placement_mode=mode)
        )
        return [((_back_wall_name(rotation) if mode in _WALL_MODES else round(center[0]), round(center[1])),
                 {uid: (center[0], center[1], normalize_rotation(rotation))}) for center, rotation in candidates]
    # A media group's box also spans a wall TV, which has no footprint and may be wider than its support.
    footprint = {uid: pose for uid, pose in template.poses.items() if ctx.floor(uid) or group.kind == "media"}
    _, (x0, _, x1, y1) = oriented_living_group_bounds(footprint, dims, 0)
    walls = (ctx.plan["preferred_walls_by_uid"].get(next(iter(template.poses))) or [])
    candidates = [(center, rotation, (_back_wall_name(rotation),))
                  for center, rotation in _wall_candidates(ctx.area[0], ctx.area[1], x1 - x0, y1, 0, walls)]
    if group.kind == "media" and ctx.view and ctx.view["viewer"] in parent.layout:
        center, rotation = _facing_wall_candidate(ctx, parent, x1 - x0, y1)
        candidates += [(center, rotation, (_back_wall_name(rotation),)), *_divider_candidates(ctx, parent)]
    for center, rotation, key in candidates:
        quarter = round((rotation - math.pi / 2) / (math.pi / 2)) % 4
        wall = key[0]
        oriented, _ = oriented_living_group_bounds(template.poses, dims, quarter)
        cx, cy = _turn((x0 + x1) / 2, y1 / 2, quarter)
        poses = {uid: (x + center[0] - cx, y + center[1] - cy, normalize_rotation(yaw)) for uid, (x, y, yaw) in oriented.items()}
        result.append((key, poses))
        sofa = next(iter(template.poses))  # sitting templates start with the sofa
        if group.kind == "sitting" and any(asset_polygon([*poses[sofa][:2], 0.0], poses[sofa][2], *dims[sofa]).intersects(zone)
                                           for zone in ctx.seating_windows):
            # sofa_wall_gap may accept a sofa backed by a window clear of its seating zone, up to 0.45 m off the wall.
            shift = WINDOW_SEATING_CLEARANCE_M + 0.025 - SOFA_WALL_GAP_M
            dx, dy = shift * math.cos(rotation), shift * math.sin(rotation)
            shifted = {uid: (x + dx, y + dy, yaw) for uid, (x, y, yaw) in poses.items()}
            if not compute_sofa_wall_gap_violations({sofa: _placement(ctx, sofa, *shifted[sofa])}, ctx.instances,
                                                    ctx.room["room_vertices"], ctx.room["room_windows"]):
                result.append(((wall, "window"), shifted))
    return result


def _lamp_slots(ctx: _Room, parent: _Partial, uid: str) -> list[tuple[list[float], float]]:
    """Legacy _rehome_service_items floor-lamp targets: beside and behind each placed seat, mid reach range."""
    width, depth = ctx.dims(uid)
    item = FloorAsset(uid, ctx.assets[uid], [0.0, 0.0, 0.0], 0.0, width, depth, Polygon())
    return [candidate for seat in floor_assets(parent.layout, ctx.instances, keywords=SEATING_ROLE_KEYWORDS) if seat.uid in ctx.seats
            for candidate in _service_slot_candidates(item, seat, gap=sum(FLOOR_LAMP_REACH_RANGE_M) / 2, include_rear=True)]


def _facing_wall_candidate(ctx: _Room, parent: _Partial, width: float, depth: float) -> tuple[list[float], float]:
    """A media box centered on the viewer's axis, against the wall the viewer faces."""
    x, y, yaw = (*parent.layout[ctx.view["viewer"]]["position"][:2], parent.layout[ctx.view["viewer"]]["rotation"][2])
    hit = _ray_hit(ctx, x, y, yaw)
    if hit is None:
        return [x, y], normalize_rotation(yaw + math.pi)
    reach = math.dist((x, y), hit) - _SEED_MARGIN - depth / 2
    return [x + math.cos(yaw) * reach, y + math.sin(yaw) * reach], normalize_rotation(yaw + math.pi)


def _divider_candidates(ctx: _Room, parent: _Partial) -> list[tuple[list[float], float, tuple]]:
    """Studio media boxes standing free on the viewer's axis and facing it, with the screen at the middle and
    quarter points of the viewing range. Only for a TV on a support the studio checks let stand free."""
    view = ctx.view
    support = ctx.supports.get(view["tv"])
    if ctx.room["room_type"] != "studio" or not support or not is_freestanding_studio_media_support(ctx.assets[support]):
        return []
    placement = parent.layout[view["viewer"]]
    (ex, ey), yaw = _eye(ctx, (*placement["position"][:2], placement["rotation"][2]))
    middle = (view["low"] + view["high"]) / 2
    # The TV stands at the support center, so the box center sits half the TV depth behind the screen.
    reaches = [distance + ctx.dims(view["tv"])[1] / 2 for distance in ((view["low"] + middle) / 2, middle, (middle + view["high"]) / 2)]
    return [([ex + math.cos(yaw) * reach, ey + math.sin(yaw) * reach], normalize_rotation(yaw + math.pi),
             ("divider", round(reach, 1))) for reach in reaches]


def _ray_hit(ctx: _Room, x: float, y: float, yaw: float) -> tuple[float, float] | None:
    """The nearest room-boundary point straight ahead of (x, y)."""
    reach = sum(ctx.area) * 2
    hits = LineString([(x, y), (x + math.cos(yaw) * reach, y + math.sin(yaw) * reach)]).intersection(ctx.polygon.exterior)
    points = [point for geometry in getattr(hits, "geoms", [hits]) for point in getattr(geometry, "coords", [])]
    return min(points, key=lambda point: math.dist((x, y), point), default=None)


def _bands(ctx: _Room, template: _Template, poses: dict[str, Pose]) -> list[Polygon]:
    """Service bands, built like comfort's sofa band: the member's footprint frame, offset past one edge."""
    bands = []
    for uid, side in template.bands:
        x, y, yaw = poses[uid]
        width, depth = ctx.dims(uid)
        forward, lateral = {"front": ((depth + ACCESS_M) / 2, 0.0), "back": (-(depth + ACCESS_M) / 2, 0.0),
                            "left": (0.0, (width + ACCESS_M) / 2), "right": (0.0, -(width + ACCESS_M) / 2)}[side]
        center = [x + forward * math.cos(yaw) - lateral * math.sin(yaw), y + forward * math.sin(yaw) + lateral * math.cos(yaw), 0.0]
        bands.append(asset_polygon(center, yaw, *((width, ACCESS_M) if side in {"front", "back"} else (ACCESS_M, depth))))
    return bands


def _violation(ctx: _Room, parent: _Partial, prepared: tuple, template: _Template, poses: dict[str, Pose],
               strict: bool) -> tuple[float, list[tuple[str, Polygon]], list[Polygon]]:
    """How much a candidate breaks the seed's cheap checks, the reserved bands, and the living-dining gap.

    Sums the overlapping areas (plus 0.01 per break) and gap shortfalls; strict
    mode stops at the first break. Returns (violation, floor footprints, bands).
    """
    furniture, furniture_union, bands, bands_union = prepared
    floors = [(uid, asset_polygon([x, y, 0.0], yaw, *ctx.dims(uid))) for uid, (x, y, yaw) in poses.items() if ctx.floor(uid)]
    own = _bands(ctx, template, poses)
    total = 0.0
    for uid, polygon in floors:
        if not ctx.cover.covers(polygon):
            total += polygon.difference(ctx.polygon).area + 0.01
        if furniture is not None and furniture.intersects(polygon):
            total += polygon.intersection(furniture_union).area + 0.01
        if bands is not None and bands.intersects(polygon):
            total += polygon.intersection(bands_union).area + 0.01
        windows = ctx.windows if requires_window_clearance(ctx.assets[uid].get("category", ""), uid) else []
        total += sum(polygon.intersection(blocker).area + 0.01 for blocker in (*ctx.doors, *windows) if polygon.intersects(blocker))
        # compute_protected_path_violations allows a small overlap.
        total += sum(area for path in ctx.paths if (area := polygon.intersection(path).area) > PROTECTED_PATH_MIN_OVERLAP_SQM)
        if strict and total:
            return total, floors, own
    for uid in poses.keys() & ctx.supports.keys():
        screen = asset_polygon([poses[uid][0], poses[uid][1], 1.0], poses[uid][2], *ctx.dims(uid))
        total += sum(0.1 for window in ctx.windows if screen.intersects(window))
    for uid in poses.keys() & ctx.mounted.keys():  # a wall TV has no footprint but must clear doors and windows
        screen = asset_polygon([*poses[uid][:2], 0.0], poses[uid][2], *ctx.dims(uid))
        total += sum(screen.intersection(blocker).area + 0.01 for blocker in (*ctx.doors, *ctx.windows) if screen.intersects(blocker))
        if strict and total:
            return total, floors, own
    for band in own:
        if not ctx.cover.covers(band):
            total += band.difference(ctx.polygon).area + 0.01
        if furniture is not None and furniture.intersects(band):
            total += band.intersection(furniture_union).area + 0.01
        if strict and total:
            return total, floors, own
    living = _living(ctx, poses)
    pairs = ([(polygon, other) for uid, polygon in floors if uid in living for other in parent.dining]
             + [(polygon, other) for uid, polygon in floors if _is_dining_table(ctx, uid) for other in parent.living])
    total += sum(max(0.0, LIVING_DINING_CLEARANCE_M - polygon.distance(other)) for polygon, other in pairs)
    return total, floors, own


def _misses_table(ctx: _Room, parent: _Partial, uid: str, pose: Pose) -> bool:
    """A separately placed seat that the living-group cohesion check would flag as detached or not facing the table."""
    layout = {**parent.layout, uid: _placement(ctx, uid, *pose)}
    return any(row.get("seat") == uid and row["kind"] in CRITICAL_LIVING_GROUP_KINDS
               for row in compute_living_group_cohesion_violations(layout, ctx.instances, mixed_groups=ctx.room["room_type"] == "studio"))


def _living(ctx: _Room, poses: dict[str, Pose]) -> set[str]:
    """Members the living-dining gap measures: sofas, lounge seats, coffee tables."""
    return {uid for uid in poses if normalize_category(ctx.assets[uid].get("category")) in SITTING_CATEGORIES | {"coffee_table"}
            or ctx.plan["placement_mode_by_uid"].get(uid) == "around_anchor"}


def _is_dining_table(ctx: _Room, uid: str) -> bool:
    return normalize_category(ctx.assets[uid].get("category")) == "dining_table"


def _preference(ctx: _Room, parent: _Partial, group: _Group, template: _Template, key: tuple,
                poses: dict[str, Pose], floors: list[tuple[str, Polygon]]) -> float:
    """Soft costs that order checked candidates: template cost, the seed's preferred walls, door
    distance, a clear view from the viewer to the TV, the sofa's wall gap, TV viewing distance,
    facing and centerline, floor lamp reach, a central dining table (in a studio or living
    room, one by a wall), and corners for floor pieces. A candidate that a soft rule would flag costs 1 more."""
    cost = template.penalty
    anchor = next(iter(template.poses))
    if group.kind not in {"dining", "free"}:
        walls = ctx.plan["preferred_walls_by_uid"].get(anchor) or []
        cost += 0.1 * (walls.index(key[0]) if key[0] in walls else len(walls))
    cost += sum(max(0.0, 0.4 - polygon.distance(door)) for _, polygon in floors for door in ctx.doors)
    # Keep the view clear: a TV whose strip to its viewer crosses tall furniture, or tall furniture placed in it, costs more.
    if group.kind == "media":
        strip = _sightline(ctx, {**parent.layout, **{uid: _placement(ctx, uid, *pose) for uid, pose in poses.items()}})
        blocked = sum(strip.intersection(polygon).area for polygon in parent.tall) if strip else 0.0
    else:
        blocked = sum(parent.sightline.intersection(polygon).area for uid, polygon in floors
                      if _blocks_view(ctx, uid)) if parent.sightline else 0.0
    cost += (blocked > 1e-6) + blocked
    if group.kind == "sitting":  # sofa_wall_gap flags a sofa (the first footprint) inside a window's seating zone
        cost += any(floors[0][1].intersects(zone) for zone in ctx.seating_windows)
    view = ctx.view
    if view and view["viewer"] in poses:
        eye, yaw = _eye(ctx, poses[view["viewer"]])
        hit = _ray_hit(ctx, *eye, yaw)
        if hit is not None:  # the media group may still pull MEDIA_PULL_M closer, except a wall TV
            distance = math.dist(eye, hit) - view["inset"]
            cost += max(0.0, view["low"] - distance, distance - (0.0 if ctx.mounted else MEDIA_PULL_M) - view["high"])
    if group.kind == "media" and view and view["viewer"] in parent.layout:
        viewer = parent.layout[view["viewer"]]
        x, y, yaw = poses[anchor]
        to_media = _angle_to(viewer["position"], [x, y])
        cost += (_angle_delta_deg(viewer["rotation"][2], to_media) + _angle_delta_deg(yaw, to_media + math.pi)) / 45.0
        if view["tv"] in poses:
            eye, _ = _eye(ctx, (*viewer["position"][:2], viewer["rotation"][2]))
            tx, ty, tyaw = poses[view["tv"]]
            inset = ctx.dims(view["tv"])[1] / 2
            distance = math.dist(eye, (tx + inset * math.cos(tyaw), ty + inset * math.sin(tyaw)))
            miss = max(0.0, view["low"] - distance, distance - view["high"])
            cost += (miss > 0) + miss
            if ctx.room["room_type"] == "studio":  # the middle of the range, so the nearer of two in-range options wins
                cost += abs(distance - (view["low"] + view["high"]) / 2) / (view["high"] - view["low"])
        if not view["bed"] and ctx.room["room_type"] != "studio":  # compute_media_focal_alignment_violations
            axis, _ = _facing_axis_and_sign(viewer["rotation"][2])
            cost += abs((y, x)[axis == "y"] - viewer["position"][1 if axis == "x" else 0]) > MEDIA_CENTERLINE_MAX_OFFSET_M
    if ctx.rug and group.kind in {"sleeping", "sitting", "dining"}:
        # comfort's sitting_rug_crosses_groups: the sitting rug under over 10% of a bed or dining table
        placed = {uid: (*placement["position"][:2], placement["rotation"][2]) for uid, placement in parent.layout.items()} | poses
        others = [uid for uid in placed if normalize_category(ctx.assets[uid].get("category")) in {"bed", "dining_table"}]
        if ctx.rug in placed and (ctx.rug in poses or any(uid in poses for uid in others)):
            rug = asset_polygon([*placed[ctx.rug][:2], 0.0], placed[ctx.rug][2], *ctx.dims(ctx.rug))
            for uid in others:
                polygon = asset_polygon([*placed[uid][:2], 0.0], placed[uid][2], *ctx.dims(uid))
                cost += rug.intersection(polygon).area > 0.10 * polygon.area
    if group.kind == "lamp" and parent.seats:  # compute_floor_lamp_reach_violations
        polygon = floors[0][1]
        low, high = FLOOR_LAMP_REACH_RANGE_M
        gap = min(polygon.distance(seat) for seat in parent.seats)
        miss = max(0.0, low - gap, gap - high)
        far = max(0.0, ctx.polygon.exterior.distance(polygon) - FLOOR_LAMP_WALL_MAX_GAP_M)
        cost += (miss > 0) + miss + (far > 0) + far
    min_x, min_y, max_x, max_y = ctx.polygon.bounds
    if group.kind == "dining" and ctx.room["room_type"] in {"studio", "living_room"}:  # its own part of the room, by a wall
        cost += DINING_WALL_COST * min(ctx.polygon.exterior.distance(polygon) for _, polygon in floors)
    elif group.kind == "dining":
        cost += 0.1 * math.dist(poses[anchor][:2], ((min_x + max_x) / 2, (min_y + max_y) / 2))
    if group.kind == "free":
        cost += 0.2 * min(math.dist(poses[anchor][:2], corner) for corner in ((min_x, min_y), (max_x, min_y), (min_x, max_y), (max_x, max_y)))
    return cost


def _blocks_view(ctx: _Room, uid: str) -> bool:
    """Floor furniture taller than the screen's bottom, other than the viewer and a coffee table, hides the screen."""
    view = ctx.view
    if not view or view["screen_z"] is None or uid == view["viewer"]:
        return False
    category = normalize_category(ctx.assets[uid].get("category"))
    return (not matches_category_keywords(category, uid, COFFEE_TABLE_ROLE_KEYWORDS)
            and float(ctx.assets[uid].get("height") or 0) > view["screen_z"])


def _sightline(ctx: _Room, layout: Record) -> Polygon | None:
    """The floor strip as wide as the TV, from the viewer's front edge to the screen, once both are placed."""
    view = ctx.view
    if not view or view["screen_z"] is None or view["viewer"] not in layout or view["tv"] not in layout:
        return None
    ends = []
    for uid, reach in ((view["viewer"], ctx.dims(view["viewer"])[1] / 2), (view["tv"], ctx.dims(view["tv"])[1] / 2)):
        x, y = layout[uid]["position"][:2]
        yaw = layout[uid]["rotation"][2]
        ends.append((x + reach * math.cos(yaw), y + reach * math.sin(yaw)))
    if math.dist(*ends) < 1e-6:
        return None
    return LineString(ends).buffer(ctx.dims(view["tv"])[0] / 2, cap_style="flat")


def _eye(ctx: _Room, pose: Pose) -> tuple[tuple[float, float], float]:
    """Comfort's estimated viewing point of the viewer at pose, and its facing."""
    x, y, yaw = pose
    depth = ctx.dims(ctx.view["viewer"])[1]
    offset = -depth / 2 + 0.40 if ctx.view["bed"] else -0.25 * depth
    return (x + offset * math.cos(yaw), y + offset * math.sin(yaw)), yaw


def _placement(ctx: _Room, uid: str, x: float, y: float, yaw: float) -> Record:
    asset = ctx.assets[uid]
    entry: Record = {"category": asset.get("category", ""), "position": [x, y, 0.0], "rotation": [0.0, 0.0, yaw]}
    if uid in ctx.supports:
        entry["position"][2] = support_top_z(ctx.assets[ctx.supports[uid]], 0.0)
        entry["on_top_of"] = ctx.supports[uid]
    elif placement_mode_for_asset(asset) == "ceiling_mounted":
        entry["position"][2] = ceiling_mount_z(asset.get("height"))
    elif placement_mode_for_asset(asset) == "wall_mounted":
        entry["position"][2] = ctx.mount_z(uid)
    return entry


def _expand(ctx: _Room, parent: _Partial, group: _Group) -> list[_Partial]:
    """The SHORTLIST best children of a partial layout for one group, measured with analyze_layout.

    Candidates that pass every check are ordered by preference; when none
    passes, the least-violating ones are kept and the group's items are marked
    unplaceable.
    """
    if all(uid in parent.layout for template in group.templates for uid in template.poses):
        return [parent]
    furniture_union = unary_union(parent.furniture) if parent.furniture else None
    bands_union = unary_union(parent.bands) if parent.bands else None
    prepared = (prep(furniture_union) if furniture_union else None, furniture_union,
                prep(bands_union) if bands_union else None, bands_union)
    candidates = [(template, key, poses) for template in group.templates for key, poses in _placements(ctx, parent, group, template)]
    ctx.stats["candidates"] += len(candidates)
    checked = []
    for template, key, poses in candidates:
        violation, floors, bands = _violation(ctx, parent, prepared, template, poses, strict=True)
        if not violation and group.kind == "seat" and _misses_table(ctx, parent, template.name, poses[template.name]):
            violation = 1.0
        if not violation:
            checked.append((_preference(ctx, parent, group, template, key, poses, floors), template, key, poses, floors, bands))
    relaxed = not checked
    if relaxed and group.kind == "lamp":
        return [parent]  # the accessory pass places it, as the seed would
    if relaxed:
        for template, key, poses in candidates:
            violation, floors, bands = _violation(ctx, parent, prepared, template, poses, strict=False)
            checked.append((violation, template, key, poses, floors, bands))
    children = []
    shortlist = LAMP_SHORTLIST if group.kind == "lamp" else SHORTLIST
    min_x, min_y, max_x, max_y = ctx.polygon.bounds
    zone = group.kind in ZONE_KINDS
    for cost, template, key, poses, floors, bands in _pick(checked, shortlist, key=lambda row: row[0], diversity=lambda row: row[2]):
        layout = {**parent.layout, **{uid: _placement(ctx, uid, *pose) for uid, pose in poses.items()}}
        living = _living(ctx, poses)
        x, y, _ = poses[next(iter(template.poses))]
        plan = (x > (min_x + max_x) / 2, y > (min_y + max_y) / 2) if group.kind == "dining" else key[0]
        child = _Partial(
            layout=layout,
            furniture=[*parent.furniture, *(polygon for _, polygon in floors)],
            bands=[*parent.bands, *bands],
            living=[*parent.living, *(polygon for uid, polygon in floors if uid in living)],
            dining=[*parent.dining, *(polygon for uid, polygon in floors if _is_dining_table(ctx, uid))],
            seats=[*parent.seats, *(polygon for uid, polygon in floors if uid in ctx.seats)],
            tall=[*parent.tall, *(polygon for uid, polygon in floors if _blocks_view(ctx, uid))],
            sightline=_sightline(ctx, layout) if group.kind == "media" else parent.sightline,
            preference=parent.preference + cost,
            score=layout_issue_score(ctx.analyze(layout)),
            unplaceable=(*parent.unplaceable, *(sorted(poses) if relaxed else ())),
            key=(*parent.key, (group.kind, plan)) if zone else parent.key,
        )
        children.append(child)
    return children


def _pick(items: list, count: int, *, key, diversity=lambda item: item.key) -> list:
    """The best `count` items, taking the best of each diversity key first."""
    ordered = sorted(items, key=key)
    first, seen = [], set()
    for index, item in enumerate(ordered):
        if diversity(item) not in seen:
            first.append(index)
            seen.add(diversity(item))
    chosen = (first + [index for index in range(len(ordered)) if index not in set(first)])[:count]
    return [ordered[index] for index in sorted(chosen)]


# --- accessories -------------------------------------------------------------


def _accessorize(ctx: _Room, partial: _Partial) -> tuple[Record, list[str]]:
    """Place the accessories with the seed generator on top of the group layout.

    An item the seed skips takes the seed's best free floor spot ignoring
    openings (else the room's representative point) and is returned as skipped.
    """
    room = ctx.room
    pairs = _lamp_supports(ctx, partial.layout)
    instances = [{**asset, "paired_support_uid": pairs[uid]} if uid in pairs else asset for uid, asset in ctx.assets.items()]
    layout, report = generate_deterministic_layout_with_report(
        instances, ctx.area, room_vertices=room["room_vertices"], room_doors=room["room_doors"],
        room_windows=room["room_windows"], planner_guidance=ctx.guidance, protected_paths=room["protected_paths"],
        placed=partial.layout,
    )
    skipped = [outcome["uid"] for outcome in report["skipped"]]
    for uid in skipped:
        width, depth = ctx.dims(uid)
        occupied = [asset_polygon(placement["position"], placement["rotation"][2], *ctx.dims(other))
                    for other, placement in layout.items() if ctx.floor(other)]
        placed, _ = _search_placement(
            search_candidates=_grid_candidates(ctx.area[0], ctx.area[1], width, depth), width=width, depth=depth,
            z=0.0, rug_like=is_rug(uid, ctx.assets[uid]), room_cover=ctx.cover, room_area=ctx.area,
            occupied=occupied, blockers=[],
        )
        point = ctx.polygon.representative_point()
        center, yaw = (placed[0], placed[1]) if placed else ([point.x, point.y], 0.0)
        layout[uid] = _placement(ctx, uid, center[0], center[1], yaw)
    return layout, skipped


def _lamp_supports(ctx: _Room, layout: Record) -> dict[str, str]:
    """A free support for each table lamp the selection did not pair, one lamp per support.

    Desk lamps take desks. Table lamps take bedside pieces first (side tables
    first for a lamp of the sitting group), then other lamp tables, never desks.
    A lamp with no free support that fits keeps the seed's choice.
    """
    free = [uid for uid in layout if ctx.floor(uid) and is_table_lamp_support_asset(ctx.assets[uid].get("category", ""), uid)]
    pairs: dict[str, str] = {}
    for uid, asset in sorted(ctx.assets.items()):
        category = asset.get("category", "")
        if not is_table_lamp_asset(category, uid) or any(asset.get(key) in layout for key in ("paired_support_uid", "support_uid")):
            continue
        desk_lamp = matches_category_keywords(category, uid, ("desk_lamp",))  # normalize_category folds it into table_lamp
        bedside_first = asset.get("functional_group") != "sitting"
        ranked = sorted(
            (is_bedside_asset(ctx.assets[support]) != bedside_first, support) for support in free
            if (normalize_category(ctx.assets[support].get("category")) == "desk") == desk_lamp
            and _support_surface_fits(asset, *ctx.dims(support))
        )
        if ranked:
            pairs[uid] = ranked[0][1]
            free.remove(ranked[0][1])
    return pairs

