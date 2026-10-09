from collections.abc import Mapping
from typing import Any
from app.rules.geometry.primitives import room_polygon
from app.rules.protected_paths import protected_paths_for_intent
from app.rules.room_policy import normalize_room_type

from app.rules.pipeline_shared import (
    door_clearance_rect,
    door_opening_rect,
    infer_wall_from_center,
    rect_bounds,
)

from ._util import round4 as _round
from .feasibility_digest import build_feasibility_digest
from .fit_check import build_fit_check
from .intent_packet import requested_counts_from_intent_packet

_WALLS = ("left", "right", "bottom", "top")


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
    if "wall" in rect:
        payload["wall"] = rect["wall"]
    if "opening_center" in rect:
        payload["opening_center"] = _point(*rect["opening_center"])
    if "opening_width" in rect:
        payload["opening_width"] = _round(rect["opening_width"])
    if "thickness" in rect:
        payload["thickness"] = _round(rect["thickness"])
    if "clearance" in rect:
        payload["clearance"] = _round(rect["clearance"])
    if "inward_normal" in rect:
        payload["inward_normal"] = [_round(v) for v in rect["inward_normal"]]
    return payload


def _opening_wall(
    opening: dict[str, Any],
    room_vertices: list[list[float]],
) -> str:
    wall = str(opening.get("wall", "") or "").strip().lower()
    if wall in _WALLS:
        return wall
    return infer_wall_from_center(opening.get("center") or [0.0, 0.0], boundary=room_vertices)


def _wall_span(
    wall: str,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    wall_length: float,
) -> list[float]:
    if wall in {"left", "right"}:
        start, end = min_y, max_y
    else:
        start, end = min_x, max_x
    start = max(0.0, min(float(wall_length), float(start)))
    end = max(start, min(float(wall_length), float(end)))
    return [_round(start), _round(end)]


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

    wall_lengths = {
        "left": depth,
        "right": depth,
        "bottom": width,
        "top": width,
    }

    opening_records: list[dict[str, Any]] = []
    door_records: list[dict[str, Any]] = []
    window_records: list[dict[str, Any]] = []
    blocked_zones: list[dict[str, Any]] = []

    for index, door in enumerate(doors):
        opening_rect = door_opening_rect(door, boundary=room_vertices, room_area=room_area)
        clearance_rect = door_clearance_rect(door, boundary=room_vertices, room_area=room_area)
        wall = opening_rect["wall"]
        min_x, min_y, max_x, max_y = rect_bounds(opening_rect)
        span = _wall_span(wall, min_x, min_y, max_x, max_y, wall_lengths[wall])
        door_record = {
            "index": index,
            "wall": wall,
            "opening": _rect_payload(opening_rect, room_area),
            "blocked_zone": _rect_payload(clearance_rect, room_area),
            "span": span,
        }
        door_records.append(door_record)
        opening_records.append({"kind": "door", "wall": wall, "span": span})
        blocked_zones.append(
            {
                "id": f"door_{index}",
                "kind": "door",
                "wall": wall,
                "reason": "door_clearance",
                **_rect_payload(clearance_rect, room_area),
            }
        )

    for index, window in enumerate(windows):
        wall = _opening_wall(window, room_vertices)
        rect = {
            "wall": wall,
            "center": [
                float((window.get("center") or [0.0, 0.0])[0]),
                float((window.get("center") or [0.0, 0.0])[1]),
            ],
            "width": max(float(window.get("width", 0.0) or 0.0), 0.05),
            "depth": max(float(window.get("depth", 0.0) or 0.0), 0.05),
        }
        min_x, min_y, max_x, max_y = rect_bounds(rect)
        span = _wall_span(wall, min_x, min_y, max_x, max_y, wall_lengths[wall])
        window_record = {
            "index": index,
            "wall": wall,
            "blocked_zone": _rect_payload(rect, room_area),
            "span": span,
        }
        window_records.append(window_record)
        opening_records.append({"kind": "window", "wall": wall, "span": span})
        blocked_zones.append(
            {
                "id": f"window_{index}",
                "kind": "window",
                "wall": wall,
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
    by_wall = {wall: {"doors": 0, "windows": 0, "total": 0} for wall in _WALLS}
    for wall in _WALLS:
        wall_openings = [record for record in opening_records if record["wall"] == wall]
        for record in wall_openings:
            by_wall[wall][f"{record['kind']}s"] += 1
            by_wall[wall]["total"] += 1
        blocked_spans = _merge_spans([record["span"] for record in wall_openings])
        clear_spans = _clear_spans(wall_lengths[wall], blocked_spans)
        blocked_length = sum(end - start for start, end in blocked_spans)
        longest_clear_span = max((end - start for start, end in clear_spans), default=wall_lengths[wall])
        walls.append(
            {
                "wall": wall,
                "length": _round(wall_lengths[wall]),
                "opening_count": by_wall[wall]["total"],
                "door_count": by_wall[wall]["doors"],
                "window_count": by_wall[wall]["windows"],
                "blocked_spans": blocked_spans,
                "clear_spans": clear_spans,
                "blocked_length": _round(blocked_length),
                "blocked_ratio": _round(blocked_length / wall_lengths[wall]) if wall_lengths[wall] > 0 else 0.0,
                "longest_clear_span": _round(longest_clear_span),
            }
        )

    candidate_anchor_walls = sorted(
        [
            {
                "wall": wall["wall"],
                "usable_length": wall["longest_clear_span"],
                "blocked_ratio": wall["blocked_ratio"],
                "opening_count": wall["opening_count"],
            }
            for wall in walls
            if wall["longest_clear_span"] > 0
        ],
        key=lambda wall: (-wall["usable_length"], wall["blocked_ratio"], wall["opening_count"], wall["wall"]),
    )
    if candidate_anchor_walls:
        candidate_anchor_walls[0]["preferred"] = True

    return {
        "version": 1,
        "shape": "rectangle",
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
            "by_wall": by_wall,
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


def format_room_facts_for_prompt(room_facts: Mapping[str, Any] | None) -> str:
    if not isinstance(room_facts, Mapping):
        return ""

    summary = room_facts.get("summary") or {}
    openings = room_facts.get("openings") or {}
    counts = openings.get("counts") or {}
    anchor_walls = room_facts.get("candidate_anchor_walls") or []
    top_anchors = ", ".join(
        f"{wall.get('wall')} ({wall.get('usable_length', 0):.2f}m)"
        for wall in anchor_walls[:3]
        if wall.get("wall")
    ) or "none"

    dimensions = summary.get("dimensions") or [room_facts.get("width"), room_facts.get("depth")]
    width = float(dimensions[0] or 0.0)
    depth = float(dimensions[1] or 0.0)
    area = float(summary.get("area") or room_facts.get("area") or 0.0)
    best_anchor = summary.get("best_anchor_wall") or "none"
    blocked_zone_count = len(room_facts.get("blocked_zones") or [])
    protected_path_count = len(room_facts.get("protected_paths") or [])

    return (
        "ROOM FACTS:\n"
        f"- dimensions: {width:.2f}m x {depth:.2f}m ({area:.2f} sqm)\n"
        f"- openings: {int(counts.get('doors', 0))} doors, {int(counts.get('windows', 0))} windows\n"
        f"- blocked_zones: {blocked_zone_count}\n"
        f"- protected_paths: {protected_path_count}\n"
        f"- best_anchor_wall: {best_anchor}\n"
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
    geometry plus:
    - protected_paths: circulation paths the layout must keep clear (P1).
    - facts: walls, openings, blocked_zones, candidate_anchor_walls.
    - digest: room_scale, density_budget, max_counts_by_category (category
      caps), required and optional roles, blocked categories.
    - furniture_area_sqm: usable floor area, the selection footprint limit.
    - fit: build_fit_check estimate for the requested counts; fit.counts.recommended
      holds the counts that fit.
    - fit_warning: True when the estimate is tight or overcrowded and some
      requested count does not fit, so selection starts with compact products.
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
    protected_paths = protected_paths_for_intent(room_area, doors, intent)
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
        "wall_height": wall_height,
        "room_doors": doors,
        "room_windows": windows,
        "protected_paths": protected_paths,
        "facts": facts,
        "digest": digest,
        "furniture_area_sqm": float(digest["available_furnishing_area_sqm"]),
        "fit": fit,
        "fit_warning": fit["severity"] in {"tight", "overcrowded"}
        and any(int(recommended.get(category, 0)) < int(count) for category, count in requested.items()),
    }
