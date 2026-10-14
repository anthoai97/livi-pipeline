from collections.abc import Mapping
from typing import Any
from app.rules.geometry.primitives import room_polygon
from app.rules.protected_paths import build_protected_paths
from app.rules.room_policy import normalize_room_type

from app.rules.pipeline_shared import (
    DEFAULT_WALL_HEIGHT_M,
    door_clearance_rect,
    door_opening_rect,
    rect_bounds,
    room_walls,
)

from ._util import round4 as _round
from ._util import wall_text
from .feasibility_digest import build_feasibility_digest
from .fit_check import build_fit_check
from .intent_packet import requested_counts_from_intent_packet


def _point(x: float, y: float) -> list[float]:
    return [_round(x), _round(y)]


def _normalize(value: float, size: float) -> float:
    if size <= 0:
        return 0.0
    return _round(value / size)


def _normalize_point(x: float, y: float, room_area: tuple[float, float]) -> list[float]:
    width, depth = room_area
    return [_normalize(x, width), _normalize(y, depth)]


def _bounds_payload(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    room_area: tuple[float, float],
) -> dict[str, list[float]]:
    return {
        "min": _point(min_x, min_y),
        "max": _point(max_x, max_y),
        "size": _point(max_x - min_x, max_y - min_y),
        "center": _point((min_x + max_x) / 2, (min_y + max_y) / 2),
        "normalized_min": _normalize_point(min_x, min_y, room_area),
        "normalized_max": _normalize_point(max_x, max_y, room_area),
        "normalized_size": _normalize_point(max_x - min_x, max_y - min_y, room_area),
        "normalized_center": _normalize_point((min_x + max_x) / 2, (min_y + max_y) / 2, room_area),
    }


def _rect_payload(
    rect: dict[str, Any],
    room_area: tuple[float, float],
) -> dict[str, Any]:
    min_x, min_y, max_x, max_y = rect_bounds(rect)
    payload = {
        "center": _point(*rect["center"]),
        "width": _round(rect["width"]),
        "depth": _round(rect["depth"]),
        "bounds": _bounds_payload(min_x, min_y, max_x, max_y, room_area),
    }
    for key in ("wall", "wall_label", "facing"):
        if key in rect:
            payload[key] = rect[key]
    for key in ("opening_width", "thickness", "clearance"):
        if key in rect:
            payload[key] = _round(rect[key])
    for key in ("opening_center", "inward_normal"):
        if key in rect:
            payload[key] = [_round(v) for v in rect[key]]
    if rect.get("polygon"):
        payload["polygon"] = [_point(x, y) for x, y in rect["polygon"]]
    return payload


def _merge_spans(spans: list[list[float]]) -> list[list[float]]:
    if not spans:
        return []
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
            continue
        merged[-1][1] = max(merged[-1][1], end)
    return [[_round(start), _round(end)] for start, end in merged]


def _clear_spans(wall_length: float, blocked_spans: list[list[float]]) -> list[list[float]]:
    clear: list[list[float]] = []
    cursor = 0.0
    for start, end in blocked_spans:
        if start > cursor:
            clear.append([_round(cursor), _round(start)])
        cursor = max(cursor, end)
    if cursor < wall_length:
        clear.append([_round(cursor), _round(wall_length)])
    return clear


def build_room_facts(
    *,
    room_area: tuple[float, float],
    room_vertices: list[list[float]],
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    width, depth = float(room_area[0]), float(room_area[1])
    area = room_polygon(room_area, room_vertices).area
    doors = room_doors or []
    windows = room_windows or []

    boundary = {
        "vertices": [_point(x, y) for x, y in room_vertices],
        "normalized_vertices": [_normalize_point(x, y, room_area) for x, y in room_vertices],
    }
    bounds = _bounds_payload(0.0, 0.0, width, depth, room_area)
    edges = room_walls(room_vertices)

    opening_records: list[dict[str, Any]] = []
    door_records: list[dict[str, Any]] = []
    window_records: list[dict[str, Any]] = []
    blocked_zones: list[dict[str, Any]] = []

    def wall_span(rect: dict[str, Any]) -> tuple[float, list[float]]:
        """The opening's center and extent along its wall, from the wall's start."""
        wall = edges[rect["wall"]]
        at = wall.along(*rect["opening_center"])
        half = rect["opening_width"] / 2
        return _round(at), [_round(max(0.0, at - half)), _round(min(wall.length, at + half))]

    for index, door in enumerate(doors):
        opening_rect = door_opening_rect(door, boundary=room_vertices, room_area=room_area)
        clearance_rect = door_clearance_rect(door, boundary=room_vertices, room_area=room_area)
        wall = opening_rect["wall"]
        at, span = wall_span(opening_rect)
        door_records.append({
            "index": index,
            "wall": wall,
            "opening": _rect_payload(opening_rect, room_area),
            "blocked_zone": _rect_payload(clearance_rect, room_area),
            "span": span,
        })
        opening_records.append({"kind": "door", "index": index, "wall": wall, "at": at, "span": span})
        blocked_zones.append(
            {
                "id": f"door_{index}",
                "kind": "door",
                "reason": "door_clearance",
                **_rect_payload(clearance_rect, room_area),
            }
        )

    for index, window in enumerate(windows):
        rect = door_opening_rect(window, boundary=room_vertices, room_area=room_area)
        wall = rect["wall"]
        at, span = wall_span(rect)
        window_records.append({
            "index": index,
            "wall": wall,
            "blocked_zone": _rect_payload(rect, room_area),
            "span": span,
        })
        opening_records.append({"kind": "window", "index": index, "wall": wall, "at": at, "span": span})
        blocked_zones.append(
            {
                "id": f"window_{index}",
                "kind": "window",
                "reason": "window_opening",
                **_rect_payload(rect, room_area),
            }
        )

    protected_paths = list(protected_paths or [])
    for path in protected_paths:
        payload = {
            "id": path.get("id"),
            "kind": "protected_path",
            "reason": path.get("reason"),
            "axis": path.get("axis"),
            "wall_pair": path.get("wall_pair") or [],
            "required_width": _round(float(path.get("required_width") or 0.0)),
            **_rect_payload(path, room_area),
        }
        blocked_zones.append(payload)

    walls: list[dict[str, Any]] = []
    for edge in edges:
        wall_openings = [record for record in opening_records if record["wall"] == edge.index]
        blocked_spans = _merge_spans([record["span"] for record in wall_openings])
        clear_spans = _clear_spans(edge.length, blocked_spans)
        blocked_length = sum(end - start for start, end in blocked_spans)
        longest_clear_span = max((end - start for start, end in clear_spans), default=edge.length)
        walls.append(
            {
                "wall": edge.index,
                "label": edge.label,
                "facing": edge.facing,
                "start": _point(*edge.start),
                "end": _point(*edge.end),
                "on_box": edge.on_box,
                "length": _round(edge.length),
                "opening_count": len(wall_openings),
                "door_count": sum(record["kind"] == "door" for record in wall_openings),
                "window_count": sum(record["kind"] == "window" for record in wall_openings),
                "openings": [{"kind": record["kind"], "index": record["index"], "at": record["at"], "span": record["span"]}
                             for record in wall_openings],
                "blocked_spans": blocked_spans,
                "clear_spans": clear_spans,
                "blocked_length": _round(blocked_length),
                "blocked_ratio": _round(blocked_length / edge.length),
                "longest_clear_span": _round(longest_clear_span),
            }
        )

    candidate_anchor_walls = sorted(
        [
            {
                "wall": wall["wall"],
                "label": wall["label"],
                "facing": wall["facing"],
                "usable_length": wall["longest_clear_span"],
                "blocked_ratio": wall["blocked_ratio"],
                "opening_count": wall["opening_count"],
            }
            for wall in walls
            if wall["longest_clear_span"] > 0
        ],
        # The facing name breaks ties as it always has, so a rectangle ranks exactly as before; the index then
        # separates walls facing the same way.
        key=lambda wall: (-wall["usable_length"], wall["blocked_ratio"], wall["opening_count"], wall["facing"] or "~", wall["wall"]),
    )
    if candidate_anchor_walls:
        candidate_anchor_walls[0]["preferred"] = True

    return {
        "version": 1,
        "shape": "rectangle" if len(edges) == 4 and all(edge.on_box for edge in edges) else "polygon",
        "vertex_count": len(edges),
        "boundary": boundary,
        "bounds": bounds,
        "width": _round(width),
        "depth": _round(depth),
        "area": _round(area),
        "openings": {
            "counts": {
                "doors": len(door_records),
                "windows": len(window_records),
                "total": len(door_records) + len(window_records),
            },
            "doors": door_records,
            "windows": window_records,
        },
        "blocked_zones": blocked_zones,
        "protected_paths": protected_paths,
        "walls": walls,
        "candidate_anchor_walls": candidate_anchor_walls,
        "summary": {
            "dimensions": _point(width, depth),
            "area": _round(area),
            "opening_counts": {
                "doors": len(door_records),
                "windows": len(window_records),
            },
            "protected_path_count": len(protected_paths),
            "best_anchor_wall": candidate_anchor_walls[0]["wall"] if candidate_anchor_walls else None,
            "largest_clear_wall_span": candidate_anchor_walls[0]["usable_length"] if candidate_anchor_walls else 0.0,
        },
    }


def format_room_shape(room_facts: Mapping[str, Any]) -> str:
    """"rectangle 4.00m x 5.00m" or "polygon with 8 walls in a 4.00m x 5.00m box"."""
    width = float(room_facts.get("width") or 0.0)
    depth = float(room_facts.get("depth") or 0.0)
    if room_facts.get("shape") == "rectangle":
        return f"rectangle {width:.2f}m x {depth:.2f}m"
    return f"polygon with {room_facts.get('vertex_count')} walls in a {width:.2f}m x {depth:.2f}m box"


def format_room_facts_for_prompt(room_facts: Mapping[str, Any] | None) -> str:
    if not isinstance(room_facts, Mapping):
        return ""

    openings = room_facts.get("openings") or {}
    counts = openings.get("counts") or {}
    anchor_walls = room_facts.get("candidate_anchor_walls") or []
    top_anchors = ", ".join(
        f"{wall_text(wall)} ({wall.get('usable_length', 0):.2f}m)" for wall in anchor_walls[:3]
    ) or "none"
    wall_lines = [
        f"  - {wall_text(wall)}, {float(wall.get('length') or 0.0):.2f} m"
        + "".join(f", {opening['kind']} at {float(opening['at']):.2f} m" for opening in wall.get("openings") or [])
        for wall in room_facts.get("walls") or []
    ]
    area = float(room_facts.get("area") or 0.0)
    blocked_zone_count = len(room_facts.get("blocked_zones") or [])
    protected_path_count = len(room_facts.get("protected_paths") or [])

    return (
        "ROOM FACTS:\n"
        f"- shape: {format_room_shape(room_facts)}, {area:.2f} sqm\n"
        f"- walls (index (name), length, openings at their distance from the wall's start):\n"
        + "\n".join(wall_lines) + "\n"
        f"- openings: {int(counts.get('doors', 0))} doors, {int(counts.get('windows', 0))} windows\n"
        f"- blocked_zones: {blocked_zone_count}\n"
        f"- protected_paths: {protected_path_count}\n"
        f"- best_anchor_wall: {wall_text(anchor_walls[0] if anchor_walls else None)}\n"
        f"- candidate_anchor_walls: {top_anchors}"
    )


def build_room_context(
    *,
    room_type: str,
    room_area: tuple[float, float],
    room_vertices: list[list[float]] | None,
    wall_height: float | None,
    room_doors: list[dict[str, Any]] | None,
    room_windows: list[dict[str, Any]] | None,
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    """Room context for one request: geometry facts, policy, and the fit estimate.

    intent is the cleaned intent (coerce_intent_packet). Returns the request
    geometry (wall_height falls back to DEFAULT_WALL_HEIGHT_M) plus:
    - protected_paths: circulation paths the layout must keep clear (P1).
    - facts: walls, openings, blocked_zones, candidate_anchor_walls.
    - digest: room_scale, density_budget, max_counts_by_category (category
      caps), required and optional roles, blocked categories.
    - furniture_area_sqm: usable floor area, the selection footprint limit.
    - fit: build_fit_check estimate for the requested counts; fit.counts.recommended
      holds the counts that fit.
    - fit_warning: True when the estimate is overcrowded and some requested
      count does not fit, so selection starts with compact products. A tight
      estimate stays advisory, as in the legacy pipeline.
    """
    room_type = normalize_room_type(room_type)
    room_area = (float(room_area[0]), float(room_area[1]))
    vertices = room_vertices or [
        [0.0, 0.0],
        [room_area[0], 0.0],
        [room_area[0], room_area[1]],
        [0.0, room_area[1]],
    ]
    doors = list(room_doors or [])
    windows = list(room_windows or [])
    intent = {**intent, "room_type": room_type}
    protected_paths = build_protected_paths(vertices, doors)
    facts = build_room_facts(
        room_area=room_area,
        room_vertices=vertices,
        room_doors=doors,
        room_windows=windows,
        protected_paths=protected_paths,
    )
    digest = build_feasibility_digest(room_facts=facts, intent_packet=intent)
    fit = build_fit_check(
        room_facts=facts,
        feasibility_digest=digest,
        requested_counts=requested_counts_from_intent_packet(intent),
        room_type=room_type,
    )
    fit.pop("category_loads", None)
    requested = fit["counts"]["requested"]
    recommended = fit["counts"]["recommended"]
    return {
        "room_type": room_type,
        "room_area": room_area,
        "room_vertices": vertices,
        "wall_height": float(wall_height or DEFAULT_WALL_HEIGHT_M),
        "room_doors": doors,
        "room_windows": windows,
        "protected_paths": protected_paths,
        "facts": facts,
        "digest": digest,
        "furniture_area_sqm": float(digest["available_furnishing_area_sqm"]),
        "fit": fit,
        "fit_warning": fit["severity"] == "overcrowded"
        and any(int(recommended.get(category, 0)) < int(count) for category, count in requested.items()),
    }
