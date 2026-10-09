"""Deterministic layout geometry placement, ported from core/geometry/placement.py."""

from __future__ import annotations
import math
from typing import Any
from shapely.geometry import Point, Polygon
from app.rules.geometry.candidates import (
    _anchor_reference,
    _local_to_world,
    _placed_reference_by_mode,
    _seating_references,
    _support_surface_reference,
)
from app.rules.pipeline_shared import support_top_z
from app.rules.geometry.primitives import _SEED_MARGIN, _TIGHT_CLEARANCE, asset_polygon

def _placement_target(
    *,
    placement_mode: str | None,
    width: float,
    depth: float,
    room_area: tuple[float, float],
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    seed_plan: dict[str, Any],
) -> tuple[float, float] | None:
    room_width, room_depth = room_area
    if placement_mode == "dining_table_anchor":
        return room_width / 2, room_depth / 2

    if placement_mode == "around_dining_table":
        table = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"dining_table_anchor"},
        )
        return (float(table[0][0]), float(table[0][1])) if table is not None else None

    if placement_mode == "front_of_desk":
        desk = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"desk_wall"},
        )
        if desk is None:
            return None
        desk_pos, desk_rot, _, desk_d, _ = desk
        return (
            float(desk_pos[0]) + math.cos(desk_rot) * (desk_d / 2 + 0.35 + depth / 2),
            float(desk_pos[1]) + math.sin(desk_rot) * (desk_d / 2 + 0.35 + depth / 2),
        )

    if placement_mode == "beside_seat":
        seats = _seating_references(layout, asset_map)
        if seats:
            return float(seats[0][0][0]), float(seats[0][0][1])
        return None

    anchor = _anchor_reference(layout, asset_map, seed_plan)
    if anchor is None:
        return None

    anchor_pos, anchor_rot, anchor_w, anchor_d, _ = anchor
    anchor_center = (float(anchor_pos[0]), float(anchor_pos[1]))
    front_x = math.cos(anchor_rot)
    front_y = math.sin(anchor_rot)
    side_x = -front_y
    side_y = front_x

    if placement_mode == "front_of_anchor":
        return (
            anchor_center[0] + front_x * (anchor_d / 2 + 0.35 + depth / 2),
            anchor_center[1] + front_y * (anchor_d / 2 + 0.35 + depth / 2),
        )
    if placement_mode == "under_anchor":
        return anchor_center
    if placement_mode == "around_anchor":
        center_surface = _placed_reference_by_mode(
            layout,
            asset_map,
            seed_plan,
            {"front_of_anchor"},
        )
        if center_surface is not None:
            return float(center_surface[0][0]), float(center_surface[0][1])
        return anchor_center
    if placement_mode == "beside_anchor":
        return (
            anchor_center[0] + side_x * (anchor_w / 2 + width / 2 + 0.08),
            anchor_center[1] + side_y * (anchor_w / 2 + width / 2 + 0.08),
        )
    return None

def _slot_available(
    point: tuple[float, float],
    *,
    room_cover: Polygon,
    blockers: list[Polygon],
    occupied: list[Polygon],
) -> bool:
    probe = Point(point)
    if not room_cover.covers(probe):
        return False
    return not any(other.covers(probe) for other in blockers + occupied)

def _future_slot_penalty(
    *,
    center: list[float],
    rotation_z: float,
    width: float,
    depth: float,
    placement_mode: str | None,
    room_cover: Polygon,
    blockers: list[Polygon],
    occupied: list[Polygon],
) -> float:
    front_x = math.cos(rotation_z)
    front_y = math.sin(rotation_z)
    side_x = -front_y
    side_y = front_x

    if placement_mode == "anchor_wall":
        slot_offsets = (
            (front_x * (depth / 2 + 0.85), front_y * (depth / 2 + 0.85)),
            (side_x * (width / 2 + 0.55), side_y * (width / 2 + 0.55)),
            (-side_x * (width / 2 + 0.55), -side_y * (width / 2 + 0.55)),
        )
    elif placement_mode == "dining_table_anchor":
        slot_offsets = (
            (front_x * (depth / 2 + 0.65), front_y * (depth / 2 + 0.65)),
            (-front_x * (depth / 2 + 0.65), -front_y * (depth / 2 + 0.65)),
            (side_x * (width / 2 + 0.65), side_y * (width / 2 + 0.65)),
            (-side_x * (width / 2 + 0.65), -side_y * (width / 2 + 0.65)),
        )
    elif placement_mode == "desk_wall":
        slot_offsets = (
            (front_x * (depth / 2 + 0.7), front_y * (depth / 2 + 0.7)),
        )
    elif placement_mode == "focal_wall":
        slot_offsets = (
            (0.0, 0.0),
        )
    else:
        return 0.0

    penalty = 0.0
    for dx, dy in slot_offsets:
        if not _slot_available(
            (float(center[0]) + dx, float(center[1]) + dy),
            room_cover=room_cover,
            blockers=blockers,
            occupied=occupied,
        ):
            penalty += 2.0
    return penalty

def _candidate_score(
    *,
    center: list[float],
    rotation_z: float,
    poly: Polygon,
    candidate_index: int,
    status: str,
    clearance: float | None,
    target: tuple[float, float] | None,
    room_area: tuple[float, float],
    placement_mode: str | None,
    width: float,
    depth: float,
    rug_like: bool,
    room_cover: Polygon,
    blockers: list[Polygon],
    occupied: list[Polygon],
) -> float:
    score = candidate_index * 0.01
    if target is not None:
        score += math.hypot(float(center[0]) - target[0], float(center[1]) - target[1]) * 4.0
    else:
        room_center = (room_area[0] / 2, room_area[1] / 2)
        score += math.hypot(float(center[0]) - room_center[0], float(center[1]) - room_center[1]) * 0.35

    if status == "placed_tightly":
        score += 1.0
    if clearance is not None and clearance < 0.35:
        score += (0.35 - clearance) * 3.0

    if blockers:
        blocker_clearance = min(float(poly.distance(other)) for other in blockers)
        if blocker_clearance < 0.55:
            score += (0.55 - blocker_clearance) * 4.0

    if occupied and not rug_like:
        asset_clearance = min(float(poly.distance(other)) for other in occupied)
        if asset_clearance < 0.25:
            score += (0.25 - asset_clearance) * 2.0

    score += _future_slot_penalty(
        center=center,
        rotation_z=rotation_z,
        width=width,
        depth=depth,
        placement_mode=placement_mode,
        room_cover=room_cover,
        blockers=blockers,
        occupied=[] if rug_like else occupied,
    )
    return score

def _placement_outcome(
    *,
    uid: str,
    asset: dict[str, Any],
    status: str,
    reason: str | None = None,
    blocker_overlap: float = 0.0,
    asset_overlap: float = 0.0,
    clearance: float | None = None,
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "uid": uid,
        "category": asset.get("category", ""),
        "status": status,
    }
    if reason:
        outcome["reason"] = reason
    if blocker_overlap > 1e-6:
        outcome["blocker_overlap_sqm"] = round(float(blocker_overlap), 4)
    if asset_overlap > 1e-6:
        outcome["asset_overlap_sqm"] = round(float(asset_overlap), 4)
    if clearance is not None:
        outcome["clearance_m"] = round(float(clearance), 3)
    return outcome

def _placement_status(
    poly: Polygon,
    *,
    occupied: list[Polygon],
    blockers: list[Polygon],
    rug_like: bool,
) -> tuple[str, float | None]:
    clearances = [float(poly.distance(other)) for other in blockers]
    if not rug_like:
        clearances.extend(float(poly.distance(other)) for other in occupied)
    if not clearances:
        return "placed_comfortably", None
    clearance = min(clearances)
    if clearance < _TIGHT_CLEARANCE:
        return "placed_tightly", clearance
    return "placed_comfortably", clearance

def _skip_reason(blocker_overlap: float, asset_overlap: float) -> str:
    if blocker_overlap > 1e-6 and blocker_overlap >= asset_overlap:
        return "blocks_opening_or_circulation"
    if asset_overlap > 1e-6:
        return "overlaps_another_asset"
    return "no_comfortable_position"

def _place_on_support_surface(
    uid: str,
    asset: dict[str, Any],
    layout: dict[str, Any],
    asset_map: dict[str, dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> bool:
    """Place `asset` on a compatible support surface; True when handled."""
    support, skip_reason = _support_surface_reference(uid, asset, layout, asset_map)
    if not support:
        outcomes.append(
            _placement_outcome(
                uid=uid,
                asset=asset,
                status=f"skipped_{skip_reason}",
                reason=skip_reason,
            )
        )
        return True
    parent_uid, parent_asset, parent_pos, parent_rot_z, _, _, _, offset = support
    world_x, world_y = _local_to_world(parent_pos, parent_rot_z, offset)
    layout[uid] = {
        "category": asset.get("category", ""),
        "position": [
            world_x,
            world_y,
            float(support_top_z(parent_asset, parent_pos[2])),
        ],
        "rotation": [0.0, 0.0, float(parent_rot_z)],
        "on_top_of": parent_uid,
    }
    outcomes.append(
        _placement_outcome(
            uid=uid,
            asset=asset,
            status="placed_comfortably",
            reason="on_support_surface",
        )
    )
    return True

def _rug_fits_room_bounds(asset: dict[str, Any], room_cover: Polygon) -> bool:
    min_x, min_y, max_x, max_y = room_cover.bounds
    available_width = max(0.0, (max_x - min_x) - 2 * _SEED_MARGIN)
    available_depth = max(0.0, (max_y - min_y) - 2 * _SEED_MARGIN)
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    return (width <= available_width and depth <= available_depth) or (
        depth <= available_width and width <= available_depth
    )

def _search_placement(
    *,
    search_candidates: list[tuple[list[float], float]],
    width: float,
    depth: float,
    z: float,
    rug_like: bool,
    room_cover: Polygon,
    room_area: tuple[float, float],
    occupied: list[Polygon],
    blockers: list[Polygon],
    target: tuple[float, float] | None = None,
    placement_mode: str | None = None,
) -> tuple[
    tuple[list[float], float, Polygon, str, float | None] | None,
    tuple[float, float],
]:
    """Scored-fit search over candidates.

    Returns (placement, best_fallback_overlaps); placement is
    (center, rotation_z, poly, status, clearance) or None when nothing fits.
    """
    best_fallback = None
    best_fallback_score = None
    best_valid = None
    best_valid_score = None
    for candidate_index, (center, rotation_z) in enumerate(search_candidates):
        poly = asset_polygon([center[0], center[1], z], rotation_z, width, depth)
        if not room_cover.covers(poly):
            continue
        blocker_overlap = sum(poly.intersection(other).area for other in blockers)
        asset_overlap = (
            0.0
            if rug_like
            else sum(poly.intersection(other).area for other in occupied)
        )
        if blocker_overlap <= 1e-6 and asset_overlap <= 1e-6:
            status, clearance = _placement_status(
                poly,
                occupied=occupied,
                blockers=blockers,
                rug_like=rug_like,
            )
            score = _candidate_score(
                center=center,
                rotation_z=rotation_z,
                poly=poly,
                candidate_index=candidate_index,
                status=status,
                clearance=clearance,
                target=target,
                room_area=room_area,
                placement_mode=placement_mode,
                width=width,
                depth=depth,
                rug_like=rug_like,
                room_cover=room_cover,
                blockers=blockers,
                occupied=occupied,
            )
            if best_valid_score is None or score < best_valid_score:
                best_valid_score = score
                best_valid = (center, rotation_z, poly, status, clearance)
            continue

        score = blocker_overlap + asset_overlap
        if best_fallback_score is None or score < best_fallback_score:
            best_fallback_score = score
            best_fallback = (blocker_overlap, asset_overlap)
    if best_valid is not None:
        return best_valid, (0.0, 0.0)
    return None, (best_fallback if best_fallback is not None else (0.0, 0.0))
