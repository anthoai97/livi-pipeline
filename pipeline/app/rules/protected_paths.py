"""Shared protected circulation path geometry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shapely.geometry import Polygon

PROTECTED_PATH_WIDTH_M = 0.91


def intent_requires_protected_path(intent_packet: Any) -> bool:
    return isinstance(intent_packet, Mapping) and bool(
        intent_packet.get("requires_straight_circulation_path")
    )


def build_protected_paths(
    room_area: tuple[float, float],
    room_doors: list[dict[str, Any]] | None,
    *,
    path_width: float = PROTECTED_PATH_WIDTH_M,
) -> list[dict[str, Any]]:
    """Build straight protected paths between opposing door walls."""
    room_width, room_depth = float(room_area[0]), float(room_area[1])
    by_wall: dict[str, list[dict[str, Any]]] = {}
    for door in room_doors or []:
        wall = str(door.get("wall") or "").strip().lower()
        if wall in {"left", "right", "bottom", "top"}:
            by_wall.setdefault(wall, []).append(door)

    paths: list[dict[str, Any]] = []
    half = float(path_width) / 2.0
    if by_wall.get("bottom") and by_wall.get("top"):
        bottom = by_wall["bottom"][0]
        top = by_wall["top"][0]
        center_x = (
            float((bottom.get("center") or [room_width / 2.0, 0.0])[0])
            + float((top.get("center") or [room_width / 2.0, room_depth])[0])
        ) / 2.0
        min_x = max(0.0, center_x - half)
        max_x = min(room_width, center_x + half)
        paths.append(
            {
                "id": "protected_path_bottom_top",
                "kind": "protected_path",
                "reason": "opposing_door_path",
                "axis": "y",
                "wall_pair": ["bottom", "top"],
                "center": [(min_x + max_x) / 2.0, room_depth / 2.0],
                "width": max_x - min_x,
                "depth": room_depth,
                "required_width": path_width,
            }
        )

    if by_wall.get("left") and by_wall.get("right"):
        left = by_wall["left"][0]
        right = by_wall["right"][0]
        center_y = (
            float((left.get("center") or [0.0, room_depth / 2.0])[1])
            + float((right.get("center") or [room_width, room_depth / 2.0])[1])
        ) / 2.0
        min_y = max(0.0, center_y - half)
        max_y = min(room_depth, center_y + half)
        paths.append(
            {
                "id": "protected_path_left_right",
                "kind": "protected_path",
                "reason": "opposing_door_path",
                "axis": "x",
                "wall_pair": ["left", "right"],
                "center": [room_width / 2.0, (min_y + max_y) / 2.0],
                "width": room_width,
                "depth": max_y - min_y,
                "required_width": path_width,
            }
        )

    return paths


def protected_paths_for_intent(
    room_area: tuple[float, float],
    room_doors: list[dict[str, Any]] | None,
    intent_packet: Any,
) -> list[dict[str, Any]]:
    if not intent_requires_protected_path(intent_packet):
        return []
    return build_protected_paths(room_area, room_doors)


def protected_path_polygon(path: dict[str, Any]) -> Polygon:
    center = path.get("center") or [0.0, 0.0]
    width = float(path.get("width") or 0.0)
    depth = float(path.get("depth") or 0.0)
    x, y = float(center[0]), float(center[1])
    half_w, half_d = width / 2.0, depth / 2.0
    return Polygon(
        [
            (x - half_w, y - half_d),
            (x + half_w, y - half_d),
            (x + half_w, y + half_d),
            (x - half_w, y + half_d),
        ]
    )


def protected_path_polygons(paths: list[dict[str, Any]]) -> list[Polygon]:
    return [protected_path_polygon(path) for path in paths]
