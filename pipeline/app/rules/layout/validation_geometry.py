"""Boundary, collision, opening, and protected-path checks."""

from typing import Any
import math
from shapely.geometry import Point, Polygon
from app.rules.door_geometry import room_walls
from app.rules.geometry.primitives import asset_polygon, extract_placement, is_rug
from app.rules.layout_rules import WALL_FLUSH_MAX_GAP_M
from app.rules.layout.studio import is_freestanding_studio_media_support
from app.rules.planner.taxonomy import SOFA_ROLE_KEYWORDS
from app.rules.pipeline_shared import (
    CEILING_HEIGHT_M,
    ceiling_mount_z,
    is_wall_aligned_asset,
    matches_category_keywords,
    nearest_wall,
    requires_window_clearance,
)
from app.rules.protected_paths import protected_path_polygon

from app.rules.layout.constants import (
    MEDIA_ROLE_KEYWORDS,
    OVERLAP_EXEMPT_CATEGORIES,
    PROTECTED_PATH_MIN_OVERLAP_SQM,
    WALL_MOUNT_WALL_THRESHOLD,
)
from app.rules.layout.metrics import (
    get_asset_by_uid,
)
from app.rules.layout.relations import (
    _back_wall_name,
    _build_blocker_specs,
    _is_ceiling_mounted_layout_asset,
    _is_media_display_asset,
    _is_near_cardinal,
    _is_wall_mounted_layout_asset,
    _inward_rotation_for_wall,
    _matches_item,
    _supported_parent_uid,
    _wall_mount_z,
    floor_assets,
)

def compute_overlaps(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    positions = []
    for item in floor_assets(layout, assets, skip_rugs=True):
        if item.asset.get("category", "") in OVERLAP_EXEMPT_CATEGORIES:
            continue
        positions.append((item.uid, item.poly, item.pos[2]))

    overlaps = []
    for index, (uid_a, poly_a, z_a) in enumerate(positions):
        for uid_b, poly_b, z_b in positions[index + 1 :]:
            if abs(z_a - z_b) > 0.1:
                continue
            if poly_a.intersects(poly_b):
                overlaps.append((uid_a, uid_b))
    return overlaps


def overlap_separation_facts(
    layout: dict[str, Any], assets: list[dict[str, Any]], pairs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Exact rectangular penetration and separating translations, without moving anything."""
    facts = []
    for uid_a, uid_b in pairs:
        polygons, yaws = [], []
        for uid in (uid_a, uid_b):
            pos, yaw, width, depth, _ = extract_placement(layout[uid], get_asset_by_uid(uid, assets))
            polygons.append(asset_polygon(pos, yaw, width, depth))
            yaws.append(yaw)
        axes = {(round(math.cos(yaw + offset), 8), round(math.sin(yaw + offset), 8))
                for yaw in yaws for offset in (0.0, math.pi / 2)}
        options = []
        for nx, ny in sorted(axes):
            projections = [[x * nx + y * ny for x, y in polygon.exterior.coords]
                           for polygon in polygons]
            a, b = projections
            for delta in (min(b) - max(a), max(b) - min(a)):
                padded = delta + math.copysign(.02, delta)
                options.append((abs(delta), [round(padded * nx, 5), round(padded * ny, 5)]))
        options.sort(key=lambda option: option[0])
        facts.append({
            "a": uid_a, "b": uid_b,
            "penetration_m": round(options[0][0], 5),
            "translations_for_a_to_clear_b_m": [vector for _, vector in options],
            "translation_for_b_is_negative_of_a": True,
            "clearance_margin_m": .02,
        })
    return facts


def wall_mount_placement_facts(
    layout: dict[str, Any], assets: list[dict[str, Any]], boundary: list[list[float]],
    room_doors: list[dict[str, Any]], room_windows: list[dict[str, Any]],
    room_area: tuple[float, float],
) -> list[dict[str, Any]]:
    """Opening-free wall spans and center intervals at each wall item's current height.

    Intervals describe individual actual wall segments, including concave rooms.
    They do not certify collisions with other furniture or artwork.
    """
    specs = _build_blocker_specs(room_doors, room_windows, boundary=boundary, room_area=room_area)
    facts = []
    for asset in assets:
        uid = str(asset.get("instance_key") or asset.get("uid") or "")
        if not _is_wall_mounted_layout_asset(asset, uid):
            continue
        placement = layout.get(uid, {})
        if _supported_parent_uid(uid, placement, layout, assets):
            continue
        width, depth = float(asset.get("width") or 0), float(asset.get("depth") or 0)
        height = float(asset.get("height") or 0)
        z = float((placement.get("position") or [0, 0, _wall_mount_z(asset)])[2])
        walls = []
        for wall in room_walls(boundary):
            yaw = _inward_rotation_for_wall(wall.name)
            nx, ny = math.cos(yaw), math.sin(yaw)
            strip = Polygon([wall.start, wall.end,
                             (wall.end[0] + nx * (depth + .04), wall.end[1] + ny * (depth + .04)),
                             (wall.start[0] + nx * (depth + .04), wall.start[1] + ny * (depth + .04))])
            spans = [wall.span()]
            openings = []
            axis = 1 if wall.vertical else 0
            for spec in specs:
                if not _opening_overlaps_height(spec, z, height) or not strip.intersects(spec["poly"]):
                    continue
                coords = [point[axis] for point in spec["poly"].exterior.coords]
                start, end = min(coords), max(coords)
                openings.append({"kind": spec["kind"], "index": spec["index"],
                                 "span_m": [round(start, 5), round(end, 5)]})
                spans = [part for low, high in spans
                         for part in ((low, min(high, start)), (max(low, end), high))
                         if part[1] > part[0]]
            half_span = width / 2 + .02
            walls.append({
                "wall": wall.name, "start_xy": wall.start, "end_xy": wall.end,
                "rotation_z": yaw, "along_world_axis": "y" if wall.vertical else "x",
                "available_spans_m": [[round(low, 5), round(high, 5)] for low, high in spans],
                "center_intervals_m": [[round(low + half_span, 5), round(high - half_span, 5)]
                                       for low, high in spans if high - low >= 2 * half_span],
                "blocking_openings": openings,
            })
        facts.append({"uid": uid, "width_m": width, "bottom_z_m": z,
                      "height_m": height, "edge_margin_m": .02, "walls": walls})
    return facts


def _opening_overlaps_height(spec: dict[str, Any], z: float, height: float) -> bool:
    return z < spec["height"] and z + height > spec.get("min_z", 0.0)

def compute_boundary_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> list[str]:
    room_poly = Polygon([(point[0], point[1]) for point in boundary])
    buffered = room_poly.buffer(0.05)
    violations = []
    for item in floor_assets(layout, assets):
        if not buffered.covers(item.poly):
            violations.append(item.uid)
    return violations

def compute_door_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_doors: list[dict[str, Any]],
    boundary: list[list[float]],
    room_area: tuple[float, float],
) -> list[tuple[str, int]]:
    door_specs = [
        spec
        for spec in _build_blocker_specs(
            room_doors,
            None,
            boundary=boundary,
            room_area=room_area,
        )
        if spec["kind"] == "door"
    ]
    violations = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if is_rug(uid, asset):
            continue
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        elevated_door_blocker = (
            _is_wall_mounted_layout_asset(asset, uid)
            or matches_category_keywords(
                asset.get("category", ""),
                uid,
                MEDIA_ROLE_KEYWORDS,
            )
        )
        if pos[2] > 0.1 and _supported_parent_uid(uid, placement, layout, assets):
            continue
        if pos[2] > 0.1 and not elevated_door_blocker:
            continue
        poly = asset_polygon(pos, rot_z, width, depth)
        for spec in door_specs:
            if pos[2] < spec["height"] and poly.intersects(spec["poly"]):
                violations.append((uid, spec["index"]))
    return violations

def compute_obstacle_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_windows: list[dict[str, Any]] | None,
    boundary: list[list[float]],
    room_area: tuple[float, float],
) -> list[str]:
    blockers = [
        spec
        for spec in _build_blocker_specs(
            None,
            room_windows,
            boundary=boundary,
            room_area=room_area,
        )
        if spec["kind"] == "window"
    ]
    violations = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if is_rug(uid, asset):
            continue
        pos, rot_z, width, depth, height = extract_placement(placement, asset)
        if not requires_window_clearance(
            asset.get("category", ""),
            uid,
            z=pos[2],
        ):
            continue
        poly = asset_polygon(pos, rot_z, width, depth)
        if any(poly.intersects(blocker["poly"])
               and (not _is_wall_mounted_layout_asset(asset, uid)
                    or _opening_overlaps_height(blocker, pos[2], height))
               for blocker in blockers):
            violations.append(uid)
    return violations

def compute_wall_mount_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> list[str]:
    room_poly = Polygon([(point[0], point[1]) for point in boundary])
    exterior = room_poly.exterior
    violations = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if not _is_wall_mounted_layout_asset(asset, uid):
            continue
        if _supported_parent_uid(uid, placement, layout, assets):
            continue
        pos, rotation_z, _, _, _ = extract_placement(placement, asset)
        point = Point(pos[0], pos[1])
        height = float(asset.get("height", 0.0) or 0.0)
        if (
            exterior.distance(point) > WALL_MOUNT_WALL_THRESHOLD
            or (pos[2] < 1.2 and height <= CEILING_HEIGHT_M - 1.2 and not _is_media_display_asset(asset, uid))
            or pos[2] < 0
            or pos[2] + height > CEILING_HEIGHT_M + 1e-6
            or not _is_near_cardinal(rotation_z)
            or _back_wall_name(rotation_z) != nearest_wall(pos, boundary).name
        ):
            violations.append(uid)
    return violations

def compute_ceiling_mount_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[str]:
    violations = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if not _is_ceiling_mounted_layout_asset(asset, uid):
            continue
        pos, _, _, _, _ = extract_placement(placement, asset)
        expected = ceiling_mount_z(asset.get("height", 0.3))
        if abs(pos[2] - expected) > 0.05:
            violations.append(uid)
    return violations

def compute_wall_aligned_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
) -> list[str]:
    room_poly = Polygon([(point[0], point[1]) for point in boundary])
    violations = []
    for item in floor_assets(layout, assets):
        if not is_wall_aligned_asset(item.asset.get("category", ""), item.uid):
            continue
        if not _is_near_cardinal(item.rot_z):
            violations.append(item.uid)
            continue
        if _matches_item(item, SOFA_ROLE_KEYWORDS) or is_freestanding_studio_media_support(item.asset):
            continue
        if _back_wall_name(item.rot_z) != nearest_wall(item.pos, boundary).name:
            violations.append(item.uid)
            continue
        wall_gap = room_poly.exterior.distance(item.poly)
        if wall_gap > WALL_FLUSH_MAX_GAP_M:
            violations.append(item.uid)
    return violations

def compute_protected_path_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    protected_paths: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if not protected_paths:
        return violations

    for path in protected_paths:
        path_poly = protected_path_polygon(path)
        path_min_x, path_min_y, path_max_x, path_max_y = path_poly.bounds
        for item in floor_assets(layout, assets, skip_rugs=True):
            if not item.poly.intersects(path_poly):
                continue
            overlap_area = float(item.poly.intersection(path_poly).area)
            if overlap_area <= PROTECTED_PATH_MIN_OVERLAP_SQM:
                continue
            item_min_x, item_min_y, item_max_x, item_max_y = item.poly.bounds
            violations.append(
                {
                    "uid": item.uid,
                    "path_id": path.get("id"),
                    "axis": path.get("axis"),
                    "wall_pair": path.get("wall_pair") or [],
                    "overlap_area_sqm": overlap_area,
                    "min_overlap_area_sqm": PROTECTED_PATH_MIN_OVERLAP_SQM,
                    "required_width_m": path.get("required_width"),
                    "path_bounds": {
                        "min_x": float(path_min_x),
                        "min_y": float(path_min_y),
                        "max_x": float(path_max_x),
                        "max_y": float(path_max_y),
                    },
                    "asset_bounds": {
                        "min_x": float(item_min_x),
                        "min_y": float(item_min_y),
                        "max_x": float(item_max_x),
                        "max_y": float(item_max_y),
                    },
                    "translations_to_clear_m": {
                        "left": float(path_min_x - item_max_x),
                        "right": float(path_max_x - item_min_x),
                        "down": float(path_min_y - item_max_y),
                        "up": float(path_max_y - item_min_y),
                    },
                }
            )
    return violations
