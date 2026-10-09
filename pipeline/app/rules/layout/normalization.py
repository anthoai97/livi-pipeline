"""Deterministic normalization and support-aware movement."""

import math
from typing import Any
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points
from app.rules.geometry.primitives import (
    asset_polygon,
    extract_placement,
    is_rug,
    projected_half_extents,
    snap_rotation,
)
from app.rules.layout_rules import WALL_FLUSH_MAX_GAP_M
from app.rules.layout.studio import is_freestanding_studio_media_support
from app.rules.planner.taxonomy import SEATING_ROLE_KEYWORDS, SOFA_ROLE_KEYWORDS
from app.rules.pipeline_shared import (
    RoomWall,
    ceiling_mount_z,
    clamp,
    is_floor_lamp_asset,
    is_wall_aligned_asset,
    matches_category_keywords,
    nearest_wall,
    room_bounds,
    support_top_z,
)

from app.rules.layout.constants import (
    FLOOR_LAMP_REACH_RANGE_M,
    FLOOR_LAMP_WALL_MAX_GAP_M,
    MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG,
    SIDE_TABLE_ROLE_KEYWORDS,
    SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG,
    SIDE_TABLE_SERVICE_REACH_MAX_M,
    WINDOW_SEATING_CLEARANCE_M,
)
from app.rules.layout.metrics import (
    FloorAsset,
    get_asset_by_uid,
)
from app.rules.layout.relations import (
    _angle_delta_deg,
    _angle_to,
    _back_wall_name,
    _best_coffee_table_candidate,
    _facing_delta_to_item,
    _floor_asset_list,
    _inset_support_polygon,
    _inward_rotation_for_wall,
    _is_ceiling_mounted_layout_asset,
    _is_floor_only_layout_asset,
    _is_media_display_asset,
    _is_media_support_asset,
    _is_wall_mounted_layout_asset,
    _parent_uid,
    _resolve_stack_chain,
    _support_child_world_position,
    _supported_parent_uid,
    _wall_mount_z,
    _window_seating_clearance_poly,
    _window_wall_line,
    floor_assets,
)


def _wall_gap(wall: RoomWall, pos: list[float], half_across: float) -> float:
    """Signed gap between ``wall`` and the back of a footprint centered at ``pos``."""
    x = float(pos[0])
    y = float(pos[1])
    if wall.name == "left":
        return x - half_across - wall.line_at(y)
    if wall.name == "right":
        return wall.line_at(y) - x - half_across
    if wall.name == "bottom":
        return y - half_across - wall.line_at(x)
    return wall.line_at(x) - y - half_across

def _flush_against_wall(
    wall: RoomWall,
    pos: list[float],
    half_across: float,
    half_along: float,
    gap: float,
) -> tuple[float, float] | None:
    """Center ``gap`` off ``wall`` and within its span; None if the wall is too short."""
    lo, hi = wall.span()
    if hi - lo < 2 * half_along:
        return None
    x = float(pos[0])
    y = float(pos[1])
    if wall.vertical:
        y = clamp(y, lo + half_along, hi - half_along)
        line = wall.line_at(y)
        x = line + half_across + gap if wall.name == "left" else line - half_across - gap
    else:
        x = clamp(x, lo + half_along, hi - half_along)
        line = wall.line_at(x)
        y = line + half_across + gap if wall.name == "bottom" else line - half_across - gap
    return x, y

def _snap_wall_aligned_placements(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    for uid, placement in result.items():
        asset = get_asset_by_uid(uid, assets)
        if is_freestanding_studio_media_support(asset):
            continue
        if not is_wall_aligned_asset(
            asset.get("category", "") or placement.get("category", ""),
            uid,
        ):
            continue
        pos, current_rot_z, width, depth, _ = extract_placement(placement, asset)
        wall = nearest_wall(pos, boundary)
        is_sofa = matches_category_keywords(
            asset.get("category", ""),
            uid,
            SOFA_ROLE_KEYWORDS,
        )
        if is_sofa:
            half_x, half_y = projected_half_extents(
                width,
                depth,
                current_rot_z,
            )
            if _wall_gap(wall, pos, half_x if wall.vertical else half_y) > WALL_FLUSH_MAX_GAP_M:
                continue
            # A sofa can sit beside a wall while facing a coffee table along the
            # other axis. Only canonicalize its facing when the nearby wall is
            # already the sofa's back wall; otherwise the wall is beside it and
            # snapping would destroy the functional seating relationship.
            if _back_wall_name(current_rot_z) != wall.name:
                continue
            placement["rotation"] = [0.0, 0.0, _inward_rotation_for_wall(wall.name)]
            continue
        rot_z = _inward_rotation_for_wall(wall.name)
        half_x, half_y = projected_half_extents(width, depth, rot_z)
        half_across, half_along = (half_x, half_y) if wall.vertical else (half_y, half_x)
        if 0.0 <= _wall_gap(wall, pos, half_across) <= WALL_FLUSH_MAX_GAP_M:
            placement["rotation"] = [0.0, 0.0, rot_z]
            continue
        flush = _flush_against_wall(wall, pos, half_across, half_along, 0.05)
        if flush is None:
            continue
        placement["rotation"] = [0.0, 0.0, rot_z]
        placement["position"] = [flush[0], flush[1], pos[2]]
    return result

def _snap_wall_mounted_positions(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    for uid, placement in result.items():
        asset = get_asset_by_uid(uid, assets)
        if not _is_wall_mounted_layout_asset(asset, uid):
            continue
        if _supported_parent_uid(uid, placement, result, assets):
            continue
        pos, _, width, _, _ = extract_placement(placement, asset)
        depth = float(asset.get("depth", 0.1) or 0.1)
        wall = nearest_wall(pos, boundary)
        flush = _flush_against_wall(wall, pos, depth / 2 + 0.02, width / 2, 0.0)
        if flush is None:
            continue
        result[uid] = {
            **placement,
            "position": [flush[0], flush[1], _wall_mount_z(asset)],
            "rotation": [0, 0, _inward_rotation_for_wall(wall.name)],
        }
    return result

def _rug_fit_rotations(rotation_z: float) -> list[float]:
    rotations: list[float] = []
    for candidate in (
        rotation_z,
        snap_rotation(rotation_z, math.pi / 2),
        0.0,
        math.pi / 2,
        math.pi,
        3 * math.pi / 2,
    ):
        normalized = candidate % (2 * math.pi)
        if not any(
            math.isclose(normalized, existing, abs_tol=1e-6)
            for existing in rotations
        ):
            rotations.append(normalized)
    return rotations

def _fit_rugs_to_room(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> dict[str, Any]:
    min_x, min_y, max_x, max_y = room_bounds(boundary=boundary)
    room_poly = Polygon(boundary).buffer(1e-6)
    result = {uid: dict(placement) for uid, placement in layout.items()}

    for uid, placement in result.items():
        asset = get_asset_by_uid(uid, assets)
        if not is_rug(uid, asset):
            continue
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        best: tuple[tuple[int, float, float, int], dict[str, Any]] | None = None
        for idx, candidate_rot in enumerate(_rug_fit_rotations(rot_z)):
            half_x, half_y = projected_half_extents(width, depth, candidate_rot)
            if half_x * 2 > max_x - min_x + 1e-6 or half_y * 2 > max_y - min_y + 1e-6:
                continue
            x = clamp(float(pos[0]), min_x + half_x, max_x - half_x)
            y = clamp(float(pos[1]), min_y + half_y, max_y - half_y)
            candidate_pos = [x, y, float(pos[2])]
            if not room_poly.covers(
                asset_polygon(candidate_pos, candidate_rot, width, depth)
            ):
                continue
            rot_delta = abs((candidate_rot - rot_z + math.pi) % (2 * math.pi) - math.pi)
            score = (
                1 if rot_delta > 1e-6 else 0,
                math.hypot(x - float(pos[0]), y - float(pos[1])),
                rot_delta,
                idx,
            )
            rotation = list(placement.get("rotation") or [0.0, 0.0, candidate_rot])
            while len(rotation) < 3:
                rotation.append(0.0)
            rotation[2] = candidate_rot
            candidate = {
                **placement,
                "position": candidate_pos,
                "rotation": rotation,
            }
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            result[uid] = best[1]
    return result

def _shift_sofas_clear_of_windows(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
    room_windows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not room_windows:
        return layout

    min_x, min_y, max_x, max_y = room_bounds(boundary=boundary)
    room_poly = Polygon(boundary).buffer(1e-6)
    result = {uid: dict(placement) for uid, placement in layout.items()}
    for uid, placement in list(result.items()):
        asset = get_asset_by_uid(uid, assets)
        if not matches_category_keywords(
            asset.get("category", ""),
            uid,
            SOFA_ROLE_KEYWORDS,
        ):
            continue
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        x = float(pos[0])
        y = float(pos[1])
        original_x = x
        original_y = y
        for window in room_windows:
            clearance_zone = _window_seating_clearance_poly(window, boundary)
            poly = asset_polygon([x, y, float(pos[2])], rot_z, width, depth)
            if not poly.intersects(clearance_zone):
                continue
            wall_line = _window_wall_line(window, boundary)
            if wall_line is None:
                continue
            wall, line = wall_line
            poly_min_x, poly_min_y, poly_max_x, poly_max_y = poly.bounds
            pad = 0.02
            if wall == "top":
                y -= max(0.0, poly_max_y - (line - WINDOW_SEATING_CLEARANCE_M))
                y -= pad
            elif wall == "bottom":
                y += max(0.0, line + WINDOW_SEATING_CLEARANCE_M - poly_min_y)
                y += pad
            elif wall == "left":
                x += max(0.0, line + WINDOW_SEATING_CLEARANCE_M - poly_min_x)
                x += pad
            else:
                x -= max(0.0, poly_max_x - (line - WINDOW_SEATING_CLEARANCE_M))
                x -= pad

        half_x, half_y = projected_half_extents(width, depth, rot_z)
        x = clamp(x, min_x + half_x, max_x - half_x)
        y = clamp(y, min_y + half_y, max_y - half_y)
        if not room_poly.covers(asset_polygon([x, y, float(pos[2])], rot_z, width, depth)):
            continue
        result[uid] = {
            **placement,
            "position": [x, y, float(pos[2])],
        }
        dx = x - original_x
        dy = y - original_y
        if math.hypot(dx, dy) <= 1e-6:
            continue
        table_candidate = _best_coffee_table_candidate(
            uid,
            asset,
            placement,
            layout,
            assets,
        )
        if not table_candidate:
            continue
        if table_candidate["forward"] <= 0 or table_candidate["gap"] > 1.0:
            continue
        table_uid = str(table_candidate["table"])
        table_placement = result.get(table_uid)
        if not table_placement:
            continue
        table_asset = get_asset_by_uid(table_uid, assets)
        table_pos, table_rot_z, table_width, table_depth, _ = extract_placement(
            table_placement,
            table_asset,
        )
        table_half_x, table_half_y = projected_half_extents(
            table_width,
            table_depth,
            table_rot_z,
        )
        table_x = clamp(
            float(table_pos[0]) + dx,
            min_x + table_half_x,
            max_x - table_half_x,
        )
        table_y = clamp(
            float(table_pos[1]) + dy,
            min_y + table_half_y,
            max_y - table_half_y,
        )
        table_poly = asset_polygon(
            [table_x, table_y, float(table_pos[2])],
            table_rot_z,
            table_width,
            table_depth,
        )
        if not room_poly.covers(table_poly) or any(
            table_poly.intersects(other.poly)
            for other in floor_assets(result, assets, skip_rugs=True)
            if other.uid != table_uid and not _parent_uid(result.get(other.uid, {}))
        ):
            continue
        result = move_asset_with_supports(result, table_uid, table_x, table_y, assets)
    return result

def _align_media_displays_to_supports(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    for uid, placement in list(result.items()):
        asset = get_asset_by_uid(uid, assets)
        if not _is_media_display_asset(asset, uid):
            continue
        parent_uid = _parent_uid(placement)
        if not parent_uid or parent_uid not in result:
            continue
        parent_asset = get_asset_by_uid(parent_uid, assets)
        if not _is_media_support_asset(parent_asset, parent_uid):
            continue
        _, display_rot_z, _, _, _ = extract_placement(placement, asset)
        _, support_rot_z, _, _, _ = extract_placement(
            result[parent_uid],
            parent_asset,
        )
        if (
            _angle_delta_deg(display_rot_z, support_rot_z)
            <= MEDIA_SUPPORT_ROTATION_MAX_DELTA_DEG
        ):
            continue
        rotation = list(placement.get("rotation") or [0.0, 0.0, display_rot_z])
        while len(rotation) < 3:
            rotation.append(0.0)
        rotation[2] = support_rot_z
        result[uid] = {**placement, "rotation": rotation[:3]}
    return result

def _service_item_fits(
    *,
    uid: str,
    poly: Polygon,
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_poly: Polygon,
) -> bool:
    if not room_poly.covers(poly):
        return False
    for other in floor_assets(layout, assets, skip_rugs=True):
        if other.uid == uid:
            continue
        if poly.intersection(other.poly).area > 1e-6:
            return False
    return True

def _service_slot_candidates(
    item: FloorAsset,
    seat: FloorAsset,
    *,
    gap: float,
    include_rear: bool,
) -> list[tuple[list[float], float]]:
    front_x = math.cos(seat.rot_z)
    front_y = math.sin(seat.rot_z)
    side_x = -front_y
    side_y = front_x
    side_gap = seat.width / 2 + max(item.width, item.depth) / 2 + gap
    rear_offset = max(0.2, seat.depth * 0.35)
    candidates: list[tuple[list[float], float]] = []
    for sign in (1.0, -1.0):
        targets = [
            (
                seat.pos[0] + sign * side_x * side_gap,
                seat.pos[1] + sign * side_y * side_gap,
            )
        ]
        if include_rear:
            targets.append(
                (
                    seat.pos[0] + sign * side_x * side_gap - front_x * rear_offset,
                    seat.pos[1] + sign * side_y * side_gap - front_y * rear_offset,
                )
            )
        for target in targets:
            rotation = _angle_to([target[0], target[1], 0.0], seat.pos)
            candidates.append(
                ([float(target[0]), float(target[1]), item.pos[2]], rotation)
            )
    return candidates

def _move_service_item_to_seat_slot(
    result: dict[str, Any],
    assets: list[dict[str, Any]],
    item: FloorAsset,
    seating: list[FloorAsset],
    *,
    room_poly: Polygon,
    gap_range: tuple[float, float],
    include_rear: bool,
    wall_max_gap: float | None = None,
) -> dict[str, Any]:
    min_gap, max_gap = gap_range
    target_gap = (min_gap + max_gap) / 2
    candidate_options: list[tuple[float, float, float, list[float], float]] = []
    for seat in seating:
        for pos, rotation in _service_slot_candidates(
            item,
            seat,
            gap=target_gap,
            include_rear=include_rear,
        ):
            poly = asset_polygon(pos, rotation, item.width, item.depth)
            if not _service_item_fits(
                uid=item.uid,
                poly=poly,
                layout=result,
                assets=assets,
                room_poly=room_poly,
            ):
                continue
            gap = float(poly.distance(seat.poly))
            if gap < min_gap or gap > max_gap:
                continue
            wall_gap = float(room_poly.exterior.distance(poly))
            wall_penalty = (
                max(0.0, wall_gap - wall_max_gap)
                if wall_max_gap is not None
                else 0.0
            )
            candidate_options.append(
                (wall_penalty, abs(gap - target_gap), wall_gap, pos, rotation)
            )

    if not candidate_options:
        return result
    _, _, _, pos, rotation_z = min(candidate_options, key=lambda option: option[:3])
    placement = dict(result[item.uid])
    placement["position"] = [float(pos[0]), float(pos[1]), float(pos[2])]
    placement["rotation"] = [0.0, 0.0, float(rotation_z)]
    return {**result, item.uid: placement}

def _rehome_service_items(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> dict[str, Any]:
    result = {uid: dict(placement) for uid, placement in layout.items()}
    room_poly = Polygon(boundary)

    for item in list(floor_assets(result, assets, keywords=SIDE_TABLE_ROLE_KEYWORDS)):
        seating = _floor_asset_list(result, assets, keywords=SEATING_ROLE_KEYWORDS)
        if not seating:
            break
        nearest_seat, nearest_gap = min(
            ((seat, float(item.poly.distance(seat.poly))) for seat in seating),
            key=lambda entry: entry[1],
        )
        if (
            nearest_gap <= SIDE_TABLE_SERVICE_REACH_MAX_M
            and _facing_delta_to_item(nearest_seat, item)
            <= SIDE_TABLE_SERVICE_MAX_FACING_DELTA_DEG
        ):
            continue
        result = _move_service_item_to_seat_slot(
            result,
            assets,
            item,
            seating,
            room_poly=room_poly,
            gap_range=(0.05, SIDE_TABLE_SERVICE_REACH_MAX_M),
            include_rear=False,
        )

    for item in list(floor_assets(result, assets)):
        if not is_floor_lamp_asset(item.asset.get("category", ""), item.uid):
            continue
        seating = _floor_asset_list(result, assets, keywords=SEATING_ROLE_KEYWORDS)
        if not seating:
            break
        _, nearest_gap = min(
            ((seat, float(item.poly.distance(seat.poly))) for seat in seating),
            key=lambda entry: entry[1],
        )
        min_gap, max_gap = FLOOR_LAMP_REACH_RANGE_M
        wall_gap = float(room_poly.exterior.distance(item.poly))
        if min_gap <= nearest_gap <= max_gap and wall_gap <= FLOOR_LAMP_WALL_MAX_GAP_M:
            continue
        result = _move_service_item_to_seat_slot(
            result,
            assets,
            item,
            seating,
            room_poly=room_poly,
            gap_range=FLOOR_LAMP_REACH_RANGE_M,
            include_rear=True,
            wall_max_gap=FLOOR_LAMP_WALL_MAX_GAP_M,
        )

    return result

def _correct_z(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {uid: {**placement} for uid, placement in layout.items()}
    support_child_counts: dict[str, int] = {}

    for uid in _resolve_stack_chain(result):
        placement = result[uid]
        asset = get_asset_by_uid(uid, assets)
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)

        ceiling_mounted = _is_ceiling_mounted_layout_asset(asset, uid)
        wall_mounted = _is_wall_mounted_layout_asset(asset, uid)
        floor_only = _is_floor_only_layout_asset(asset, uid)
        raw_parent_uid = _parent_uid(placement)
        parent_uid = _supported_parent_uid(uid, placement, result, assets)
        parent_placement = result.get(parent_uid) if parent_uid else None
        fixed_height = (
            ceiling_mounted
            or floor_only
            or (wall_mounted and not parent_placement)
        )
        if parent_placement and not ceiling_mounted and not floor_only:
            parent_asset = get_asset_by_uid(parent_uid, assets)
            p_pos, p_rot_z, p_width, p_depth, _ = extract_placement(
                parent_placement, parent_asset
            )
            child_index = support_child_counts.get(parent_uid, 0)
            support_child_counts[parent_uid] = child_index + 1
            parent_poly = asset_polygon(p_pos, p_rot_z, p_width, p_depth)
            point = Point(pos[0], pos[1])
            if not parent_poly.buffer(0.01).contains(point):
                snapped, _ = nearest_points(parent_poly, point)
                pos[0], pos[1] = float(snapped.x), float(snapped.y)
            support_poly = _inset_support_polygon(parent_poly, p_width, p_depth)
            item_poly = asset_polygon(pos, rot_z, width, depth)
            if raw_parent_uid != parent_uid or not support_poly.covers(item_poly):
                pos[0], pos[1] = _support_child_world_position(
                    p_pos,
                    p_rot_z,
                    p_width,
                    p_depth,
                    width,
                    depth,
                    child_index,
                )
            pos[2] = support_top_z(parent_asset, p_pos[2])
        elif ceiling_mounted:
            pos[2] = ceiling_mount_z(asset.get("height", 0.3))
        elif floor_only:
            pos[2] = 0.0
        elif wall_mounted:
            pos[2] = _wall_mount_z(asset)

        normalized = {**placement, "position": pos}
        if parent_placement and not ceiling_mounted and not floor_only:
            normalized["on_top_of"] = parent_uid
        if fixed_height:
            normalized.pop("on_top_of", None)
        result[uid] = normalized
    return result

def normalize_layout(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
    room_windows: list[dict[str, Any]] | None = None,
    rehome_service_items: bool = True,
) -> dict[str, Any]:
    """Canonicalize a layout within the room polygon.

    Snaps wall-aligned and wall-mounted items, fits rugs, shifts sofas clear of
    windows, seats displays on their supports, rehomes side tables and floor
    lamps, and sets heights. Returns a new layout keyed like the input. Legacy
    fresh designs pass room_windows=None and rehome_service_items=False.
    """
    result = _snap_wall_aligned_placements(layout, assets, boundary)
    result = _snap_wall_mounted_positions(result, assets, boundary)
    result = _fit_rugs_to_room(result, assets, boundary)
    result = _shift_sofas_clear_of_windows(
        result,
        assets,
        boundary,
        room_windows,
    )
    result = _align_media_displays_to_supports(result, assets)
    if rehome_service_items:
        result = _rehome_service_items(result, assets, boundary)
    return _correct_z(result, assets)

def move_asset_with_supports(
    layout: dict[str, Any],
    uid: str,
    new_x: float,
    new_y: float,
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    if uid not in layout:
        return layout

    result = {item_uid: dict(placement) for item_uid, placement in layout.items()}
    old_pos = list(result[uid].get("position", [0.0, 0.0, 0.0]))
    if len(old_pos) < 3:
        old_pos += [0.0] * (3 - len(old_pos))
    dx = float(new_x) - old_pos[0]
    dy = float(new_y) - old_pos[1]

    moved_pos = [float(new_x), float(new_y), float(old_pos[2])]
    result[uid] = {**result[uid], "position": moved_pos}

    for child_uid, placement in result.items():
        if child_uid == uid or _parent_uid(placement) != uid:
            continue
        child_pos = list(placement.get("position", [0.0, 0.0, 0.0]))
        if len(child_pos) < 3:
            child_pos += [0.0] * (3 - len(child_pos))
        child_pos[0] += dx
        child_pos[1] += dy
        result[child_uid] = {**placement, "position": child_pos}

    return result
