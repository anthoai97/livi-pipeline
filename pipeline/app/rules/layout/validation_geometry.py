"""Boundary, collision, opening, and protected-path checks."""

from typing import Any
from shapely.geometry import Point, Polygon
from app.rules.geometry.primitives import asset_polygon, extract_placement, is_rug
from app.rules.layout_rules import WALL_FLUSH_MAX_GAP_M
from app.rules.layout.studio import is_freestanding_studio_media_support
from app.rules.planner.taxonomy import SOFA_ROLE_KEYWORDS
from app.rules.pipeline_shared import (
    DEFAULT_WALL_HEIGHT_M,
    ceiling_mount_z,
    is_wall_aligned_asset,
    matches_category_keywords,
    nearest_axis_wall,
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
    _matches_item,
    _supported_parent_uid,
    _window_seating_clearance_poly,
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
    wall_height: float = DEFAULT_WALL_HEIGHT_M,
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
            or (pos[2] < 1.2 and height <= wall_height - 1.2 and not _is_media_display_asset(asset, uid))
            or pos[2] < 0
            or pos[2] + height > wall_height + 1e-6
            or not _is_near_cardinal(rotation_z)
            or _back_wall_name(rotation_z) != nearest_axis_wall(pos, boundary).facing
        ):
            violations.append(uid)
    return violations

def compute_ceiling_mount_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    wall_height: float = DEFAULT_WALL_HEIGHT_M,
) -> list[str]:
    violations = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if not _is_ceiling_mounted_layout_asset(asset, uid):
            continue
        pos, _, _, _, _ = extract_placement(placement, asset)
        expected = ceiling_mount_z(asset.get("height", 0.3), wall_height)
        if abs(pos[2] - expected) > 0.05:
            violations.append(uid)
    return violations

def compute_window_front_violations(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    room_windows: list[dict[str, Any]] | None,
    boundary: list[list[float]],
) -> list[dict[str, Any]]:
    """Floor pieces taller than a window's sill standing in the strip in front of it (advisory)."""
    violations = []
    for index, window in enumerate(room_windows or []):
        sill = window.get("sill_height")
        sill = 0.9 if sill is None else float(sill)
        strip = _window_seating_clearance_poly(window, boundary)
        for item in floor_assets(layout, assets, skip_rugs=True):
            height = float(item.asset.get("height") or 0.0)
            if height > sill + 1e-6 and item.poly.intersection(strip).area > 1e-6:
                violations.append({"uid": item.uid, "window": index, "kind": "tall_piece_in_front_of_window",
                                   "height_m": height, "sill_height_m": sill})
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
        if _back_wall_name(item.rot_z) != nearest_axis_wall(item.pos, boundary).facing:
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
                    # A routed path is not a band: no single move along an axis is guaranteed to clear it.
                    **({"translations_to_clear_m": {
                        "left": float(path_min_x - item_max_x),
                        "right": float(path_max_x - item_min_x),
                        "down": float(path_min_y - item_max_y),
                        "up": float(path_max_y - item_min_y),
                    }} if path.get("axis") in ("x", "y") else {}),
                }
            )
    return violations
