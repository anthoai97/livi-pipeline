"""Deterministic layout geometry candidates, ported from core/geometry/candidates.py."""

from __future__ import annotations
import math
from typing import Any
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.taxonomy import SEATING_ROLE_KEYWORDS as _SEATING_ROLE_KEYWORDS
from app.rules.geometry.primitives import (
    _FLOOR_LAMP_SEAT_GAP,
    _MEDIA_DISPLAY_ROLE_KEYWORDS,
    _MEDIA_SUPPORT_ROLE_KEYWORDS,
    _SEED_MARGIN,
    _SEED_WALL_FRACTIONS,
    _SUPPORT_INSET_M,
    _WALL_PLACEMENT_MODES,
    asset_polygon,
    extract_placement,
    frange,
    media_display_fits_support_dimensions,
    normalize_rotation,
    projected_half_extents,
    yaw_to_object_rotation,
)
from app.rules.pipeline_shared import (
    clamp,
    is_table_lamp_asset,
    is_table_lamp_support_asset,
    is_tabletop_support_asset,
    is_wall_aligned_asset,
    is_wall_mounted_asset,
    matches_category_keywords,
    tabletop_support_priority,
)

def _rotation_towards_room_center(
    position: tuple[float, float],
    room_center: tuple[float, float],
) -> float:
    dx = room_center[0] - position[0]
    dy = room_center[1] - position[1]
    if abs(dx) >= abs(dy):
        return 0.0 if dx >= 0 else math.pi
    return math.pi / 2 if dy >= 0 else 3 * math.pi / 2

def _wall_candidates(
    room_width: float,
    room_depth: float,
    width: float,
    depth: float,
    wall_offset: int,
    preferred_walls: list[str] | None = None,
) -> list[tuple[list[float], float]]:
    walls = ("top", "bottom", "left", "right")
    ordered_walls = list(walls[wall_offset % 4 :] + walls[: wall_offset % 4])
    preferred = [wall for wall in (preferred_walls or []) if wall in walls]
    ordered_walls = preferred + [wall for wall in ordered_walls if wall not in preferred]
    candidates: list[tuple[list[float], float]] = []

    for wall in ordered_walls:
        rotation = {
            "top": 3 * math.pi / 2,
            "bottom": math.pi / 2,
            "left": 0.0,
            "right": math.pi,
        }[wall]
        half_x, half_y = projected_half_extents(width, depth, rotation)
        for fraction in _SEED_WALL_FRACTIONS:
            if wall == "top":
                x = clamp(room_width * fraction, half_x + _SEED_MARGIN, room_width - half_x - _SEED_MARGIN)
                y = room_depth - half_y - _SEED_MARGIN
            elif wall == "bottom":
                x = clamp(room_width * fraction, half_x + _SEED_MARGIN, room_width - half_x - _SEED_MARGIN)
                y = half_y + _SEED_MARGIN
            elif wall == "left":
                x = half_x + _SEED_MARGIN
                y = clamp(room_depth * fraction, half_y + _SEED_MARGIN, room_depth - half_y - _SEED_MARGIN)
            else:
                x = room_width - half_x - _SEED_MARGIN
                y = clamp(room_depth * fraction, half_y + _SEED_MARGIN, room_depth - half_y - _SEED_MARGIN)
            candidates.append(([x, y], rotation))

    return candidates

def _grid_candidates(
    room_width: float,
    room_depth: float,
    width: float,
    depth: float,
) -> list[tuple[list[float], float]]:
    radius = max(width, depth) / 2 + _SEED_MARGIN
    step = max(min(width, depth) * 0.75, 0.35)
    xs = frange(radius, room_width - radius, step)
    ys = frange(radius, room_depth - radius, step)
    room_center = (room_width / 2, room_depth / 2)
    points = sorted(
        ((x, y) for x in xs for y in ys),
        key=lambda point: (
            abs(point[0] - room_center[0]) + abs(point[1] - room_center[1]),
            abs(point[1] - room_center[1]),
            abs(point[0] - room_center[0]),
            point[1],
            point[0],
        ),
    )

    candidates: list[tuple[list[float], float]] = []
    for x, y in points:
        preferred = _rotation_towards_room_center((x, y), room_center)
        for index in range(4):
            rotation = (preferred + index * math.pi / 2) % (2 * math.pi)
            candidates.append(([x, y], rotation))
    return candidates

def _centered_candidates(
    room_area: tuple[float, float],
    width: float,
    depth: float,
    target: tuple[float, float],
    preferred_rotation: float | None = None,
) -> list[tuple[list[float], float]]:
    room_width, room_depth = room_area
    step = max(min(width, depth) * 0.65, 0.3)
    offsets = (
        (0.0, 0.0),
        (step, 0.0),
        (-step, 0.0),
        (0.0, step),
        (0.0, -step),
        (step, step),
        (step, -step),
        (-step, step),
        (-step, -step),
    )
    rotations = [preferred_rotation] if preferred_rotation is not None else [0.0]
    if preferred_rotation is not None:
        rotations.extend((preferred_rotation + index * math.pi / 2) % (2 * math.pi) for index in range(1, 4))
    else:
        rotations = [index * math.pi / 2 for index in range(4)]

    candidates: list[tuple[list[float], float]] = []
    for dx, dy in offsets:
        for rotation in rotations:
            half_x, half_y = projected_half_extents(width, depth, rotation)
            x = clamp(target[0] + dx, half_x + _SEED_MARGIN, room_width - half_x - _SEED_MARGIN)
            y = clamp(target[1] + dy, half_y + _SEED_MARGIN, room_depth - half_y - _SEED_MARGIN)
            candidates.append(([x, y], rotation))
    return candidates

def _rotation_towards_point(
    source: tuple[float, float],
    target: tuple[float, float],
) -> float:
    dx = target[0] - source[0]
    dy = target[1] - source[1]
    if abs(dx) >= abs(dy):
        return 0.0 if dx >= 0 else math.pi
    return math.pi / 2 if dy >= 0 else 3 * math.pi / 2

def _angular_delta(a: float, b: float) -> float:
    return abs((normalize_rotation(a - b + math.pi) - math.pi))

def _opposite_wall_name(wall: str | None) -> str | None:
    return {
        "top": "bottom",
        "bottom": "top",
        "left": "right",
        "right": "left",
    }.get(str(wall or "").strip().lower())

def _wall_from_rotation(rotation_z: float) -> str | None:
    wall_rotations = {
        "left": 0.0,
        "bottom": math.pi / 2,
        "right": math.pi,
        "top": 3 * math.pi / 2,
    }
    wall, delta = min(
        wall_rotations.items(),
        key=lambda item: _angular_delta(rotation_z, item[1]),
    )
    return wall if delta <= math.pi / 4 else None

def _placed_reference(
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    uid: str | None,
) -> tuple[list[float], float, float, float, float] | None:
    if not uid or uid not in layout or uid not in asset_map:
        return None
    return extract_placement(layout[uid], asset_map[uid])

def _anchor_reference(
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    seed_plan: dict[str, Any],
    uid: str | None = None,
) -> tuple[list[float], float, float, float, float] | None:
    associated_uid = (seed_plan.get("anchor_by_uid") or {}).get(uid)
    if associated_uid:
        return _placed_reference(layout, asset_map, associated_uid)
    primary = _placed_reference(layout, asset_map, seed_plan.get("primary_anchor_uid"))
    if primary is not None:
        return primary
    placement_modes = seed_plan.get("placement_mode_by_uid") or {}
    ordered_uids = seed_plan.get("ordered_uids") or []
    for uid in ordered_uids:
        if placement_modes.get(uid) == "anchor_wall":
            placed = _placed_reference(layout, asset_map, uid)
            if placed is not None:
                return placed
    return None

def _placed_reference_by_mode(
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    seed_plan: dict[str, Any],
    modes: set[str],
) -> tuple[list[float], float, float, float, float] | None:
    placement_modes = seed_plan.get("placement_mode_by_uid") or {}
    ordered_uids = seed_plan.get("ordered_uids") or []
    for uid in ordered_uids:
        if placement_modes.get(uid) not in modes:
            continue
        placed = _placed_reference(layout, asset_map, uid)
        if placed is not None:
            return placed
    return None

def _seating_references(
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
) -> list[tuple[list[float], float, float, float, float]]:
    seats = []
    for uid, placement in layout.items():
        asset = asset_map.get(uid)
        if not asset or not matches_category_keywords(
            asset.get("category", ""),
            uid,
            _SEATING_ROLE_KEYWORDS,
        ):
            continue
        pos, rot_z, width, depth, height = extract_placement(placement, asset)
        if pos[2] > 0.1:
            continue
        seats.append((pos, rot_z, width, depth, height))
    return seats

def _is_media_display_asset(asset: dict[str, Any], uid: str) -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        _MEDIA_DISPLAY_ROLE_KEYWORDS,
    )

def _is_media_support_asset(asset: dict[str, Any], uid: str) -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        _MEDIA_SUPPORT_ROLE_KEYWORDS,
    )

def _requires_support_surface(uid: str, asset: dict[str, Any]) -> bool:
    category = asset.get("category", "")
    if is_table_lamp_asset(category, uid):
        return True
    return placement_mode_for_asset({**asset, "uid": uid}) == "tabletop"

def _support_surface_eligible(
    uid: str,
    asset: dict[str, Any],
    parent_uid: str,
    parent_asset: dict[str, Any],
) -> bool:
    parent_category = parent_asset.get("category", "")
    if parent_uid == uid or _requires_support_surface(parent_uid, parent_asset):
        return False
    if _is_media_display_asset(asset, uid):
        return _is_media_support_asset(parent_asset, parent_uid)
    if is_table_lamp_asset(asset.get("category", ""), uid):
        if is_table_lamp_support_asset(parent_category, parent_uid):
            return True
        explicit_support_uid = str(
            asset.get("paired_support_uid") or asset.get("support_uid") or ""
        ).strip()
        return (
            explicit_support_uid == parent_uid
            and is_tabletop_support_asset(parent_category, parent_uid)
        )
    return is_tabletop_support_asset(parent_category, parent_uid)

def _support_usable_size(width: float, depth: float) -> tuple[float, float]:
    return (
        max(0.0, width - 2 * _SUPPORT_INSET_M),
        max(0.0, depth - 2 * _SUPPORT_INSET_M),
    )

def _support_fit_margins(
    asset: dict[str, Any],
    parent_width: float,
    parent_depth: float,
) -> tuple[float, float]:
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    usable_width, usable_depth = _support_usable_size(parent_width, parent_depth)
    return (
        (usable_width - width) / 2,
        (usable_depth - depth) / 2,
    )

def _support_surface_fits(
    asset: dict[str, Any],
    parent_width: float,
    parent_depth: float,
) -> bool:
    margin_x, margin_y = _support_fit_margins(asset, parent_width, parent_depth)
    return margin_x >= -1e-6 and margin_y >= -1e-6

def _support_local_offset(
    uid: str,
    asset: dict[str, Any],
    parent_width: float,
    parent_depth: float,
) -> tuple[float, float]:
    margin_x, margin_y = _support_fit_margins(asset, parent_width, parent_depth)
    if _is_media_display_asset(asset, uid):
        return 0.0, 0.0

    max_x = max(0.0, margin_x)
    max_y = max(0.0, margin_y)
    sign = -1.0 if sum(ord(ch) for ch in uid) % 2 else 1.0
    offset_x = sign * max_x * 0.6 if max_x >= 0.04 else 0.0
    offset_y = -max_y * 0.35 if max_y >= 0.04 else 0.0
    return offset_x, offset_y

def _local_to_world(
    parent_pos: list[float],
    parent_rot_z: float,
    offset: tuple[float, float],
) -> tuple[float, float]:
    object_rotation = yaw_to_object_rotation(parent_rot_z)
    cos_a = math.cos(object_rotation)
    sin_a = math.sin(object_rotation)
    local_x, local_y = offset
    return (
        float(parent_pos[0]) + local_x * cos_a - local_y * sin_a,
        float(parent_pos[1]) + local_x * sin_a + local_y * cos_a,
    )

def _support_surface_reference(
    child_uid: str,
    asset: dict[str, Any],
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
) -> tuple[
    tuple[
        str,
        dict[str, Any],
        list[float],
        float,
        float,
        float,
        float,
        tuple[float, float],
    ]
    | None,
    str,
]:
    candidates = []
    saw_eligible = False
    seats = _seating_references(layout, asset_map)
    explicit_parent_uid = next(
        (
            parent_uid
            for field in ("paired_support_uid", "support_uid")
            if (parent_uid := str(asset.get(field) or ""))
            and parent_uid != child_uid
            and parent_uid in layout
            and parent_uid in asset_map
            and _support_surface_eligible(
                child_uid,
                asset,
                parent_uid,
                asset_map[parent_uid],
            )
        ),
        None,
    )
    parent_placements = (
        [(explicit_parent_uid, layout[explicit_parent_uid])]
        if explicit_parent_uid
        else layout.items()
    )
    for parent_uid, placement in parent_placements:
        parent_asset = asset_map.get(parent_uid)
        if not parent_asset or not _support_surface_eligible(
            child_uid,
            asset,
            parent_uid,
            parent_asset,
        ):
            continue
        saw_eligible = True
        parent_category = parent_asset.get("category", "")
        pos, rot_z, width, depth, height = extract_placement(placement, parent_asset)
        if _is_media_display_asset(asset, child_uid):
            fits_support = media_display_fits_support_dimensions(
                float(asset.get("width", 0.5) or 0.5),
                float(asset.get("depth", 0.5) or 0.5),
                width,
                depth,
            )
        else:
            fits_support = _support_surface_fits(asset, width, depth)
        if not fits_support:
            continue
        offset = _support_local_offset(child_uid, asset, width, depth)
        nearest_seat_distance = (
            min(math.hypot(pos[0] - seat[0][0], pos[1] - seat[0][1]) for seat in seats)
            if seats and is_table_lamp_asset(asset.get("category", ""), child_uid)
            else 0.0
        )
        if _is_media_display_asset(asset, child_uid):
            support_rank = 0 if _is_media_support_asset(parent_asset, parent_uid) else 1
        elif is_table_lamp_asset(asset.get("category", ""), child_uid):
            support_rank = (
                0
                if is_table_lamp_support_asset(
                    parent_asset.get("category", ""),
                    parent_uid,
                )
                else 1
            )
        else:
            support_rank = tabletop_support_priority(parent_category, parent_uid)
        support_area = width * depth
        candidates.append(
            (
                support_rank,
                nearest_seat_distance,
                -support_area,
                parent_uid,
                parent_asset,
                pos,
                rot_z,
                width,
                depth,
                height,
                offset,
            )
        )
    if not candidates:
        return None, "support_surface_too_small" if saw_eligible else "no_support_surface"
    _, _, _, parent_uid, parent_asset, pos, rot_z, width, depth, height, offset = min(
        candidates,
        key=lambda item: item[:4],
    )
    return (parent_uid, parent_asset, pos, rot_z, width, depth, height, offset), ""

def _beside_seat_candidates(
    asset: dict[str, Any],
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
) -> list[tuple[list[float], float]]:
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    candidates: list[tuple[list[float], float]] = []
    for seat_pos, seat_rot, seat_w, seat_d, _ in _seating_references(layout, asset_map):
        front_x = math.cos(seat_rot)
        front_y = math.sin(seat_rot)
        side_x = -math.sin(seat_rot)
        side_y = math.cos(seat_rot)
        side_gap = (
            seat_w / 2
            + _FLOOR_LAMP_SEAT_GAP
            + max(width, depth) / 2
        )
        rear_offset = max(0.2, seat_d * 0.35)
        seat_center = (float(seat_pos[0]), float(seat_pos[1]))
        seat_poly = asset_polygon(seat_pos, seat_rot, seat_w, seat_d)
        for sign in (1.0, -1.0):
            raw_targets = (
                (
                    seat_center[0] + sign * side_x * side_gap,
                    seat_center[1] + sign * side_y * side_gap,
                ),
                (
                    seat_center[0] + sign * side_x * side_gap - front_x * rear_offset,
                    seat_center[1] + sign * side_y * side_gap - front_y * rear_offset,
                ),
            )
            for raw_target in raw_targets:
                target = raw_target
                rotation = _rotation_towards_point(target, seat_center)
                away_x = target[0] - seat_center[0]
                away_y = target[1] - seat_center[1]
                away_len = math.hypot(away_x, away_y) or 1.0
                away_x /= away_len
                away_y /= away_len
                for _ in range(3):
                    lamp_poly = asset_polygon(
                        [target[0], target[1], 0.0],
                        rotation,
                        width,
                        depth,
                    )
                    gap_delta = _FLOOR_LAMP_SEAT_GAP - float(
                        lamp_poly.distance(seat_poly)
                    )
                    if abs(gap_delta) <= 0.01:
                        break
                    target = (
                        target[0] + away_x * gap_delta,
                        target[1] + away_y * gap_delta,
                    )
                candidates.append(
                    ([float(target[0]), float(target[1])], rotation)
                )
    return candidates

def _front_of_reference_candidates(
    room_area: tuple[float, float],
    width: float,
    depth: float,
    reference: tuple[list[float], float, float, float, float],
    *,
    gap: float,
) -> list[tuple[list[float], float]]:
    ref_pos, ref_rot, _, ref_d, _ = reference
    ref_center = (float(ref_pos[0]), float(ref_pos[1]))
    front_x = math.cos(ref_rot)
    front_y = math.sin(ref_rot)
    target = (
        ref_center[0] + front_x * (ref_d / 2 + gap + depth / 2),
        ref_center[1] + front_y * (ref_d / 2 + gap + depth / 2),
    )
    return _centered_candidates(
        room_area,
        width,
        depth,
        target,
        _rotation_towards_point(target, ref_center),
    )

def _around_reference_candidates(
    room_area: tuple[float, float],
    width: float,
    depth: float,
    reference: tuple[list[float], float, float, float, float],
    *,
    side_gap: float,
    front_gap: float,
) -> list[tuple[list[float], float]]:
    ref_pos, ref_rot, ref_w, ref_d, _ = reference
    ref_center = (float(ref_pos[0]), float(ref_pos[1]))
    front_x = math.cos(ref_rot)
    front_y = math.sin(ref_rot)
    side_x = -front_y
    side_y = front_x
    targets = [
        (
            ref_center[0] + side_x * (ref_w / 2 + width / 2 + side_gap),
            ref_center[1] + side_y * (ref_w / 2 + width / 2 + side_gap),
        ),
        (
            ref_center[0] - side_x * (ref_w / 2 + width / 2 + side_gap),
            ref_center[1] - side_y * (ref_w / 2 + width / 2 + side_gap),
        ),
        (
            ref_center[0] + front_x * (ref_d / 2 + depth / 2 + front_gap),
            ref_center[1] + front_y * (ref_d / 2 + depth / 2 + front_gap),
        ),
        (
            ref_center[0] - front_x * (ref_d / 2 + depth / 2 + front_gap),
            ref_center[1] - front_y * (ref_d / 2 + depth / 2 + front_gap),
        ),
    ]
    candidates: list[tuple[list[float], float]] = []
    for target in targets:
        rotation = _rotation_towards_point(target, ref_center)
        candidates.extend(_centered_candidates(room_area, width, depth, target, rotation))
    return candidates

def _guided_candidates(
    uid: str,
    asset: dict[str, Any],
    room_area: tuple[float, float],
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    seed_plan: dict[str, Any],
) -> list[tuple[list[float], float]]:
    placement_mode = (seed_plan.get("placement_mode_by_uid") or {}).get(uid)
    if not placement_mode:
        return []

    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    if placement_mode == "beside_seat":
        return _beside_seat_candidates(asset, layout, asset_map)

    room_width, room_depth = room_area
    preferred_walls = (seed_plan.get("preferred_walls_by_uid") or {}).get(uid)
    if placement_mode == "desk_wall":
        return _wall_candidates(room_width, room_depth, width, depth, 0, preferred_walls)

    if placement_mode == "dining_table_anchor":
        room_center = (room_width / 2, room_depth / 2)
        candidates: list[tuple[list[float], float]] = []
        for rotation in (0.0, math.pi / 2):
            candidates.extend(_centered_candidates(room_area, width, depth, room_center, rotation))
        return candidates

    if placement_mode == "around_dining_table":
        table = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"dining_table_anchor"},
        )
        if table is None:
            return []
        return _around_reference_candidates(
            room_area,
            width,
            depth,
            table,
            side_gap=0.25,
            front_gap=0.25,
        )

    if placement_mode == "front_of_desk":
        desk = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"desk_wall"},
        )
        if desk is None:
            return []
        return _front_of_reference_candidates(
            room_area,
            width,
            depth,
            desk,
            gap=0.35,
        )

    anchor = _anchor_reference(layout, asset_map, seed_plan, uid)
    if anchor is None or placement_mode in _WALL_PLACEMENT_MODES | {"free"}:
        return []

    anchor_pos, anchor_rot, anchor_w, anchor_d, _ = anchor
    front_x = math.cos(anchor_rot)
    front_y = math.sin(anchor_rot)
    side_x = -front_y
    side_y = front_x
    anchor_center = (float(anchor_pos[0]), float(anchor_pos[1]))

    if placement_mode == "beside_bed_head":
        side_gap = anchor_w / 2 + width / 2 + 0.05
        targets = [(anchor_center[0] - front_x * (anchor_d / 2 - depth / 2) + sign * side_x * side_gap,
                    anchor_center[1] - front_y * (anchor_d / 2 - depth / 2) + sign * side_y * side_gap)
                   for sign in (-1, 1)]
        return [(list(target), anchor_rot) for target in targets]

    if placement_mode == "front_of_anchor":
        gap = 0.35
        target = (
            anchor_center[0] + front_x * (anchor_d / 2 + gap + depth / 2),
            anchor_center[1] + front_y * (anchor_d / 2 + gap + depth / 2),
        )
        return _centered_candidates(room_area, width, depth, target, anchor_rot)

    if placement_mode == "under_anchor":
        return _centered_candidates(room_area, width, depth, anchor_center, anchor_rot)

    if placement_mode == "beside_anchor":
        side_gap = anchor_w / 2 + width / 2 + 0.08
        targets = [
            (
                anchor_center[0] + side_x * side_gap,
                anchor_center[1] + side_y * side_gap,
            ),
            (
                anchor_center[0] - side_x * side_gap,
                anchor_center[1] - side_y * side_gap,
            ),
        ]
        candidates: list[tuple[list[float], float]] = []
        for target in targets:
            candidates.extend(_centered_candidates(room_area, width, depth, target, anchor_rot))
        return candidates

    if placement_mode == "around_anchor":
        center_surface = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"front_of_anchor"},
        )
        if center_surface is not None:
            return _around_reference_candidates(
                room_area,
                width,
                depth,
                center_surface,
                side_gap=0.45,
                front_gap=0.55,
            )
        side_gap = anchor_w / 2 + width / 2 + 0.35
        front_gap = anchor_d / 2 + depth / 2 + 0.7
        targets = [
            (
                anchor_center[0] + side_x * side_gap,
                anchor_center[1] + side_y * side_gap,
            ),
            (
                anchor_center[0] - side_x * side_gap,
                anchor_center[1] - side_y * side_gap,
            ),
            (
                anchor_center[0] + front_x * front_gap + side_x * max(width * 0.45, 0.3),
                anchor_center[1] + front_y * front_gap + side_y * max(width * 0.45, 0.3),
            ),
            (
                anchor_center[0] + front_x * front_gap - side_x * max(width * 0.45, 0.3),
                anchor_center[1] + front_y * front_gap - side_y * max(width * 0.45, 0.3),
            ),
        ]
        candidates: list[tuple[list[float], float]] = []
        for target in targets:
            rotation = _rotation_towards_point(target, anchor_center)
            candidates.extend(_centered_candidates(room_area, width, depth, target, rotation))
        return candidates

    return []

def _dedupe_candidates(
    candidates: list[tuple[list[float], float]],
) -> list[tuple[list[float], float]]:
    deduped: list[tuple[list[float], float]] = []
    seen: set[tuple[float, float, float]] = set()
    for center, rotation in candidates:
        key = (
            round(float(center[0]), 4),
            round(float(center[1]), 4),
            round(normalize_rotation(float(rotation)), 4),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(([float(center[0]), float(center[1])], float(rotation)))
    return deduped

def _placement_candidates(
    asset: dict[str, Any],
    room_area: tuple[float, float],
    wall_offset: int,
    preferred_walls: list[str] | None = None,
    placement_mode: str | None = None,
) -> list[tuple[list[float], float]]:
    room_width, room_depth = room_area
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    uid = asset.get("uid", "")
    category = asset.get("category", "")

    if (
        placement_mode in _WALL_PLACEMENT_MODES
        or is_wall_aligned_asset(category, uid)
        or is_wall_mounted_asset(category, uid)
    ):
        return _wall_candidates(room_width, room_depth, width, depth, wall_offset, preferred_walls)
    return _grid_candidates(room_width, room_depth, width, depth)

def _dynamic_preferred_walls(
    uid: str,
    placement_mode: str | None,
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    seed_plan: dict[str, Any],
) -> list[str] | None:
    preferred = list((seed_plan.get("preferred_walls_by_uid") or {}).get(uid) or [])
    if placement_mode != "focal_wall":
        return preferred or None

    anchor = _anchor_reference(layout, asset_map, seed_plan, uid)
    if anchor is None:
        return preferred or None
    actual_anchor_wall = _wall_from_rotation(anchor[1])
    focal_wall = _opposite_wall_name(actual_anchor_wall)
    if not focal_wall:
        return preferred or None
    return [focal_wall, *[wall for wall in preferred if wall != focal_wall]]
