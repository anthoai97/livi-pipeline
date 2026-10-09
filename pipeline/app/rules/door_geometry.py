"""Room-bounds, room-wall and door-opening geometry helpers."""

import math
from typing import Any, NamedTuple

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

DEFAULT_DOOR_WIDTH = 0.9   # meters – used when door geometry is missing
DEFAULT_DOOR_THICKNESS = 0.08  # meters – thin opening marker along the wall
DOOR_CLEARANCE = 0.6       # meters – minimum gap between furniture and door zone
WALL_NAMES = ("left", "right", "bottom", "top")
WALL_CARDINAL_TOLERANCE = 0.2  # radians – steeper edges are angled, not named walls


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
    """A near-cardinal room edge, named by the side of the room it bounds."""

    name: str
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def vertical(self) -> bool:
        return self.name in ("left", "right")

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
        along, across = (y, x) if self.vertical else (x, y)
        lo, hi = self.span()
        overshoot = max(lo - along, 0.0, along - hi)
        offset = abs(across - self.line_at(along))
        return math.hypot(offset, overshoot) if overshoot else offset


def room_walls(boundary: list[list[float]]) -> list[RoomWall]:
    """Near-cardinal edges of the room polygon, ordered left, right, bottom, top.

    Angled edges are left out: wall-aligned and wall-mounted items stay cardinal,
    so they never back onto one.
    """
    ring = orient(Polygon(boundary), 1.0).exterior.coords
    walls = []
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        if (x0, y0) == (x1, y1):
            continue
        # The ring runs counter-clockwise, so the room lies left of each edge.
        inward_yaw = math.atan2(x1 - x0, y0 - y1)
        quarter = round(inward_yaw / (math.pi / 2))
        if abs(inward_yaw - quarter * math.pi / 2) > WALL_CARDINAL_TOLERANCE:
            continue
        # A wall facing +X bounds the left side of the room, +Y the bottom.
        name = ("left", "bottom", "right", "top")[quarter % 4]
        walls.append(RoomWall(name, (x0, y0), (x1, y1)))
    return sorted(walls, key=lambda wall: WALL_NAMES.index(wall.name))


def nearest_wall(
    point: list[float] | tuple[float, float],
    boundary: list[list[float]],
) -> RoomWall:
    x, y = float(point[0]), float(point[1])
    return min(room_walls(boundary), key=lambda wall: wall.distance(x, y))


def infer_wall_from_center(
    center: list[float] | tuple[float, float],
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> str:
    if not boundary or len(boundary) < 3:
        min_x, min_y, max_x, max_y = room_bounds(room_area=room_area)
        boundary = [[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]]
    return nearest_wall(center, boundary).name


def door_wall(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> str:
    wall = str(door.get("wall", "") or "").strip().lower()
    if wall in WALL_NAMES:
        return wall
    center = door.get("center") or [0.0, 0.0]
    return infer_wall_from_center(center, boundary=boundary, room_area=room_area)


def door_inward_normal(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> tuple[float, float]:
    wall = door_wall(door, boundary=boundary, room_area=room_area)
    if wall == "left":
        return 1.0, 0.0
    if wall == "right":
        return -1.0, 0.0
    if wall == "bottom":
        return 0.0, 1.0
    return 0.0, -1.0


def door_dimensions(door: dict) -> tuple[float, float]:
    """Return (opening_width, thickness) for a door opening."""
    opening_width = max(
        float(door.get("width", DEFAULT_DOOR_WIDTH) or DEFAULT_DOOR_WIDTH),
        0.1,
    )
    thickness = float(
        door.get("thickness", door.get("depth", DEFAULT_DOOR_THICKNESS))
        or DEFAULT_DOOR_THICKNESS
    )
    # Older runs stored depth=width. Treat oversized depth as missing thickness
    # unless the caller explicitly supplied a true wall thickness.
    if thickness > min(0.35, opening_width * 0.5) and "thickness" not in door:
        thickness = DEFAULT_DOOR_THICKNESS
    thickness = max(0.02, min(thickness, max(opening_width, DEFAULT_DOOR_THICKNESS)))
    return opening_width, thickness


def door_opening_rect(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Return the canonical axis-aligned rectangle for the door opening marker."""
    opening_width, thickness = door_dimensions(door)
    wall = door_wall(door, boundary=boundary, room_area=room_area)
    nx, ny = door_inward_normal(door, boundary=boundary, room_area=room_area)
    raw_center = door.get("center") or [0.0, 0.0]
    cx, cy = float(raw_center[0]), float(raw_center[1])
    min_x, min_y, max_x, max_y = room_bounds(boundary=boundary, room_area=room_area)

    # Imported concave rooms can have openings on inset boundary segments.
    # Their measured center is already wall-anchored, not on the bounding box.
    if boundary:
        opening_center = [cx, cy]
        rect_center = [cx + nx * thickness / 2, cy + ny * thickness / 2]
        rect_width, rect_depth = ((thickness, opening_width) if wall in {"left", "right"}
                                  else (opening_width, thickness))
    elif wall in {"left", "right"}:
        cy = clamp(cy, min_y + opening_width / 2, max_y - opening_width / 2)
        opening_center = [min_x if wall == "left" else max_x, cy]
        rect_center = [opening_center[0] + nx * thickness / 2, opening_center[1]]
        rect_width = thickness
        rect_depth = opening_width
    else:
        cx = clamp(cx, min_x + opening_width / 2, max_x - opening_width / 2)
        opening_center = [cx, min_y if wall == "bottom" else max_y]
        rect_center = [opening_center[0], opening_center[1] + ny * thickness / 2]
        rect_width = opening_width
        rect_depth = thickness

    return {
        "wall": wall,
        "opening_center": opening_center,
        "center": rect_center,
        "width": rect_width,
        "depth": rect_depth,
        "opening_width": opening_width,
        "thickness": thickness,
        "inward_normal": [nx, ny],
    }


def door_clearance_rect(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
    clearance: float = DOOR_CLEARANCE,
) -> dict[str, Any]:
    """Return the inward-only doorway clearance rectangle."""
    opening = door_opening_rect(door, boundary=boundary, room_area=room_area)
    nx, ny = opening["inward_normal"]
    opening_center = opening["opening_center"]
    if opening["wall"] in {"left", "right"}:
        return {
            **opening,
            "center": [opening_center[0] + nx * clearance / 2, opening_center[1]],
            "width": clearance,
            "depth": opening["opening_width"],
            "clearance": clearance,
        }
    return {
        **opening,
        "center": [opening_center[0], opening_center[1] + ny * clearance / 2],
        "width": opening["opening_width"],
        "depth": clearance,
        "clearance": clearance,
    }


def door_clearance_area(
    door: dict,
    *,
    boundary: list[list[float]] | None = None,
    room_area: tuple[float, float] | None = None,
    clearance: float = DOOR_CLEARANCE,
) -> float:
    rect = door_clearance_rect(
        door,
        boundary=boundary,
        room_area=room_area,
        clearance=clearance,
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
        f"- door_{index}: wall={opening['wall']}, "
        f"opening_center=[{open_x:.2f}, {open_y:.2f}], "
        f"opening_width={opening['opening_width']:.2f}m, "
        f"clear_zone x=[{clear_min_x:.2f}, {clear_max_x:.2f}] "
        f"y=[{clear_min_y:.2f}, {clear_max_y:.2f}]"
    )
