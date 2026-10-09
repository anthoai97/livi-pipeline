"""Deterministic layout geometry primitives."""

from __future__ import annotations
import math
from typing import Any
from shapely.geometry import Polygon
from app.rules.pipeline_shared import door_clearance_rect

_SEED_MARGIN = 0.05

_SEED_WALL_FRACTIONS = (0.5, 0.33, 0.67, 0.2, 0.8)

_FLOOR_LAMP_SEAT_GAP = 0.35

_TIGHT_CLEARANCE = 0.12

_SUPPORT_INSET_M = 0.04
_SUPPORT_TOLERANCE_M = 0.01
_DIMENSION_EPSILON_M = 1e-9

_MEDIA_DISPLAY_ROLE_KEYWORDS = ("tv", "television", "monitor")

_MEDIA_SUPPORT_ROLE_KEYWORDS = ("tv_stand", "media_unit", "media_console", "console")


def service_center_bounds(
    room: Polygon, footprint: Polygon, service_region: Polygon, center: list[float],
) -> dict[str, list[float]]:
    """Center intervals keeping a footprint/service band within the room bbox.

    This is boundary evidence, not a placement search or clearance certification.
    Round inward so the reported interval does not reintroduce a small overrun.
    """
    service_bounds = footprint.union(service_region).bounds
    room_bounds = room.bounds
    return {
        axis: [math.ceil((room_bounds[index] - service_bounds[index] + center[index]) * 10000) / 10000,
               math.floor((room_bounds[index + 2] - service_bounds[index + 2] + center[index]) * 10000) / 10000]
        for index, axis in enumerate(("x", "y"))
    }


def media_display_fits_support_dimensions(
    display_width: float,
    display_depth: float,
    support_width: float,
    support_depth: float,
) -> bool:
    """Use the same physical TV/support rule in selection and seed placement."""
    width_limit = support_width - 2 * _SUPPORT_INSET_M + _SUPPORT_TOLERANCE_M
    depth_limit = max(
        0.08,
        support_depth - 2 * _SUPPORT_INSET_M + _SUPPORT_TOLERANCE_M,
    )
    return (
        display_width <= width_limit + _DIMENSION_EPSILON_M
        and display_depth <= depth_limit + _DIMENSION_EPSILON_M
    )


_WALL_PLACEMENT_MODES = {"anchor_wall", "focal_wall", "support_wall", "wall", "desk_wall"}


def normalize_rotation(rotation_z: float) -> float:
    two_pi = 2 * math.pi
    normalized = rotation_z % two_pi
    return 0.0 if math.isclose(normalized, two_pi, abs_tol=1e-6) else normalized

def snap_rotation(rotation_z: float, step: float) -> float:
    snapped = round(rotation_z / step) * step
    return normalize_rotation(snapped)

def yaw_to_object_rotation(rotation_z: float) -> float:
    """Convert stored yaw (0=+X) to the asset/image rotation used by geometry."""
    return normalize_rotation(rotation_z + math.pi / 2)

def is_rug(uid: str, asset: dict[str, Any]) -> bool:
    return "rug" in uid.lower() or "rug" in str(asset.get("category", "")).lower()

def extract_placement(
    placement: dict[str, Any],
    asset: dict[str, Any],
) -> tuple[list[float], float, float, float, float]:
    pos = list(placement.get("position", [0.0, 0.0, 0.0]))
    if len(pos) < 3:
        pos += [0.0] * (3 - len(pos))
    rot = placement.get("rotation", [0.0, 0.0, 0.0])
    rot_z = float(rot[2]) if len(rot) > 2 else 0.0
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    height = float(asset.get("height", 0.5) or 0.5)
    return pos, rot_z, width, depth, height

def asset_polygon(
    pos: list[float],
    rot_z: float,
    width: float,
    depth: float,
) -> Polygon:
    object_rotation = yaw_to_object_rotation(rot_z)
    cos_a = math.cos(object_rotation)
    sin_a = math.sin(object_rotation)
    corners = [
        (-width / 2, -depth / 2),
        (width / 2, -depth / 2),
        (width / 2, depth / 2),
        (-width / 2, depth / 2),
    ]
    return Polygon(
        [
            (
                pos[0] + dx * cos_a - dy * sin_a,
                pos[1] + dx * sin_a + dy * cos_a,
            )
            for dx, dy in corners
        ]
    )

def frange(start: float, stop: float, step: float) -> list[float]:
    if stop <= start:
        return [round((start + stop) / 2, 4)]
    values = []
    current = start
    while current <= stop + 1e-6:
        values.append(round(current, 4))
        current += step
    return values

def projected_half_extents(
    width: float,
    depth: float,
    rotation_z: float,
) -> tuple[float, float]:
    object_rotation = yaw_to_object_rotation(rotation_z)
    return (
        (
            abs(width * math.cos(object_rotation))
            + abs(depth * math.sin(object_rotation))
        )
        / 2,
        (
            abs(width * math.sin(object_rotation))
            + abs(depth * math.cos(object_rotation))
        )
        / 2,
    )

def room_polygon(
    room_area: tuple[float, float],
    room_vertices: list[list[float]] | None,
) -> Polygon:
    room_width, room_depth = room_area
    if room_vertices and len(room_vertices) >= 3:
        vertices = [(float(x), float(y)) for x, y in room_vertices]
    else:
        vertices = [
            (0.0, 0.0),
            (room_width, 0.0),
            (room_width, room_depth),
            (0.0, room_depth),
        ]
    return Polygon(vertices)

def rect_polygon(center: list[float], width: float, depth: float) -> Polygon:
    cx, cy = float(center[0]), float(center[1])
    return Polygon(
        [
            (cx - width / 2, cy - depth / 2),
            (cx + width / 2, cy - depth / 2),
            (cx + width / 2, cy + depth / 2),
            (cx - width / 2, cy + depth / 2),
        ]
    )

def polygon_from_rect(rect: dict[str, Any]) -> Polygon:
    return rect_polygon(rect["center"], rect["width"], rect["depth"])

def _blocker_polygons(
    room_doors: list[dict[str, Any]] | None,
    room_windows: list[dict[str, Any]] | None,
    *,
    boundary: list[list[float]] | None,
    room_area: tuple[float, float],
    allow_window_overlap: bool = False,
) -> list[Polygon]:
    blockers = [
        polygon_from_rect(
            door_clearance_rect(door, boundary=boundary, room_area=room_area)
        )
        for door in room_doors or []
    ]
    if allow_window_overlap:
        return blockers
    for window in room_windows or []:
        center = window.get("center") or [0.0, 0.0]
        width = float(window.get("width", 1.0) or 1.0)
        depth = float(window.get("depth", 0.1) or 0.1)
        blockers.append(rect_polygon(center, width, depth))
    return blockers
