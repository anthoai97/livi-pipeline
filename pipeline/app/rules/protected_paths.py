"""Shared protected circulation path geometry."""

from __future__ import annotations

import heapq
from itertools import combinations
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points

from app.rules.door_geometry import door_opening_rect, room_bounds

PROTECTED_PATH_WIDTH_M = 0.91
FALLBACK_PATH_WIDTH_M = 0.6  # the builder's shortest wall; tried when no 0.91 m route exists


def build_protected_paths(
    room_vertices: list[list[float]],
    room_doors: list[dict[str, Any]] | None,
    *,
    path_width: float = PROTECTED_PATH_WIDTH_M,
) -> list[dict[str, Any]]:
    """Clear routes between every pair of doors whose walls face each other, inside the room polygon.

    A straight axis-aligned band between the doors keeps the axis record (`axis` "x" or "y"); otherwise the
    shortest route through the polygon eroded by half the width is buffered to the width and stored with
    `axis` "routed", its `centerline` and `polygon`. A pair with no route at the width, nor at
    FALLBACK_PATH_WIDTH_M, gets none.
    """
    polygon = orient(Polygon(room_vertices), 1.0)
    openings = [door_opening_rect(door, boundary=room_vertices) for door in room_doors or []]
    paths: list[dict[str, Any]] = []
    for (i, first), (j, second) in combinations(enumerate(openings), 2):
        (ax, ay), (bx, by) = first["inward_normal"], second["inward_normal"]
        if ax * bx + ay * by >= -0.5:
            continue
        record = _straight_path(first, second, polygon, path_width) or next(
            (route for width in (path_width, FALLBACK_PATH_WIDTH_M) if (route := _routed_path(first, second, polygon, width))),
            None,
        )
        if record is None:
            continue
        paths.append(
            {
                "id": f"protected_path_door_{i}_{j}",
                "kind": "protected_path",
                "reason": "opposing_door_path",
                "wall_pair": [first["wall"], second["wall"]],
                "wall_labels": [first["wall_label"], second["wall_label"]],
                **record,
            }
        )
    return paths


def _straight_path(
    first: dict[str, Any],
    second: dict[str, Any],
    polygon: Polygon,
    width: float,
) -> dict[str, Any] | None:
    """The axis-aligned band of ``width`` centred on the doors' mean along-coordinate, from one door's wall to the
    other's, when both doors are on axis walls and the band lies inside the room."""
    if not first["facing"] or not second["facing"]:
        return None
    min_x, min_y, max_x, max_y = room_bounds(boundary=list(polygon.exterior.coords))
    (x0, y0), (x1, y1) = first["opening_center"], second["opening_center"]
    half = width / 2
    if first["facing"] in ("left", "right"):
        center_y = (y0 + y1) / 2
        lo, hi = max(min_y, center_y - half), min(max_y, center_y + half)
        band = {"axis": "x", "center": [(x0 + x1) / 2, (lo + hi) / 2], "width": abs(x1 - x0), "depth": hi - lo}
    else:
        center_x = (x0 + x1) / 2
        lo, hi = max(min_x, center_x - half), min(max_x, center_x + half)
        band = {"axis": "y", "center": [(lo + hi) / 2, (y0 + y1) / 2], "width": hi - lo, "depth": abs(y1 - y0)}
    if not polygon.buffer(1e-6).covers(protected_path_polygon(band)):
        return None
    return {**band, "required_width": width}


def _routed_path(
    first: dict[str, Any],
    second: dict[str, Any],
    polygon: Polygon,
    width: float,
) -> dict[str, Any] | None:
    free = polygon.buffer(-width / 2, join_style=2)
    # A door near a corner has its inward point outside the eroded room; the route starts at the nearest free point.
    anchors = [
        nearest_points(free, Point(opening["opening_center"][0] + opening["inward_normal"][0] * width / 2,
                                   opening["opening_center"][1] + opening["inward_normal"][1] * width / 2))[0]
        for opening in (first, second)
    ]
    route = _shortest_inside_route(free.buffer(1e-7), *anchors)
    if route is None:
        return None
    centerline = [list(first["opening_center"]), *([x, y] for x, y in route), list(second["opening_center"])]
    passage = LineString(centerline).buffer(width / 2, cap_style=2, join_style=1).intersection(polygon)
    if passage.geom_type != "Polygon":
        passage = max(passage.geoms, key=lambda part: part.area) if not passage.is_empty else None
    if passage is None or passage.is_empty:
        return None
    x0, y0, x1, y1 = passage.bounds
    return {
        "axis": "routed",
        "center": [(x0 + x1) / 2, (y0 + y1) / 2],
        "width": x1 - x0,
        "depth": y1 - y0,
        "required_width": width,
        "centerline": centerline,
        "polygon": [[x, y] for x, y in list(passage.exterior.coords)[:-1]],
    }


def _shortest_inside_route(free_space: Any, start: Point, end: Point) -> list[tuple[float, float]] | None:
    """Visibility-graph route inside one connected polygon; None means no route."""
    components = list(free_space.geoms) if hasattr(free_space, "geoms") else [free_space]
    component = next((part for part in components if part.covers(start) and part.covers(end)), None)
    if component is None:
        return None
    points = [tuple(start.coords)[0], tuple(end.coords)[0], *list(component.exterior.coords)[:-1]]
    for ring in component.interiors:
        points.extend(list(ring.coords)[:-1])
    # Dijkstra over visible boundary vertices gives an exact polygon route.
    distances = {0: 0.0}
    previous: dict[int, int] = {}
    queue = [(0.0, 0)]
    cover = component.buffer(1e-8)
    while queue:
        distance, index = heapq.heappop(queue)
        if distance != distances[index]:
            continue
        if index == 1:
            route = [points[index]]
            while index in previous:
                index = previous[index]
                route.append(points[index])
            return list(reversed(route))
        for other, point in enumerate(points):
            if other == index:
                continue
            line = LineString([points[index], point])
            candidate = distance + line.length
            if candidate < distances.get(other, float("inf")) and cover.covers(line):
                distances[other] = candidate
                previous[other] = index
                heapq.heappush(queue, (candidate, other))
    return None


def protected_path_polygon(path: dict[str, Any]) -> Polygon:
    if path.get("polygon"):
        return Polygon(path["polygon"])
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
