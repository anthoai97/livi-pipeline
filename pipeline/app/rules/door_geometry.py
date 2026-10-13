"""Room-bounds, room-wall and door-opening geometry helpers."""

import math
from typing import Any, NamedTuple

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

DEFAULT_DOOR_WIDTH = 0.9   # meters – used when door geometry is missing
DEFAULT_DOOR_THICKNESS = 0.08  # meters – thin opening marker along the wall
DIAGONAL = math.sin(math.pi / 8)  # a wall more than 22.5 deg off an axis is a corner wall (wallNames.ts)
_TIER_M = 0.01  # walls within this of the outermost one share its name
_CARDINAL_NORMALS = {"left": (1.0, 0.0), "right": (-1.0, 0.0), "bottom": (0.0, 1.0), "top": (0.0, -1.0)}


def clamp(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        return (lower + upper) / 2
    return max(lower, min(upper, value))


def room_bounds(
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    if boundary and len(boundary) >= 3:
        xs = [float(point[0]) for point in boundary]
        ys = [float(point[1]) for point in boundary]
        return min(xs), min(ys), max(xs), max(ys)
    if room_area is not None:
        room_width, room_depth = room_area
        return 0.0, 0.0, float(room_width), float(room_depth)
    return 0.0, 0.0, 0.0, 0.0


class RoomWall(NamedTuple):
    """One edge of the room outline, in ring order of the counter-clockwise polygon."""

    index: int
    start: tuple[float, float]
    end: tuple[float, float]
    length: float
    normal: tuple[float, float]  # inward unit vector
    label: str  # the builder's name: "Back", "Back (inner)", "Right (front)", "Back-right"
    on_box: bool  # the edge lies on the bounding box

    @property
    def facing(self) -> str | None:
        """The side of the room this wall bounds ("left" faces +X inward); None for a corner wall."""
        nx, ny = self.normal
        if abs(nx) > DIAGONAL and abs(ny) > DIAGONAL:
            return None
        if abs(nx) >= abs(ny):
            return "left" if nx > 0 else "right"
        return "bottom" if ny > 0 else "top"

    @property
    def vertical(self) -> bool:
        return self.facing in ("left", "right")

    @property
    def direction(self) -> tuple[float, float]:
        """Unit vector from start to end."""
        return (self.end[0] - self.start[0]) / self.length, (self.end[1] - self.start[1]) / self.length

    def along(self, x: float, y: float) -> float:
        """Distance of a point's projection from the wall's start, along the wall."""
        ux, uy = self.direction
        return (x - self.start[0]) * ux + (y - self.start[1]) * uy

    def point_at(self, along: float) -> tuple[float, float]:
        ux, uy = self.direction
        return self.start[0] + ux * along, self.start[1] + uy * along

    def span(self) -> tuple[float, float]:
        """Extent along the wall: y for left/right walls, x for bottom/top."""
        axis = 1 if self.vertical else 0
        return tuple(sorted((self.start[axis], self.end[axis])))

    def line_at(self, along: float) -> float:
        """Wall coordinate across its normal (x for left/right) at ``along``."""
        a, c = (1, 0) if self.vertical else (0, 1)
        a0, c0, a1, c1 = self.start[a], self.start[c], self.end[a], self.end[c]
        return c0 + (c1 - c0) * (along - a0) / (a1 - a0)

    def distance(self, x: float, y: float) -> float:
        px, py = self.point_at(clamp(self.along(x, y), 0.0, self.length))
        return math.hypot(x - px, y - py)


def _wall_labels(normals: list[tuple[float, float]], mids: list[tuple[float, float]]) -> list[str]:
    """The builder's wall names (wallNames.ts): the facing word, "(inner)" for a wall set back from the
    outermost one facing the same way, "(front)"/"(back)" or "(left)"/"(right)" for two walls on one line,
    "Back-right" for a corner wall."""
    walls = []
    for (ix, iy), mid in zip(normals, mids):
        nx, ny = -ix, -iy  # outward
        if abs(nx) > DIAGONAL and abs(ny) > DIAGONAL:
            walls.append({"base": f"{'Back' if ny > 0 else 'Front'}-{'right' if nx > 0 else 'left'}", "out": 0.0, "along": 0.0, "axis": False})
        elif abs(nx) >= abs(ny):
            walls.append({"base": "Right" if nx > 0 else "Left", "out": mid[0] if nx > 0 else -mid[0], "along": mid[1], "axis": True})
        else:
            walls.append({"base": "Back" if ny > 0 else "Front", "out": mid[1] if ny > 0 else -mid[1], "along": mid[0], "axis": True})
    names = [wall["base"] for wall in walls]

    def pair(base: str, a: int, b: int, prefix: str) -> None:
        vertical = base in ("Right", "Left")
        lo, hi = (a, b) if walls[a]["along"] <= walls[b]["along"] else (b, a)
        names[lo] = f"{base} ({prefix}{'front' if vertical else 'left'})"
        names[hi] = f"{base} ({prefix}{'back' if vertical else 'right'})"

    groups: dict[str, list[int]] = {}
    for index, wall in enumerate(walls):
        groups.setdefault(wall["base"], []).append(index)
    for base, idx in groups.items():
        if len(idx) < 2:
            continue
        if not walls[idx[0]]["axis"]:
            for k, i in enumerate(idx[1:], 2):
                names[i] = f"{base} {k}"
            continue
        outer = max(walls[i]["out"] for i in idx)
        outer_idx = [i for i in idx if walls[i]["out"] >= outer - _TIER_M]
        inner = sorted((i for i in idx if walls[i]["out"] < outer - _TIER_M), key=lambda i: -walls[i]["out"])
        if len(outer_idx) == 2:
            pair(base, outer_idx[0], outer_idx[1], "")
        else:
            for k, i in enumerate(outer_idx[1:], 2):
                names[i] = f"{base} {k}"
        if len(inner) == 2 and abs(walls[inner[0]]["out"] - walls[inner[1]]["out"]) < _TIER_M:
            pair(base, inner[0], inner[1], "inner ")
        else:
            for k, i in enumerate(inner, 1):
                names[i] = f"{base} (inner)" if len(inner) == 1 else f"{base} (inner {k})"
    return names


def room_walls(boundary: list[list[float]]) -> list[RoomWall]:
    """Every edge of the room polygon, in ring order, with its inward normal and the builder's label."""
    ring = orient(Polygon(boundary), 1.0).exterior.coords
    min_x, min_y, max_x, max_y = room_bounds(boundary=boundary)
    edges = []
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-9:
            continue
        # The ring runs counter-clockwise, so the room lies left of each edge.
        edges.append(((x0, y0), (x1, y1), length, ((y0 - y1) / length, (x1 - x0) / length)))
    labels = _wall_labels([edge[3] for edge in edges], [((s[0] + e[0]) / 2, (s[1] + e[1]) / 2) for s, e, _, _ in edges])
    walls = []
    for index, ((start, end, length, normal), label) in enumerate(zip(edges, labels)):
        on_box = any(abs(start[axis] - side) < 1e-9 and abs(end[axis] - side) < 1e-9
                     for axis, side in ((0, min_x), (0, max_x), (1, min_y), (1, max_y)))
        walls.append(RoomWall(index, start, end, length, normal, label, on_box))
    return walls


def nearest_wall(
    point: list[float] | tuple[float, float],
    boundary: list[list[float]],
) -> RoomWall:
    x, y = float(point[0]), float(point[1])
    return min(room_walls(boundary), key=lambda wall: wall.distance(x, y))


def nearest_axis_wall(
    point: list[float] | tuple[float, float],
    boundary: list[list[float]],
) -> RoomWall:
    """The nearest wall that faces a cardinal direction: placement stays on axis walls until phase 3 of #53."""
    x, y = float(point[0]), float(point[1])
    walls = room_walls(boundary)
    return min([wall for wall in walls if wall.facing] or walls, key=lambda wall: wall.distance(x, y))


def _opening_wall(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> RoomWall:
    """The edge nearest the opening's center; the builder puts the center on the edge."""
    if not boundary or len(boundary) < 3:
        min_x, min_y, max_x, max_y = room_bounds(room_area=room_area)
        boundary = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
    return nearest_wall(door.get("center") or [0.0, 0.0], boundary)


def door_wall(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> int:
    return _opening_wall(door, boundary=boundary, room_area=room_area).index


def door_inward_normal(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> tuple[float, float]:
    wall = _opening_wall(door, boundary=boundary, room_area=room_area)
    return _CARDINAL_NORMALS[wall.facing] if wall.facing else wall.normal


def door_dimensions(door: dict) -> tuple[float, float]:
    """Return (opening_width, thickness) for a door or window; the web sends depth 0.05 as the thickness."""
    opening_width = max(float(door.get("width") or DEFAULT_DOOR_WIDTH), 0.1)
    thickness = float(door.get("thickness") or door.get("depth") or DEFAULT_DOOR_THICKNESS)
    return opening_width, clamp(thickness, 0.02, 0.35)


def _oriented_rect(
    origin: tuple[float, float],
    direction: tuple[float, float],
    normal: tuple[float, float],
    half_along: float,
    depth: float,
) -> list[list[float]]:
    """Corners of the rectangle centered on ``origin`` along the wall, reaching ``depth`` inward from it."""
    ux, uy = direction
    nx, ny = normal
    return [
        [origin[0] - ux * half_along, origin[1] - uy * half_along],
        [origin[0] + ux * half_along, origin[1] + uy * half_along],
        [origin[0] + ux * half_along + nx * depth, origin[1] + uy * half_along + ny * depth],
        [origin[0] - ux * half_along + nx * depth, origin[1] - uy * half_along + ny * depth],
    ]


def _on_wall_rect(
    wall: RoomWall,
    opening_center: tuple[float, float],
    half_along: float,
    depth: float,
) -> dict[str, Any]:
    """Rectangle on the wall reaching ``depth`` into the room: axis-aligned on an axis wall, else an oriented
    ``polygon`` whose bounds give ``center``, ``width`` and ``depth``."""
    if wall.facing:
        nx, ny = _CARDINAL_NORMALS[wall.facing]
        return {
            "center": [opening_center[0] + nx * depth / 2, opening_center[1] + ny * depth / 2],
            "width": depth if wall.vertical else 2 * half_along,
            "depth": 2 * half_along if wall.vertical else depth,
            "inward_normal": [nx, ny],
        }
    polygon = _oriented_rect(opening_center, wall.direction, wall.normal, half_along, depth)
    min_x, min_y, max_x, max_y = Polygon(polygon).bounds
    return {
        "center": [(min_x + max_x) / 2, (min_y + max_y) / 2],
        "width": max_x - min_x,
        "depth": max_y - min_y,
        "inward_normal": list(wall.normal),
        "polygon": polygon,
    }


def door_opening_rect(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """The opening marker on its wall: the edge nearest ``center``, ``width`` along it, the thickness inward."""
    opening_width, thickness = door_dimensions(door)
    wall = _opening_wall(door, boundary=boundary, room_area=room_area)
    raw_center = door.get("center") or [0.0, 0.0]
    opening_center = wall.point_at(clamp(wall.along(float(raw_center[0]), float(raw_center[1])), 0.0, wall.length))
    return {
        "wall": wall.index,
        "wall_label": wall.label,
        "facing": wall.facing,
        "opening_center": list(opening_center),
        "opening_width": opening_width,
        "thickness": thickness,
        **_on_wall_rect(wall, opening_center, opening_width / 2, thickness),
    }


def door_clearance_rect(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Return the inward-only doorway clearance rectangle, as deep as the opening is wide (the leaf's swing)."""
    opening = door_opening_rect(door, boundary=boundary, room_area=room_area)
    wall = _opening_wall(door, boundary=boundary, room_area=room_area)
    clearance = opening["opening_width"]
    return {
        **opening,
        **_on_wall_rect(wall, tuple(opening["opening_center"]), clearance / 2, clearance),
        "clearance": clearance,
    }


def door_clearance_area(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> float:
    rect = door_clearance_rect(
        door,
        boundary=boundary,
        room_area=room_area,
    )
    return float(rect["width"]) * float(rect["depth"])


def rect_bounds(rect: dict[str, Any]) -> tuple[float, float, float, float]:
    cx, cy = rect["center"]
    width = float(rect["width"])
    depth = float(rect["depth"])
    return (
        float(cx) - width / 2,
        float(cy) - depth / 2,
        float(cx) + width / 2,
        float(cy) + depth / 2,
    )


def format_door_for_prompt(
    index: int,
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> str:
    """Consistent door string for all LLM prompts."""
    opening = door_opening_rect(door, boundary=boundary, room_area=room_area)
    clear_rect = door_clearance_rect(door, boundary=boundary, room_area=room_area)
    clear_min_x, clear_min_y, clear_max_x, clear_max_y = rect_bounds(clear_rect)
    open_x, open_y = opening["opening_center"]
    return (
        f"- door_{index}: wall {opening['wall']} ({opening['wall_label']}), "
        f"opening_center=[{open_x:.2f}, {open_y:.2f}], "
        f"opening_width={opening['opening_width']:.2f}m, "
        f"clear_zone x=[{clear_min_x:.2f}, {clear_max_x:.2f}] "
        f"y=[{clear_min_y:.2f}, {clear_max_y:.2f}]"
    )
