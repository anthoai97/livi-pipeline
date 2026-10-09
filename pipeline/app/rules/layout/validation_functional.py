"""Living, dining, media, lighting, and service checks."""

import math
from typing import Any
from shapely.geometry import LineString, Polygon
from app.rules.geometry.primitives import asset_polygon, extract_placement, is_rug
from app.rules.layout.bedroom import is_bedside_asset
from app.rules.planner.taxonomy import normalize_category
from app.rules.layout_rules import (
    SOFA_BACK_WALL_DISTANCE_RANGE_M,
    SOFA_COFFEE_TABLE_DISTANCE_RANGE_M,
)
from app.rules.planner.taxonomy import SEATING_ROLE_KEYWORDS, SOFA_ROLE_KEYWORDS
from app.rules.pipeline_shared import (
    is_floor_lamp_asset,
    is_table_lamp_asset,
    matches_category_keywords,
    nearest_wall,
    room_walls,
    support_top_z,
)

from app.rules.layout.constants import (
    COFFEE_TABLE_AXIS_TOLERANCE,
    COFFEE_TABLE_ROLE_KEYWORDS,
    DINING_CHAIR_FACING_MAX_DELTA_DEG,
    DINING_CHAIR_TABLE_AIM_TOLERANCE_M,
    DINING_CHAIR_TABLE_MAX_GAP_M,
    FLOOR_LAMP_REACH_RANGE_M,
    FLOOR_LAMP_WALL_MAX_GAP_M,
    LIVING_DINING_CLEARANCE_M,
    LIVING_SEAT_COFFEE_TABLE_MAX_GAP_M,
    LIVING_SEAT_RUG_MAX_GAP_M,
    LOUNGE_CHAIR_FACING_MAX_DELTA_DEG,
    LOUNGE_CHAIR_TABLE_AIM_TOLERANCE_M,
    MEDIA_CENTERLINE_MAX_OFFSET_M,
    MEDIA_ROLE_KEYWORDS,
    MEDIA_SEATING_MAX_DISTANCE_M,
    MEDIA_SUPPORT_CENTERLINE_MAX_OFFSET_M,
    MEDIA_SUPPORT_MAX_GAP_M,
    MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG,
    SIDE_TABLE_ROLE_KEYWORDS,
    SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG,
    SIDE_TABLE_SERVICE_REACH_MAX_M,
    TABLETOP_SURFACE_Z_TOLERANCE_M,
    TASK_CHAIR_DESK_MAX_GAP_M,
    TASK_CHAIR_FACING_MAX_DELTA_DEG,
    WINDOW_SEATING_CLEARANCE_M,
)
from app.rules.layout.metrics import (
    get_asset_by_uid,
)
from app.rules.layout.relations import (
    _angle_delta_deg,
    _back_wall_name,
    _back_wall_window_overlaps_item,
    _best_coffee_table_candidate,
    _facing_axis_and_sign,
    _facing_delta_to_item,
    _floor_asset_list,
    _front_wall_name,
    _functional_group_assets,
    _gather_floor_seating,
    _inset_support_polygon,
    _is_media_display_asset,
    _is_media_support_asset,
    _is_near_cardinal,
    _is_non_media_tabletop_item,
    _is_tabletop_support_asset,
    _largest_floor_asset,
    _long_axis_rotation,
    _nearest_floor_asset,
    _parallel_rotation_delta,
    _parent_uid,
    _support_edge_margins,
    _window_seating_clearance_poly,
    floor_assets,
)

def compute_sofa_coffee_table_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations = []
    min_gap, max_gap = SOFA_COFFEE_TABLE_DISTANCE_RANGE_M
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if not matches_category_keywords(
            asset.get("category", ""),
            uid,
            SOFA_ROLE_KEYWORDS,
        ):
            continue
        candidate = _best_coffee_table_candidate(uid, asset, placement, layout, assets)
        if not candidate:
            continue
        if candidate["gap"] < min_gap or candidate["gap"] > max_gap:
            axis = str(candidate["axis"])
            sign = float(candidate["sign"])
            table_delta_bounds = sorted(
                (
                    sign * (min_gap - float(candidate["gap"])),
                    sign * (max_gap - float(candidate["gap"])),
                )
            )
            sofa_delta_bounds = sorted(-delta for delta in table_delta_bounds)
            violations.append(
                {
                    "sofa": uid,
                    "table": candidate["table"],
                    "gap": candidate["gap"],
                    "min_gap": min_gap,
                    "max_gap": max_gap,
                    "forward": candidate["forward"],
                    "lateral": candidate["lateral"],
                    "axis": axis,
                    "sign": sign,
                    "table_center_translation_to_valid_gap_m": {
                        "axis": axis,
                        "min": table_delta_bounds[0],
                        "max": table_delta_bounds[1],
                    },
                    "sofa_center_translation_to_valid_gap_m": {
                        "axis": axis,
                        "min": sofa_delta_bounds[0],
                        "max": sofa_delta_bounds[1],
                    },
                }
            )
    return violations

def compute_sofa_wall_gap_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
    room_windows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    walls = room_walls(boundary)
    min_gap, max_gap = SOFA_BACK_WALL_DISTANCE_RANGE_M
    window_clearance_zones = [
        _window_seating_clearance_poly(window, boundary) for window in room_windows or []
    ]
    violations = []
    for item in floor_assets(layout, assets, keywords=SOFA_ROLE_KEYWORDS):
        if not _is_near_cardinal(item.rot_z):
            continue
        uid = item.uid
        back_wall = _back_wall_name(item.rot_z)
        poly_min_x, poly_min_y, poly_max_x, poly_max_y = item.poly.bounds
        back_x, back_y = {
            "left": (poly_min_x, item.pos[1]),
            "right": (poly_max_x, item.pos[1]),
            "bottom": (item.pos[0], poly_min_y),
            "top": (item.pos[0], poly_max_y),
        }[back_wall]
        wall = min(
            (wall for wall in walls if wall.name == back_wall),
            key=lambda wall: wall.distance(back_x, back_y),
        )
        if back_wall == "top":
            gap = wall.line_at(back_x) - poly_max_y
        elif back_wall == "bottom":
            gap = poly_min_y - wall.line_at(back_x)
        elif back_wall == "left":
            gap = poly_min_x - wall.line_at(back_y)
        else:
            gap = wall.line_at(back_y) - poly_max_x
        max_allowed_gap = max_gap
        if _back_wall_window_overlaps_item(item, back_wall, room_windows):
            max_allowed_gap = max(max_allowed_gap, WINDOW_SEATING_CLEARANCE_M + 0.05)
        window_blocked = any(
            item.poly.intersects(clearance_zone)
            for clearance_zone in window_clearance_zones
        )
        if window_blocked:
            violations.append(
                {
                    "uid": uid,
                    "kind": "window_clearance",
                    "clearance_m": WINDOW_SEATING_CLEARANCE_M,
                    "back_wall": back_wall,
                }
            )
        elif gap < min_gap or gap > max_allowed_gap:
            violations.append(
                {
                    "uid": uid,
                    "kind": "wall_gap",
                    "gap": float(gap),
                    "min_gap": min_gap,
                    "max_gap": max_allowed_gap,
                    "back_wall": back_wall,
                }
            )
    return violations

def compute_media_focal_alignment_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> list[dict[str, Any]]:
    anchors: list[tuple[str, list[float], float, float]] = []
    for item in floor_assets(layout, assets, keywords=SOFA_ROLE_KEYWORDS):
        if not _is_near_cardinal(item.rot_z):
            continue
        anchors.append((item.uid, item.pos, item.rot_z, item.width * item.depth))
    if not anchors:
        return []

    media_assets: list[tuple[str, list[float], float]] = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if not matches_category_keywords(
            asset.get("category", ""),
            uid,
            MEDIA_ROLE_KEYWORDS,
        ):
            continue
        pos, _, width, depth, _ = extract_placement(placement, asset)
        media_assets.append((uid, pos, width * depth))
    if not media_assets:
        return []

    sofa_uid, sofa_pos, sofa_rot_z, _ = max(anchors, key=lambda item: item[3])
    media_uid, media_pos, _ = max(media_assets, key=lambda item: item[2])
    expected_wall = _front_wall_name(sofa_rot_z)
    actual_wall = nearest_wall(media_pos, boundary).name
    axis, _ = _facing_axis_and_sign(sofa_rot_z)
    centerline_offset = abs(
        float(media_pos[1] - sofa_pos[1]) if axis == "x" else float(media_pos[0] - sofa_pos[0])
    )

    violations: list[dict[str, Any]] = []
    if actual_wall != expected_wall:
        violations.append(
            {
                "sofa": sofa_uid,
                "media": media_uid,
                "kind": "wall",
                "expected_wall": expected_wall,
                "actual_wall": actual_wall,
            }
        )
    if centerline_offset > MEDIA_CENTERLINE_MAX_OFFSET_M:
        violations.append(
            {
                "sofa": sofa_uid,
                "media": media_uid,
                "kind": "centerline",
                "offset": centerline_offset,
                "max_offset": MEDIA_CENTERLINE_MAX_OFFSET_M,
            }
        )
    return violations

def compute_media_group_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    displays: list[tuple[str, dict[str, Any], list[float], Polygon]] = []
    supports: list[tuple[str, dict[str, Any], list[float], Polygon]] = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        poly = asset_polygon(pos, rot_z, width, depth)
        if _is_media_display_asset(asset, uid):
            displays.append((uid, asset, pos, poly))
        elif _is_media_support_asset(asset, uid):
            supports.append((uid, asset, pos, poly))
    if not displays and not supports:
        return []

    violations: list[dict[str, Any]] = []
    # TV comfort is measured against its intended viewer and screen width by the
    # shared comfort analyzer. Keep the coarse cue only for console-only scenes.
    media_targets = [item for item in supports if item[1].get("viewing_target") != "bed"] if not displays else []
    sofa = _largest_floor_asset(
        _floor_asset_list(layout, assets, keywords=SOFA_ROLE_KEYWORDS)
    )
    if sofa and media_targets:
        media_uid, _, media_pos, _ = max(
            media_targets,
            key=lambda item: float(item[3].area),
        )
        seating_distance = math.hypot(
            float(sofa.pos[0] - media_pos[0]),
            float(sofa.pos[1] - media_pos[1]),
        )
        if seating_distance > MEDIA_SEATING_MAX_DISTANCE_M:
            violations.append(
                {
                    "sofa": sofa.uid,
                    "media": media_uid,
                    "kind": "seating_media_distance",
                    "distance": seating_distance,
                    "max_distance": MEDIA_SEATING_MAX_DISTANCE_M,
                }
            )

    if not displays or not supports:
        return violations

    for uid, _, pos, poly in displays:
        placement = layout.get(uid) or {}
        parent_uid = _parent_uid(placement)
        candidates = [
            support
            for support in supports
            if not parent_uid or support[0] == parent_uid
        ] or supports
        support_uid, _, support_pos, support_poly = min(
            candidates,
            key=lambda item: math.hypot(pos[0] - item[2][0], pos[1] - item[2][1]),
        )
        centerline_offset = math.hypot(
            float(pos[0] - support_pos[0]),
            float(pos[1] - support_pos[1]),
        )
        surface_gap = float(poly.distance(support_poly))
        _, display_rot_z, _, _, _ = extract_placement(
            layout.get(uid) or {},
            get_asset_by_uid(uid, assets),
        )
        _, support_rot_z, _, _, _ = extract_placement(
            layout.get(support_uid) or {},
            get_asset_by_uid(support_uid, assets),
        )
        rotation_delta_deg = _angle_delta_deg(display_rot_z, support_rot_z)
        if rotation_delta_deg > MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG:
            violations.append(
                {
                    "display": uid,
                    "support": support_uid,
                    "kind": "display_support_rotation",
                    "delta_deg": rotation_delta_deg,
                    "max_delta_deg": MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG,
                }
            )
        if (
            parent_uid
            and parent_uid == support_uid
            and centerline_offset <= MEDIA_SUPPORT_CENTERLINE_MAX_OFFSET_M
            and rotation_delta_deg <= MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG
        ):
            continue
        if centerline_offset > MEDIA_SUPPORT_CENTERLINE_MAX_OFFSET_M:
            violations.append(
                {
                    "display": uid,
                    "support": support_uid,
                    "kind": "display_support_centerline",
                    "offset": centerline_offset,
                    "max_offset": MEDIA_SUPPORT_CENTERLINE_MAX_OFFSET_M,
                }
            )
        if not parent_uid and surface_gap > MEDIA_SUPPORT_MAX_GAP_M:
            violations.append(
                {
                    "display": uid,
                    "support": support_uid,
                    "kind": "display_detached_from_support",
                    "gap": surface_gap,
                    "max_gap": MEDIA_SUPPORT_MAX_GAP_M,
                }
            )
    return violations

def compute_living_group_cohesion_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    *, mixed_groups: bool = False,
) -> list[dict[str, Any]]:
    groups = _functional_group_assets(layout, assets)
    if mixed_groups:
        from app.rules.layout.studio import accessory_group
        groups["rugs"] = [rug for rug in groups["rugs"] if accessory_group(layout, assets, rug.uid) == "sitting"]
    rug = _largest_floor_asset(groups["rugs"])
    table = _largest_floor_asset(groups["coffee_tables"])
    violations: list[dict[str, Any]] = []

    if table and rug:
        table_rug_gap = float(table.poly.distance(rug.poly))
        if table_rug_gap > LIVING_SEAT_RUG_MAX_GAP_M:
            violations.append(
                {
                    "kind": "coffee_table_off_rug",
                    "table": table.uid,
                    "rug": rug.uid,
                    "gap": table_rug_gap,
                    "max_gap": LIVING_SEAT_RUG_MAX_GAP_M,
                }
            )

    for chair in groups["lounge_chairs"]:
        if rug:
            rug_gap = float(chair.poly.distance(rug.poly))
            if rug_gap > LIVING_SEAT_RUG_MAX_GAP_M:
                violations.append(
                    {
                        "kind": "seat_off_rug",
                        "seat": chair.uid,
                        "rug": rug.uid,
                        "gap": rug_gap,
                        "max_gap": LIVING_SEAT_RUG_MAX_GAP_M,
                    }
                )
        nearest_table = _nearest_floor_asset(chair, groups["coffee_tables"])
        if nearest_table:
            table, table_gap = nearest_table
            if table_gap > LIVING_SEAT_COFFEE_TABLE_MAX_GAP_M:
                violations.append(
                    {
                        "kind": "seat_detached_from_table",
                        "seat": chair.uid,
                        "table": table.uid,
                        "gap": table_gap,
                        "max_gap": LIVING_SEAT_COFFEE_TABLE_MAX_GAP_M,
                    }
                )
            facing_delta = _facing_delta_to_item(chair, table)
            dx = table.pos[0] - chair.pos[0]
            dy = table.pos[1] - chair.pos[1]
            # Extend beyond the whole oriented tabletop, so its actual footprint
            # (not a center-angle cutoff) determines whether the chair faces it.
            ray_length = math.hypot(dx, dy) + math.hypot(table.width, table.depth)
            front_ray = LineString([
                chair.pos[:2],
                [
                    chair.pos[0] + math.cos(chair.rot_z) * ray_length,
                    chair.pos[1] + math.sin(chair.rot_z) * ray_length,
                ],
            ])
            aim_miss = float(front_ray.distance(table.poly))
            if aim_miss > LOUNGE_CHAIR_TABLE_AIM_TOLERANCE_M:
                violations.append(
                    {
                        "kind": (
                            "seat_faces_away"
                            if facing_delta > LOUNGE_CHAIR_FACING_MAX_DELTA_DEG
                            else "seat_misses_table"
                        ),
                        "seat": chair.uid,
                        "table": table.uid,
                        "facing_delta_deg": facing_delta,
                        "front_ray_miss_m": aim_miss,
                        "max_front_ray_miss_m": LOUNGE_CHAIR_TABLE_AIM_TOLERANCE_M,
                        "target_yaw_rad": math.atan2(dy, dx) % math.tau,
                    }
                )
    return violations

def compute_dining_group_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = _functional_group_assets(layout, assets)
    violations: list[dict[str, Any]] = []
    for chair in groups["dining_chairs"]:
        nearest = _nearest_floor_asset(chair, groups["dining_tables"])
        if nearest is None:
            continue
        table, gap = nearest
        facing_delta = _facing_delta_to_item(chair, table)
        ray_length = math.hypot(table.pos[0] - chair.pos[0], table.pos[1] - chair.pos[1]) + math.hypot(table.width, table.depth)
        front_ray = LineString([
            chair.pos[:2],
            (chair.pos[0] + math.cos(chair.rot_z) * ray_length,
             chair.pos[1] + math.sin(chair.rot_z) * ray_length),
        ])
        aim_miss = float(front_ray.distance(table.poly))
        if (gap <= DINING_CHAIR_TABLE_MAX_GAP_M
                and facing_delta <= DINING_CHAIR_FACING_MAX_DELTA_DEG
                and aim_miss <= DINING_CHAIR_TABLE_AIM_TOLERANCE_M):
            continue
        kind = (
            "chair_detached_and_faces_away"
            if gap > DINING_CHAIR_TABLE_MAX_GAP_M
            and facing_delta > DINING_CHAIR_FACING_MAX_DELTA_DEG
            else "chair_detached"
            if gap > DINING_CHAIR_TABLE_MAX_GAP_M
            else "chair_faces_away"
            if facing_delta > DINING_CHAIR_FACING_MAX_DELTA_DEG
            else "chair_misses_table"
        )
        violations.append(
            {
                "kind": kind,
                "chair": chair.uid,
                "table": table.uid,
                "gap": gap,
                "max_gap": DINING_CHAIR_TABLE_MAX_GAP_M,
                "facing_delta_deg": facing_delta,
                "max_delta_deg": DINING_CHAIR_FACING_MAX_DELTA_DEG,
                "front_ray_miss_m": aim_miss,
                "max_front_ray_miss_m": DINING_CHAIR_TABLE_AIM_TOLERANCE_M,
            }
        )
    return violations

def compute_living_dining_clearance_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = _functional_group_assets(layout, assets)
    dining_tables = groups["dining_tables"]
    if not dining_tables:
        return []

    living_assets = groups["sofas"] + groups["lounge_chairs"] + groups["coffee_tables"]

    violations: list[dict[str, Any]] = []
    for dining_table in dining_tables:
        for living_item in living_assets:
            gap = float(dining_table.poly.distance(living_item.poly))
            if gap >= LIVING_DINING_CLEARANCE_M:
                continue
            violations.append(
                {
                    "dining_table": dining_table.uid,
                    "living_item": living_item.uid,
                    "gap": gap,
                    "min_gap": LIVING_DINING_CLEARANCE_M,
                }
            )
    return violations

def compute_task_chair_orientation_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = _functional_group_assets(layout, assets)
    violations: list[dict[str, Any]] = []
    for chair in groups["task_chairs"]:
        nearest = _nearest_floor_asset(chair, groups["desks"])
        if nearest is None:
            continue
        desk, gap = nearest
        facing_delta = _facing_delta_to_item(chair, desk)
        if gap <= TASK_CHAIR_DESK_MAX_GAP_M and facing_delta <= TASK_CHAIR_FACING_MAX_DELTA_DEG:
            continue
        kind = (
            "chair_detached_and_faces_away"
            if gap > TASK_CHAIR_DESK_MAX_GAP_M
            and facing_delta > TASK_CHAIR_FACING_MAX_DELTA_DEG
            else "chair_detached"
            if gap > TASK_CHAIR_DESK_MAX_GAP_M
            else "chair_faces_away"
        )
        violations.append(
            {
                "kind": kind,
                "chair": chair.uid,
                "desk": desk.uid,
                "gap": gap,
                "max_gap": TASK_CHAIR_DESK_MAX_GAP_M,
                "facing_delta_deg": facing_delta,
                "max_delta_deg": TASK_CHAIR_FACING_MAX_DELTA_DEG,
            }
        )
    return violations

def compute_coffee_table_axis_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations = []
    for item in floor_assets(layout, assets, keywords=SOFA_ROLE_KEYWORDS):
        uid = item.uid
        sofa_rot_z, sofa_width, sofa_depth = item.rot_z, item.width, item.depth
        candidate = _best_coffee_table_candidate(
            uid,
            item.asset,
            layout[uid],
            layout,
            assets,
        )
        if not candidate:
            continue
        table_uid = candidate["table"]
        table_asset = get_asset_by_uid(table_uid, assets)
        table_placement = layout.get(table_uid) or {}
        _, table_rot_z, table_width, table_depth, _ = extract_placement(
            table_placement,
            table_asset,
        )
        # Only enforce this for clearly rectangular tables.
        if abs(float(table_width) - float(table_depth)) < 0.1:
            continue
        sofa_axis = _long_axis_rotation(sofa_rot_z, sofa_width, sofa_depth)
        table_axis = _long_axis_rotation(table_rot_z, table_width, table_depth)
        delta = _parallel_rotation_delta(table_axis, sofa_axis)
        if delta > COFFEE_TABLE_AXIS_TOLERANCE:
            violations.append(
                {
                    "sofa": uid,
                    "table": table_uid,
                    "delta": float(delta),
                    "max_delta": COFFEE_TABLE_AXIS_TOLERANCE,
                }
            )
    return violations

def compute_side_table_reach_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seating = _floor_asset_list(layout, assets, keywords=SEATING_ROLE_KEYWORDS)
    has_bed = any(normalize_category(a.get("category")) == "bed" for a in assets)

    violations = []
    if not seating:
        return violations

    for item in floor_assets(layout, assets, keywords=SIDE_TABLE_ROLE_KEYWORDS):
        if has_bed and is_bedside_asset(item.asset):
            continue  # The bedroom analyzer validates the selected bedside relationship.
        nearest_seat, nearest_gap = min(
            (
                (seat, float(item.poly.distance(seat.poly)))
                for seat in seating
            ),
            key=lambda entry: entry[1],
        )
        facing_delta = _facing_delta_to_item(nearest_seat, item)
        if (
            nearest_gap > SIDE_TABLE_SERVICE_REACH_MAX_M
            or facing_delta > SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG
        ):
            violations.append(
                {
                    "table": item.uid,
                    "seat": nearest_seat.uid,
                    "gap": nearest_gap,
                    "max_gap": SIDE_TABLE_SERVICE_REACH_MAX_M,
                    "facing_delta_deg": facing_delta,
                    "max_facing_delta_deg": SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG,
                }
            )
    return violations

def compute_floor_lamp_reach_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> list[dict[str, Any]]:
    seating = _gather_floor_seating(layout, assets)
    room_exterior = Polygon(boundary).exterior

    violations = []
    if not seating:
        return violations

    min_gap, max_gap = FLOOR_LAMP_REACH_RANGE_M
    for item in floor_assets(layout, assets):
        if not is_floor_lamp_asset(item.asset.get("category", ""), item.uid):
            continue
        nearest_uid, nearest_gap = min(
            (
                (seat_uid, float(item.poly.distance(seat_poly)))
                for seat_uid, seat_poly in seating
            ),
            key=lambda entry: entry[1],
        )
        wall_gap = float(room_exterior.distance(item.poly))
        if nearest_gap < min_gap or nearest_gap > max_gap:
            violations.append(
                {
                    "reason": "seat_reach",
                    "lamp": item.uid,
                    "seat": nearest_uid,
                    "gap": nearest_gap,
                    "min_gap": min_gap,
                    "max_gap": max_gap,
                    "wall_gap": wall_gap,
                    "max_wall_gap": FLOOR_LAMP_WALL_MAX_GAP_M,
                }
            )
        elif wall_gap > FLOOR_LAMP_WALL_MAX_GAP_M:
            violations.append(
                {
                    "reason": "wall_proximity",
                    "lamp": item.uid,
                    "seat": nearest_uid,
                    "gap": nearest_gap,
                    "min_gap": min_gap,
                    "max_gap": max_gap,
                    "wall_gap": wall_gap,
                    "max_wall_gap": FLOOR_LAMP_WALL_MAX_GAP_M,
                }
            )
    return violations

def compute_table_lamp_support_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations = []
    surface_overlap_pairs: set[tuple[str, str]] = set()
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        category = asset.get("category", "")
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        is_table_lamp = is_table_lamp_asset(category, uid)
        is_tabletop_display = _is_non_media_tabletop_item(asset, uid, placement)
        if not is_table_lamp and not (
            is_tabletop_display and (_parent_uid(placement) or pos[2] > 0.1)
        ):
            continue
        parent_uid = _parent_uid(placement)
        parent_asset = get_asset_by_uid(parent_uid, assets) if parent_uid else {}
        if (
            not parent_uid
            or parent_uid not in layout
            or not _is_tabletop_support_asset(parent_asset, parent_uid)
        ):
            violations.append(
                {
                    "lamp": uid,
                    "item": uid,
                    "kind": "table_lamp" if is_table_lamp else "tabletop_display",
                    "parent": parent_uid,
                    "reason": "invalid_parent",
                }
            )
            continue

        parent_placement = layout[parent_uid]
        p_pos, p_rot_z, p_width, p_depth, _ = extract_placement(
            parent_placement,
            parent_asset,
        )
        expected_z = support_top_z(parent_asset, p_pos[2])
        if abs(float(pos[2]) - expected_z) > TABLETOP_SURFACE_Z_TOLERANCE_M:
            violations.append(
                {
                    "lamp": uid,
                    "item": uid,
                    "kind": "table_lamp" if is_table_lamp else "tabletop_display",
                    "parent": parent_uid,
                    "reason": "support_height",
                    "z": round(float(pos[2]), 3),
                    "expected_z": round(float(expected_z), 3),
                }
            )
        parent_poly = asset_polygon(p_pos, p_rot_z, p_width, p_depth)
        support_poly = _inset_support_polygon(parent_poly, p_width, p_depth)
        item_poly = asset_polygon(pos, rot_z, width, depth)
        if not support_poly.covers(item_poly):
            margin_x, margin_y = _support_edge_margins(
                width,
                depth,
                p_width,
                p_depth,
            )
            violations.append(
                {
                    "lamp": uid,
                    "item": uid,
                    "kind": "table_lamp" if is_table_lamp else "tabletop_display",
                    "parent": parent_uid,
                    "reason": "support_edge",
                    "edge_margin_x": round(float(margin_x), 3),
                    "edge_margin_y": round(float(margin_y), 3),
                }
            )

        for other_uid, other_placement in layout.items():
            if other_uid in {uid, parent_uid}:
                continue
            other_asset = get_asset_by_uid(other_uid, assets)
            if is_rug(other_uid, other_asset):
                continue
            other_parent_uid = _parent_uid(other_placement)
            other_pos, other_rot_z, other_width, other_depth, _ = extract_placement(
                other_placement,
                other_asset,
            )
            same_support = other_parent_uid == parent_uid
            same_surface_height = (
                float(pos[2]) > 0.1
                and float(other_pos[2]) > 0.1
                and abs(float(other_pos[2]) - float(pos[2]))
                <= TABLETOP_SURFACE_Z_TOLERANCE_M
            )
            if not same_support and not same_surface_height:
                continue
            other_poly = asset_polygon(
                other_pos,
                other_rot_z,
                other_width,
                other_depth,
            )
            overlap_area = float(item_poly.intersection(other_poly).area)
            if overlap_area <= 1e-6:
                continue
            pair_key = tuple(sorted((uid, other_uid)))
            if pair_key in surface_overlap_pairs:
                continue
            surface_overlap_pairs.add(pair_key)
            violations.append(
                {
                    "lamp": uid,
                    "item": uid,
                    "kind": "table_lamp" if is_table_lamp else "tabletop_display",
                    "parent": parent_uid,
                    "other": other_uid,
                    "reason": "surface_overlap",
                    "overlap_area": overlap_area,
                }
            )
    return violations

def _service_corridor_polygon(
    *,
    sofa_placement: dict[str, Any],
    sofa_asset: dict[str, Any],
    table_placement: dict[str, Any],
    table_asset: dict[str, Any],
) -> Polygon | None:
    sofa_pos, sofa_rot_z, sofa_width, sofa_depth, _ = extract_placement(
        sofa_placement,
        sofa_asset,
    )
    if not _is_near_cardinal(sofa_rot_z):
        return None
    table_pos, table_rot_z, table_width, table_depth, _ = extract_placement(
        table_placement,
        table_asset,
    )
    sofa_poly = asset_polygon(sofa_pos, sofa_rot_z, sofa_width, sofa_depth)
    table_poly = asset_polygon(table_pos, table_rot_z, table_width, table_depth)
    sofa_min_x, sofa_min_y, sofa_max_x, sofa_max_y = sofa_poly.bounds
    table_min_x, table_min_y, table_max_x, table_max_y = table_poly.bounds
    axis, sign = _facing_axis_and_sign(sofa_rot_z)
    lateral_margin = 0.25

    if axis == "x":
        start = sofa_max_x if sign > 0 else table_max_x
        end = table_min_x if sign > 0 else sofa_min_x
        lateral_min = max(sofa_min_y, table_min_y) - lateral_margin
        lateral_max = min(sofa_max_y, table_max_y) + lateral_margin
        if sign < 0:
            start = table_max_x
            end = sofa_min_x
        if end <= start or lateral_max <= lateral_min:
            return None
        return Polygon(
            [
                (start, lateral_min),
                (end, lateral_min),
                (end, lateral_max),
                (start, lateral_max),
            ]
        )

    start = sofa_max_y if sign > 0 else table_max_y
    end = table_min_y if sign > 0 else sofa_min_y
    lateral_min = max(sofa_min_x, table_min_x) - lateral_margin
    lateral_max = min(sofa_max_x, table_max_x) + lateral_margin
    if sign < 0:
        start = table_max_y
        end = sofa_min_y
    if end <= start or lateral_max <= lateral_min:
        return None
    return Polygon(
        [
            (lateral_min, start),
            (lateral_max, start),
            (lateral_max, end),
            (lateral_min, end),
        ]
    )

def compute_service_corridor_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for sofa_uid, sofa_placement in layout.items():
        sofa_asset = get_asset_by_uid(sofa_uid, assets)
        if not matches_category_keywords(
            sofa_asset.get("category", ""),
            sofa_uid,
            SOFA_ROLE_KEYWORDS,
        ):
            continue
        candidate = _best_coffee_table_candidate(
            sofa_uid,
            sofa_asset,
            sofa_placement,
            layout,
            assets,
        )
        if not candidate:
            continue
        table_uid = candidate["table"]
        table_placement = layout.get(table_uid) or {}
        table_asset = get_asset_by_uid(table_uid, assets)
        corridor = _service_corridor_polygon(
            sofa_placement=sofa_placement,
            sofa_asset=sofa_asset,
            table_placement=table_placement,
            table_asset=table_asset,
        )
        if corridor is None:
            continue
        for item in floor_assets(layout, assets, skip_rugs=True):
            if item.uid in {sofa_uid, table_uid}:
                continue
            if matches_category_keywords(
                item.asset.get("category", ""),
                item.uid,
                SOFA_ROLE_KEYWORDS + COFFEE_TABLE_ROLE_KEYWORDS,
            ):
                continue
            if not item.poly.intersects(corridor):
                continue
            key = (item.uid, sofa_uid, table_uid)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "uid": item.uid,
                    "sofa": sofa_uid,
                    "table": table_uid,
                    "kind": "sofa_table_service_corridor",
                }
            )
    return violations
