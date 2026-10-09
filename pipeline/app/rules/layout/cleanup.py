"""Deterministic layout repair, ported from core/layout/cleanup.py and the
clear_protected_path and satisfy_sofa_table_gap solver operations."""

import math
from collections.abc import Callable
from typing import Any
from shapely.geometry import Polygon
from app.rules.geometry.primitives import (
    asset_polygon,
    extract_placement,
    frange,
    is_rug,
    projected_half_extents,
)
from app.rules.planner.taxonomy import SOFA_ROLE_KEYWORDS
from app.rules.pipeline_shared import (
    clamp,
    is_wall_aligned_asset,
    matches_category_keywords,
    nearest_wall,
    requires_window_clearance,
    room_bounds,
)
from app.rules.protected_paths import protected_path_polygon
from app.rules.layout.studio import is_freestanding_studio_media_support

from app.rules.layout.analysis import (
    analyze_layout,
)
from app.rules.layout.constants import (
    DINING_CHAIR_GAP_M,
    ISSUE_KEYS_BY_TIER,
    LIVING_DINING_CLEARANCE_M,
    MEDIA_ROLE_KEYWORDS,
)
from app.rules.layout.metrics import (
    FloorAsset,
    get_asset_by_uid,
    issue_counts,
    layout_change_summary,
    layout_issue_score,
)
from app.rules.layout.normalization import (
    _correct_z,
    move_asset_with_supports,
    normalize_layout,
)
from app.rules.layout.relations import (
    _angle_delta_deg,
    _build_blocker_specs,
    _functional_group_assets,
    _is_wall_mounted_layout_asset,
    _nearest_floor_asset,
    floor_assets,
)

def _overlap_move_uid(uid_a: str, uid_b: str, assets: list[dict[str, Any]]) -> str:
    asset_a = get_asset_by_uid(uid_a, assets)
    asset_b = get_asset_by_uid(uid_b, assets)
    a_seating = matches_category_keywords(asset_a.get("category", ""), uid_a, SOFA_ROLE_KEYWORDS)
    b_seating = matches_category_keywords(asset_b.get("category", ""), uid_b, SOFA_ROLE_KEYWORDS)
    if a_seating and not b_seating:
        return uid_b
    if b_seating and not a_seating:
        return uid_a
    area_a = float(asset_a.get("width", 0.5) or 0.5) * float(asset_a.get("depth", 0.5) or 0.5)
    area_b = float(asset_b.get("width", 0.5) or 0.5) * float(asset_b.get("depth", 0.5) or 0.5)
    return uid_a if area_a <= area_b else uid_b

def _p0_move_uids(issues: dict[str, Any], assets: list[dict[str, Any]]) -> set[str]:
    uids = set(issues.get("boundary_violations", []))
    uids.update(issues.get("obstacle_violations", []))
    for uid_a, uid_b in issues.get("overlaps", []):
        uids.add(_overlap_move_uid(uid_a, uid_b, assets))
    for uid, _ in issues.get("door_violations", []):
        uids.add(uid)
    return uids

def _protected_path_move_uids(issues: dict[str, Any]) -> set[str]:
    return {
        str(item.get("uid"))
        for item in issues.get("protected_path_violations", [])
        if item.get("uid")
    }

def _position_candidates(
    pos: list[float],
    rot_z: float,
    width: float,
    depth: float,
    boundary: list[list[float]],
) -> list[tuple[float, float]]:
    min_x, min_y, max_x, max_y = room_bounds(boundary=boundary)
    half_x, half_y = projected_half_extents(width, depth, rot_z)
    step = max(min(width, depth) * 0.75, 0.35)
    xs = _candidate_axis_values(min_x + half_x, max_x - half_x, step)
    ys = _candidate_axis_values(min_y + half_y, max_y - half_y, step)
    current_x = clamp(float(pos[0]), min_x + half_x, max_x - half_x)
    current_y = clamp(float(pos[1]), min_y + half_y, max_y - half_y)
    points = [(current_x, current_y)]
    points.extend(
        sorted(
            (
                (x, y)
                for x in xs
                for y in ys
                if abs(x - current_x) > 1e-6 or abs(y - current_y) > 1e-6
            ),
            key=lambda point: (
                abs(point[0] - current_x) + abs(point[1] - current_y),
                abs(point[1] - current_y),
                abs(point[0] - current_x),
            ),
        )
    )
    return points

def _candidate_axis_values(start: float, stop: float, step: float) -> list[float]:
    values = frange(start, stop, step)
    for endpoint in (round(start, 4), round(stop, 4)):
        if endpoint not in values:
            values.append(endpoint)
    return sorted(values)

def _wall_aligned_position_candidates(
    pos: list[float],
    rot_z: float,
    width: float,
    depth: float,
    boundary: list[list[float]],
) -> list[tuple[float, float]]:
    half_x, half_y = projected_half_extents(width, depth, rot_z)
    wall = nearest_wall(pos, boundary)
    lo, hi = wall.span()
    step = 0.15

    if not wall.vertical:
        values = _candidate_axis_values(lo + half_x, hi - half_x, step)
        return sorted(
            [
                (
                    x,
                    wall.line_at(x) + half_y + 0.05
                    if wall.name == "bottom"
                    else wall.line_at(x) - half_y - 0.05,
                )
                for x in values
            ],
            key=lambda point: abs(point[0] - float(pos[0])),
        )

    values = _candidate_axis_values(lo + half_y, hi - half_y, step)
    return sorted(
        [
            (
                wall.line_at(y) + half_x + 0.05
                if wall.name == "left"
                else wall.line_at(y) - half_x - 0.05,
                y,
            )
            for y in values
        ],
        key=lambda point: abs(point[1] - float(pos[1])),
    )

def _collect_floor_state(
    result: dict[str, Any],
    assets: list[dict[str, Any]],
    move_uids: set[str],
) -> tuple[dict[str, tuple[list[float], float, float, float]], list[Polygon]]:
    """Gather P0-movable placements plus floor footprints that stay put."""
    occupied: list[Polygon] = []
    floor_positions: dict[str, tuple[list[float], float, float, float]] = {}
    for uid, placement in result.items():
        asset = get_asset_by_uid(uid, assets)
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        rug = is_rug(uid, asset)
        elevated_blocker = (
            uid in move_uids
            and (
                _is_wall_mounted_layout_asset(asset, uid)
                or matches_category_keywords(
                    asset.get("category", ""),
                    uid,
                    MEDIA_ROLE_KEYWORDS,
                )
            )
        )
        if (rug and uid not in move_uids) or (pos[2] > 0.1 and not elevated_blocker):
            continue
        floor_positions[uid] = (pos, rot_z, width, depth)
        if not rug and uid not in move_uids and pos[2] <= 0.1:
            occupied.append(asset_polygon(pos, rot_z, width, depth))
    return floor_positions, occupied

def _plan_p0_moves(
    *,
    move_uids: set[str],
    floor_positions: dict[str, tuple[list[float], float, float, float]],
    occupied: list[Polygon],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
    room_poly: Polygon,
    all_blockers: list[Polygon],
    door_only_blockers: list[Polygon],
) -> tuple[dict[str, tuple[float, float]], list[dict[str, Any]]]:
    """Pick a collision-free destination for each violating asset (largest first)."""
    planned_moves: dict[str, tuple[float, float]] = {}
    planned_move_records: list[dict[str, Any]] = []
    for uid in sorted(
        move_uids,
        key=lambda value: (
            -float(get_asset_by_uid(value, assets).get("width", 0.5) or 0.5)
            * float(get_asset_by_uid(value, assets).get("depth", 0.5) or 0.5),
            value,
        ),
    ):
        if uid not in floor_positions:
            continue
        pos, rot_z, width, depth = floor_positions[uid]
        asset = get_asset_by_uid(uid, assets)
        rug = is_rug(uid, asset)
        blockers = (
            all_blockers
            if requires_window_clearance(
                asset.get("category", ""),
                uid,
                z=pos[2],
            )
            else door_only_blockers
        )
        chosen = None
        candidate_points = []
        if (
            (is_wall_aligned_asset(asset.get("category", ""), uid)
             and not is_freestanding_studio_media_support(asset))
            or _is_wall_mounted_layout_asset(asset, uid)
        ):
            candidate_points.extend(
                _wall_aligned_position_candidates(
                    pos,
                    rot_z,
                    width,
                    depth,
                    boundary,
                )
            )
        candidate_points.extend(
            _position_candidates(
                pos,
                rot_z,
                width,
                depth,
                boundary,
            )
        )
        seen_points: set[tuple[float, float]] = set()
        for candidate_x, candidate_y in candidate_points:
            point_key = (round(candidate_x, 4), round(candidate_y, 4))
            if point_key in seen_points:
                continue
            seen_points.add(point_key)
            candidate_poly = asset_polygon(
                [candidate_x, candidate_y, pos[2]],
                rot_z,
                width,
                depth,
            )
            if not room_poly.covers(candidate_poly):
                continue
            if not rug and any(
                candidate_poly.intersects(blocker) for blocker in blockers
            ):
                continue
            if not rug and any(
                candidate_poly.intersects(other) for other in occupied
            ):
                continue
            chosen = (candidate_x, candidate_y, candidate_poly)
            break
        if chosen is None:
            continue
        planned_moves[uid] = (chosen[0], chosen[1])
        planned_move_records.append(
            {
                "uid": uid,
                "from": [round(pos[0], 4), round(pos[1], 4)],
                "to": [round(chosen[0], 4), round(chosen[1], 4)],
            }
        )
        if not rug:
            occupied.append(chosen[2])
    return planned_moves, planned_move_records

def run_deterministic_p0_cleanup(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis_tiers = ("P0", "P1", "P2")
    hard_issue_keys = ISSUE_KEYS_BY_TIER["P0"] + ISSUE_KEYS_BY_TIER["P1"]
    baseline_issues = analyze_layout(
        layout,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=analysis_tiers,
    )
    baseline_score = layout_issue_score(baseline_issues)
    result = normalize_layout(layout, assets, boundary, room_windows=room_windows)
    cleanup_tiers = ("P0", "P1") if protected_paths else ("P0",)
    issues = analyze_layout(
        result,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=analysis_tiers,
    )
    move_uids = _p0_move_uids(issues, assets)
    move_uids.update(_protected_path_move_uids(issues))
    if not move_uids:
        normalized_score = layout_issue_score(issues)
        normalized_hard_regressions = [
            key
            for key in hard_issue_keys
            if len(issues.get(key) or []) > len(baseline_issues.get(key) or [])
        ]
        normalization_safe = (
            normalized_score[0] <= baseline_score[0]
            and normalized_score[1] <= baseline_score[1]
            and not normalized_hard_regressions
        )
        final_result = result if normalization_safe else layout
        final_issues = issues if normalization_safe else baseline_issues
        return final_result, {
            "status": "normalized" if normalization_safe else "no_changes",
            "move_uids": [],
            "planned_moves": [],
            "moved_count": 0,
            "issue_counts_before": issue_counts(baseline_issues, cleanup_tiers),
            "issue_counts_after": issue_counts(final_issues, cleanup_tiers),
            "baseline_score": list(baseline_score),
            "candidate_scores": {"normalized": list(normalized_score)},
            "candidate_hard_regressions": {
                "normalized": normalized_hard_regressions
            },
            "normalization_selected": (
                "normalized" if normalization_safe else "pre_cleanup"
            ),
            "rejection_reasons": (
                {}
                if normalization_safe or normalized_score == baseline_score
                else {
                    "normalized": (
                        (
                            ["increased_hard_issues"]
                            if normalized_score[0] > baseline_score[0]
                            else []
                        )
                        + (
                            ["increased_critical_functional_issues"]
                            if normalized_score[1] > baseline_score[1]
                            else []
                        )
                        + (
                            ["increased_hard_issue_types"]
                            if normalized_hard_regressions
                            else []
                        )
                    )
                }
            ),
            "change_summary": layout_change_summary(layout, final_result),
        }

    room_poly = Polygon([(point[0], point[1]) for point in boundary]).buffer(0.05)
    blocker_specs = _build_blocker_specs(
        room_doors,
        room_windows,
        boundary=boundary,
        room_area=room_area,
    )
    protected_blockers = [
        protected_path_polygon(path)
        for path in protected_paths or []
    ]
    floor_positions, occupied = _collect_floor_state(result, assets, move_uids)
    planned_moves, planned_move_records = _plan_p0_moves(
        move_uids=move_uids,
        floor_positions=floor_positions,
        occupied=occupied,
        assets=assets,
        boundary=boundary,
        room_poly=room_poly,
        all_blockers=[spec["poly"] for spec in blocker_specs] + protected_blockers,
        door_only_blockers=[
            spec["poly"] for spec in blocker_specs if spec["kind"] == "door"
        ] + protected_blockers,
    )

    for uid, (target_x, target_y) in planned_moves.items():
        result = move_asset_with_supports(result, uid, target_x, target_y, assets)

    normalized_result = normalize_layout(
        result,
        assets,
        boundary,
        room_windows=room_windows,
        rehome_service_items=False,
    )
    raw_result = _correct_z(result, assets)
    raw_issues = analyze_layout(
        raw_result,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=analysis_tiers,
    )
    normalized_issues = analyze_layout(
        normalized_result,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=analysis_tiers,
    )
    candidates = {
        "raw": (raw_result, raw_issues, layout_issue_score(raw_issues)),
        "normalized": (
            normalized_result,
            normalized_issues,
            layout_issue_score(normalized_issues),
        ),
    }
    candidate_hard_regressions = {
        name: [
            key
            for key in hard_issue_keys
            if len(candidate_issues.get(key) or [])
            > len(baseline_issues.get(key) or [])
        ]
        for name, (_, candidate_issues, _) in candidates.items()
    }
    safe_candidates = {
        name: candidate
        for name, candidate in candidates.items()
        if candidate[2][0] < baseline_score[0]
        and candidate[2][1] <= baseline_score[1]
        and not candidate_hard_regressions[name]
    }
    rejection_reasons = {}
    for name, (_, _, score) in candidates.items():
        reasons = []
        if score[0] >= baseline_score[0]:
            reasons.append("did_not_reduce_hard_issues")
        if score[1] > baseline_score[1]:
            reasons.append("increased_critical_functional_issues")
        if candidate_hard_regressions[name]:
            reasons.append("increased_hard_issue_types")
        if reasons:
            rejection_reasons[name] = reasons

    if safe_candidates:
        selected_name, (final_result, final_issues, _) = min(
            safe_candidates.items(),
            key=lambda item: item[1][2],
        )
        status = "moved"
    else:
        selected_name = "pre_cleanup"
        final_result = layout
        final_issues = baseline_issues
        status = (
            "rejected_critical_functional_regression"
            if any(
                "increased_critical_functional_issues" in reasons
                for reasons in rejection_reasons.values()
            )
            else (
                "rejected_hard_issue_regression"
                if any(candidate_hard_regressions.values())
                else (
                    "rejected_no_hard_improvement"
                    if planned_moves
                    else "no_valid_destination"
                )
            )
        )

    return final_result, {
        "status": status,
        "move_uids": sorted(move_uids),
        "planned_moves": planned_move_records,
        "moved_count": len(planned_move_records) if safe_candidates else 0,
        "issue_counts_before": issue_counts(baseline_issues, cleanup_tiers),
        "issue_counts_after": issue_counts(final_issues, cleanup_tiers),
        "baseline_score": list(baseline_score),
        "candidate_scores": {
            name: list(candidate[2]) for name, candidate in candidates.items()
        },
        "candidate_hard_regressions": candidate_hard_regressions,
        "normalization_selected": selected_name,
        "rejection_reasons": rejection_reasons,
        "change_summary": layout_change_summary(layout, final_result),
    }

def _rotate_dining_chairs_toward_tables(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    chair_uids: set[str] | None = None,
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    groups = _functional_group_assets(result, assets)
    tables = groups["dining_tables"]
    if not tables:
        return result
    for chair in groups["dining_chairs"]:
        if chair_uids is not None and chair.uid not in chair_uids:
            continue
        nearest = _nearest_floor_asset(chair, tables)
        if nearest is None or chair.uid not in result:
            continue
        table, _ = nearest
        target_yaw = math.atan2(
            float(table.pos[1]) - float(chair.pos[1]),
            float(table.pos[0]) - float(chair.pos[0]),
        ) % (2 * math.pi)
        placement = result[chair.uid]
        rotation = list(placement.get("rotation") or [0.0, 0.0, chair.rot_z])
        while len(rotation) < 3:
            rotation.append(0.0)
        rotation[2] = target_yaw
        result[chair.uid] = {**placement, "rotation": rotation[:3]}
    return result

def _rotate_task_chairs_toward_desks(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    chair_uids: set[str] | None = None,
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    groups = _functional_group_assets(result, assets)
    desks = groups["desks"]
    if not desks:
        return result
    for chair in groups["task_chairs"]:
        if chair_uids is not None and chair.uid not in chair_uids:
            continue
        nearest = _nearest_floor_asset(chair, desks)
        if nearest is None or chair.uid not in result:
            continue
        desk, _ = nearest
        target_yaw = math.atan2(
            float(desk.pos[1]) - float(chair.pos[1]),
            float(desk.pos[0]) - float(chair.pos[0]),
        ) % (2 * math.pi)
        placement = result[chair.uid]
        rotation = list(placement.get("rotation") or [0.0, 0.0, chair.rot_z])
        while len(rotation) < 3:
            rotation.append(0.0)
        rotation[2] = target_yaw
        result[chair.uid] = {**placement, "rotation": rotation[:3]}
    return result

def _arrange_dining_chairs_around_tables(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    result = _rotate_dining_chairs_toward_tables(layout, assets)
    groups = _functional_group_assets(result, assets)
    tables = groups["dining_tables"]
    if not tables:
        return result

    living_items = groups["sofas"] + groups["lounge_chairs"] + groups["coffee_tables"]
    living_center = None
    if living_items:
        living_center = [
            sum(float(item.pos[0]) for item in living_items) / len(living_items),
            sum(float(item.pos[1]) for item in living_items) / len(living_items),
        ]

    for table in tables:
        chairs = [
            chair
            for chair in groups["dining_chairs"]
            if (nearest := _nearest_floor_asset(chair, tables)) is not None
            and nearest[0].uid == table.uid
            and chair.uid in result
        ]
        if not chairs:
            continue

        object_rotation = (table.rot_z + math.pi / 2.0) % (2 * math.pi)
        ux = (math.cos(object_rotation), math.sin(object_rotation))
        uy = (-math.sin(object_rotation), math.cos(object_rotation))
        if living_center:
            to_living = (
                float(living_center[0]) - float(table.pos[0]),
                float(living_center[1]) - float(table.pos[1]),
            )
            width_dot = to_living[0] * ux[0] + to_living[1] * ux[1]
            depth_dot = to_living[0] * uy[0] + to_living[1] * uy[1]
            prefer_width_side = abs(width_dot) > abs(depth_dot)
            width_sign = -1.0 if width_dot > 0 else 1.0
            depth_sign = -1.0 if depth_dot > 0 else 1.0
        else:
            prefer_width_side = False
            width_sign = 1.0
            depth_sign = 1.0

        side_order = (
            [("width", width_sign), ("depth", depth_sign), ("depth", -depth_sign), ("width", -width_sign)]
            if prefer_width_side
            else [("depth", depth_sign), ("width", width_sign), ("width", -width_sign), ("depth", -depth_sign)]
        )
        slots: list[tuple[float, float, float]] = []
        for axis_name, sign in side_order:
            if len(slots) >= len(chairs):
                break
            axis = ux if axis_name == "width" else uy
            lateral_axis = uy if axis_name == "width" else ux
            table_half = table.width / 2.0 if axis_name == "width" else table.depth / 2.0
            lateral_span = table.depth if axis_name == "width" else table.width
            side_chairs = min(2, len(chairs) - len(slots))
            offsets = [0.0] if side_chairs == 1 else [-lateral_span / 4.0, lateral_span / 4.0]
            for offset in offsets:
                chair = chairs[len(slots)]
                chair_half = max(chair.width, chair.depth) / 2.0
                distance = table_half + chair_half + DINING_CHAIR_GAP_M
                x = (
                    float(table.pos[0])
                    + axis[0] * sign * distance
                    + lateral_axis[0] * offset
                )
                y = (
                    float(table.pos[1])
                    + axis[1] * sign * distance
                    + lateral_axis[1] * offset
                )
                yaw = math.atan2(-axis[1] * sign, -axis[0] * sign) % (2 * math.pi)
                slots.append((x, y, yaw))
                if len(slots) >= len(chairs):
                    break

        for chair, (x, y, yaw) in zip(chairs, slots):
            placement = result[chair.uid]
            pos = list(placement.get("position") or [0.0, 0.0, 0.0])
            while len(pos) < 3:
                pos.append(0.0)
            rotation = list(placement.get("rotation") or [0.0, 0.0, chair.rot_z])
            while len(rotation) < 3:
                rotation.append(0.0)
            pos[0], pos[1] = x, y
            rotation[2] = yaw
            result[chair.uid] = {
                **placement,
                "position": pos[:3],
                "rotation": rotation[:3],
            }
    return result

def _translate_layout_group(
    layout: dict[str, Any],
    uids: set[str],
    dx: float,
    dy: float,
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    for uid in uids:
        placement = result.get(uid)
        if not placement:
            continue
        pos = list(placement.get("position") or [0.0, 0.0, 0.0])
        while len(pos) < 3:
            pos.append(0.0)
        pos[0] = float(pos[0]) + dx
        pos[1] = float(pos[1]) + dy
        result[uid] = {**placement, "position": pos[:3]}
    return result

def _dining_cluster_uids_for_table(
    table: FloorAsset,
    groups: dict[str, list[FloorAsset]],
) -> set[str]:
    uids = {table.uid}
    for chair in groups["dining_chairs"]:
        nearest = _nearest_floor_asset(chair, groups["dining_tables"])
        if nearest is not None and nearest[0].uid == table.uid:
            uids.add(chair.uid)
    return uids

def run_deterministic_living_dining_cleanup(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = normalize_layout(
        layout,
        assets,
        boundary,
        room_windows=room_windows,
        rehome_service_items=False,
    )
    baseline_result = {uid: dict(placement) for uid, placement in result.items()}
    initial_issues = analyze_layout(
        result,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=("P0", "P1", "P2"),
    )
    needs_living_dining = bool(initial_issues.get("living_dining_clearance_violations"))
    needs_dining_group = bool(initial_issues.get("dining_group_violations"))
    needs_task_chairs = bool(initial_issues.get("task_chair_orientation_violations"))
    if not (needs_living_dining or needs_dining_group or needs_task_chairs):
        return result, {
            "status": "no_changes",
            "issue_counts_before": issue_counts(initial_issues, ("P0", "P1", "P2")),
        }

    task_chair_uids = {
        str(item.get("chair"))
        for item in initial_issues.get("task_chair_orientation_violations", [])
        if item.get("chair")
    }
    dining_chair_uids = {
        str(item.get("chair"))
        for item in initial_issues.get("dining_group_violations", [])
        if item.get("chair")
    }
    if needs_task_chairs:
        result = _rotate_task_chairs_toward_desks(result, assets, task_chair_uids)
    if needs_living_dining:
        result = _arrange_dining_chairs_around_tables(result, assets)
    elif needs_dining_group:
        result = _rotate_dining_chairs_toward_tables(result, assets, dining_chair_uids)
    result, _ = run_deterministic_p0_cleanup(
        result,
        assets,
        room_area,
        boundary,
        room_doors=room_doors,
        room_windows=room_windows,
        protected_paths=protected_paths,
    )
    if needs_living_dining:
        result = _rotate_dining_chairs_toward_tables(result, assets)
    elif needs_dining_group:
        result = _rotate_dining_chairs_toward_tables(result, assets, dining_chair_uids)
    issues_before = analyze_layout(
        result,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=("P0", "P1", "P2"),
    )
    if not needs_living_dining:
        if layout_issue_score(issues_before) >= layout_issue_score(initial_issues):
            return baseline_result, {
                "status": "skipped_non_improving_issues",
                "issue_counts_before": issue_counts(initial_issues, ("P0", "P1", "P2")),
                "issue_counts_after": issue_counts(issues_before, ("P0", "P1", "P2")),
            }
        return result, {
            "status": "functional_group_cleanup",
            "issue_counts_before": issue_counts(initial_issues, ("P0", "P1", "P2")),
            "issue_counts_after": issue_counts(issues_before, ("P0", "P1", "P2")),
        }
    groups = _functional_group_assets(result, assets)
    if not groups["dining_tables"]:
        return result, {
            "status": "no_dining_table",
            "issue_counts_before": issue_counts(issues_before, ("P0", "P1", "P2")),
        }

    blocker_specs = _build_blocker_specs(
        room_doors,
        room_windows,
        boundary=boundary,
        room_area=room_area,
    )
    blockers = [spec["poly"] for spec in blocker_specs] + [
        protected_path_polygon(path) for path in protected_paths or []
    ]
    room_poly = Polygon([(point[0], point[1]) for point in boundary]).buffer(0.05)
    moved_records: list[dict[str, Any]] = []
    working = result
    before_score = layout_issue_score(issues_before)

    for table in groups["dining_tables"]:
        current_groups = _functional_group_assets(working, assets)
        current_tables = current_groups["dining_tables"]
        current_table = next((item for item in current_tables if item.uid == table.uid), None)
        if current_table is None:
            continue
        group_uids = _dining_cluster_uids_for_table(current_table, current_groups)
        occupied = [
            item.poly
            for item in floor_assets(working, assets, skip_rugs=True)
            if item.uid not in group_uids
        ]
        best_layout: dict[str, Any] | None = None
        best_score: tuple[int, int, int, float] | None = None
        best_to: tuple[float, float] | None = None
        candidate_rotations: list[float] = []
        for rotation_offset in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
            rotation = (current_table.rot_z + rotation_offset) % (2 * math.pi)
            if not any(
                abs(_angle_delta_deg(rotation, existing)) < 1.0
                for existing in candidate_rotations
            ):
                candidate_rotations.append(rotation)

        for candidate_rot_z in candidate_rotations:
            candidate_points = _position_candidates(
                current_table.pos,
                candidate_rot_z,
                current_table.width,
                current_table.depth,
                boundary,
            )
            min_x, min_y, max_x, max_y = room_bounds(boundary=boundary)
            half_x, half_y = projected_half_extents(
                current_table.width,
                current_table.depth,
                candidate_rot_z,
            )
            for violation in issues_before.get("living_dining_clearance_violations", []):
                if str(violation.get("dining_table") or "") != current_table.uid:
                    continue
                living_uid = str(violation.get("living_item") or "")
                living_item = next(
                    (
                        item
                        for item in floor_assets(working, assets, skip_rugs=True)
                        if item.uid == living_uid
                    ),
                    None,
                )
                if living_item is None:
                    continue
                gap = float(violation.get("gap") or 0.0)
                min_gap = float(violation.get("min_gap") or LIVING_DINING_CLEARANCE_M)
                missing = max(0.0, min_gap - gap)
                if missing <= 0:
                    continue
                dx_from_living = float(current_table.pos[0]) - float(living_item.pos[0])
                dy_from_living = float(current_table.pos[1]) - float(living_item.pos[1])
                move_y = abs(dy_from_living) >= abs(dx_from_living)
                sign = 1.0 if (dy_from_living if move_y else dx_from_living) >= 0 else -1.0
                for pad in (0.08, 0.25, 0.45):
                    candidate_x = float(current_table.pos[0])
                    candidate_y = float(current_table.pos[1])
                    if move_y:
                        candidate_y += sign * (missing + pad)
                    else:
                        candidate_x += sign * (missing + pad)
                    candidate_x = clamp(candidate_x, min_x + half_x, max_x - half_x)
                    candidate_y = clamp(candidate_y, min_y + half_y, max_y - half_y)
                    if not any(
                        abs(candidate_x - x) < 1e-6 and abs(candidate_y - y) < 1e-6
                        for x, y in candidate_points
                    ):
                        candidate_points.append((candidate_x, candidate_y))

            for candidate_x, candidate_y in candidate_points:
                dx = float(candidate_x) - float(current_table.pos[0])
                dy = float(candidate_y) - float(current_table.pos[1])
                same_position = abs(dx) < 1e-6 and abs(dy) < 1e-6
                same_rotation = abs(_angle_delta_deg(candidate_rot_z, current_table.rot_z)) < 1.0
                if same_position and same_rotation:
                    continue
                candidate = _translate_layout_group(working, group_uids, dx, dy)
                table_placement = dict(candidate.get(current_table.uid, {}))
                table_rotation = list(
                    table_placement.get("rotation") or [0.0, 0.0, current_table.rot_z]
                )
                while len(table_rotation) < 3:
                    table_rotation.append(0.0)
                table_rotation[2] = candidate_rot_z
                candidate[current_table.uid] = {
                    **table_placement,
                    "rotation": table_rotation[:3],
                }
                candidate = _arrange_dining_chairs_around_tables(candidate, assets)
                candidate = normalize_layout(
                    candidate,
                    assets,
                    boundary,
                    room_windows=room_windows,
                    rehome_service_items=False,
                )
                group_items = [
                    item
                    for item in floor_assets(candidate, assets, skip_rugs=True)
                    if item.uid in group_uids
                ]
                if any(not room_poly.covers(item.poly) for item in group_items):
                    continue
                if any(
                    item.poly.intersects(blocker)
                    for item in group_items
                    for blocker in blockers
                ):
                    continue
                if any(
                    item.poly.intersects(other)
                    for item in group_items
                    for other in occupied
                ):
                    continue
                candidate_issues = analyze_layout(
                    candidate,
                    assets,
                    boundary,
                    room_doors or [],
                    room_area,
                    room_windows=room_windows,
                    protected_paths=protected_paths,
                    tiers=("P0", "P1", "P2"),
                )
                score = (*layout_issue_score(candidate_issues), abs(dx) + abs(dy))
                if best_score is None or score < best_score:
                    best_layout = candidate
                    best_score = score
                    best_to = (float(candidate_x), float(candidate_y))
                if score[:3] == (0, 0, 0):
                    break
            if best_score is not None and best_score[:3] == (0, 0, 0):
                break
        if best_layout is not None and best_score is not None and best_score[:3] < before_score:
            moved_records.append(
                {
                    "uid": current_table.uid,
                    "from": [
                        round(float(current_table.pos[0]), 4),
                        round(float(current_table.pos[1]), 4),
                    ],
                    "to": [round(best_to[0], 4), round(best_to[1], 4)] if best_to else None,
                    "group_uids": sorted(group_uids),
                }
            )
            working = best_layout
            before_score = best_score[:3]

    current_issues = analyze_layout(
        working,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=("P0", "P1", "P2"),
    )
    for violation in list(current_issues.get("living_dining_clearance_violations", [])):
        uid = str(violation.get("living_item") or "")
        if uid not in working:
            continue
        item = next(
            (
                floor_item
                for floor_item in floor_assets(working, assets, skip_rugs=True)
                if floor_item.uid == uid
            ),
            None,
        )
        if item is None:
            continue
        occupied = [
            floor_item.poly
            for floor_item in floor_assets(working, assets, skip_rugs=True)
            if floor_item.uid != uid
        ]
        current_score = layout_issue_score(current_issues)
        best_layout = None
        best_score: tuple[int, int, int, float] | None = None
        best_to: tuple[float, float] | None = None
        for candidate_x, candidate_y in _position_candidates(
            item.pos,
            item.rot_z,
            item.width,
            item.depth,
            boundary,
        ):
            if (
                abs(float(candidate_x) - float(item.pos[0])) < 1e-6
                and abs(float(candidate_y) - float(item.pos[1])) < 1e-6
            ):
                continue
            candidate = move_asset_with_supports(
                working,
                uid,
                float(candidate_x),
                float(candidate_y),
                assets,
            )
            candidate = normalize_layout(
                candidate,
                assets,
                boundary,
                room_windows=room_windows,
                rehome_service_items=False,
            )
            moved_item = next(
                (
                    floor_item
                    for floor_item in floor_assets(candidate, assets, skip_rugs=True)
                    if floor_item.uid == uid
                ),
                None,
            )
            if moved_item is None:
                continue
            if not room_poly.covers(moved_item.poly):
                continue
            if any(moved_item.poly.intersects(blocker) for blocker in blockers):
                continue
            if any(moved_item.poly.intersects(other) for other in occupied):
                continue
            candidate_issues = analyze_layout(
                candidate,
                assets,
                boundary,
                room_doors or [],
                room_area,
                room_windows=room_windows,
                protected_paths=protected_paths,
                tiers=("P0", "P1", "P2"),
            )
            score = (
                *layout_issue_score(candidate_issues),
                abs(float(candidate_x) - float(item.pos[0]))
                + abs(float(candidate_y) - float(item.pos[1])),
            )
            if best_score is None or score < best_score:
                best_layout = candidate
                best_score = score
                best_to = (float(candidate_x), float(candidate_y))
            if score[:3] == (0, 0, 0):
                break
        if best_layout is not None and best_score is not None and best_score[:3] < current_score:
            moved_records.append(
                {
                    "uid": uid,
                    "from": [round(float(item.pos[0]), 4), round(float(item.pos[1]), 4)],
                    "to": [round(best_to[0], 4), round(best_to[1], 4)] if best_to else None,
                    "reason": "living_dining_clearance",
                }
            )
            working = best_layout
            current_issues = analyze_layout(
                working,
                assets,
                boundary,
                room_doors or [],
                room_area,
                room_windows=room_windows,
                protected_paths=protected_paths,
                tiers=("P0", "P1", "P2"),
            )

    issues_after = analyze_layout(
        working,
        assets,
        boundary,
        room_doors or [],
        room_area,
        room_windows=room_windows,
        protected_paths=protected_paths,
        tiers=("P0", "P1", "P2"),
    )
    if layout_issue_score(issues_after) >= layout_issue_score(initial_issues):
        return baseline_result, {
            "status": "skipped_non_improving_issues",
            "planned_moves": moved_records,
            "moved_count": 0,
            "issue_counts_before": issue_counts(
                initial_issues,
                ("P0", "P1", "P2"),
            ),
            "issue_counts_after": issue_counts(
                issues_after,
                ("P0", "P1", "P2"),
            ),
            "change_summary": layout_change_summary(layout, baseline_result),
        }
    return working, {
        "status": "moved" if moved_records else "no_valid_destination",
        "planned_moves": moved_records,
        "moved_count": len(moved_records),
        "issue_counts_before": issue_counts(issues_before, ("P0", "P1", "P2")),
        "issue_counts_after": issue_counts(issues_after, ("P0", "P1", "P2")),
        "change_summary": layout_change_summary(layout, working),
    }

def _apply_finding_moves(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]] | None,
    room_windows: list[dict[str, Any]] | None,
    protected_paths: list[dict[str, Any]] | None,
    room_type: str,
    issue_key: str,
    finding_key: Callable[[dict[str, Any]], tuple[str, ...]],
    finding_moves: Callable[[dict[str, Any]], list[tuple[str, float, float, dict[str, Any]]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Try each current finding's moves in order; keep the first that lowers layout_issue_score.

    Candidates are normalized the way legacy solver operations were.
    """
    def measure(candidate: dict[str, Any]) -> dict[str, Any]:
        return analyze_layout(
            candidate,
            assets,
            boundary,
            room_doors or [],
            room_area,
            room_windows=room_windows,
            protected_paths=protected_paths,
            room_type=room_type,
        )

    working = layout
    issues = measure(working)
    attempted: set[tuple[str, ...]] = set()
    records: list[dict[str, Any]] = []
    while finding := next(
        (item for item in issues.get(issue_key) or [] if finding_key(item) not in attempted),
        None,
    ):
        attempted.add(finding_key(finding))
        score = layout_issue_score(issues)
        for uid, dx, dy, note in finding_moves(finding):
            if uid not in working:
                continue
            pos = working[uid].get("position") or [0.0, 0.0, 0.0]
            candidate = normalize_layout(
                move_asset_with_supports(working, uid, float(pos[0]) + dx, float(pos[1]) + dy, assets),
                assets,
                boundary,
                room_windows=None,
                rehome_service_items=False,
            )
            candidate_issues = measure(candidate)
            if layout_issue_score(candidate_issues) < score:
                working, issues = candidate, candidate_issues
                records.append({"uid": uid, "dx": dx, "dy": dy, **note})
                break
    return working, {"status": "moved" if records else "no_changes", "planned_moves": records}

def clear_protected_paths(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
    room_type: str = "living_room",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move each protected-path blocker by its validator translations_to_clear_m.

    Directions are tried smallest move first. Returns (layout, report) with
    report {"status": "moved" | "no_changes", "planned_moves": [...]}.
    """
    def moves(finding: dict[str, Any]) -> list[tuple[str, float, float, dict[str, Any]]]:
        translations = finding.get("translations_to_clear_m") or {}
        return [
            (
                str(finding.get("uid") or ""),
                float(delta) if direction in {"left", "right"} else 0.0,
                float(delta) if direction in {"down", "up"} else 0.0,
                {"path_id": finding.get("path_id"), "direction": direction},
            )
            for direction, delta in sorted(translations.items(), key=lambda item: abs(float(item[1])))
        ]

    return _apply_finding_moves(
        layout, assets, room_area, boundary, room_doors, room_windows, protected_paths, room_type,
        "protected_path_violations",
        lambda finding: (str(finding.get("uid") or ""), str(finding.get("path_id") or "")),
        moves,
    )

def satisfy_sofa_table_gaps(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
    room_type: str = "living_room",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move the coffee table, else the sofa, to the midpoint of its valid-gap interval.

    Returns (layout, report) with report {"status": "moved" | "no_changes",
    "planned_moves": [...]}.
    """
    def moves(finding: dict[str, Any]) -> list[tuple[str, float, float, dict[str, Any]]]:
        result = []
        for role, reference_role in (("table", "sofa"), ("sofa", "table")):
            interval = finding.get(f"{role}_center_translation_to_valid_gap_m") or {}
            axis = str(interval.get("axis") or "")
            if axis not in {"x", "y"}:
                continue
            delta = (float(interval["min"]) + float(interval["max"])) / 2.0
            result.append((
                str(finding.get(role) or ""),
                delta if axis == "x" else 0.0,
                delta if axis == "y" else 0.0,
                {"reference_uid": str(finding.get(reference_role) or ""), "axis": axis},
            ))
        return result

    return _apply_finding_moves(
        layout, assets, room_area, boundary, room_doors, room_windows, protected_paths, room_type,
        "sofa_coffee_table_violations",
        lambda finding: (str(finding.get("sofa") or ""), str(finding.get("table") or "")),
        moves,
    )
