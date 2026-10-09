"""Asset classification, support, facing, and group relationships."""

import math
from typing import Any, Iterator
from shapely.geometry import Polygon
from app.rules.geometry.primitives import (
    asset_polygon,
    extract_placement,
    is_rug,
    polygon_from_rect,
    rect_polygon,
    snap_rotation,
    yaw_to_object_rotation,
)
from app.rules.layout_rules import SOFA_COFFEE_TABLE_DISTANCE_RANGE_M
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.taxonomy import (
    DESK_ROLE_KEYWORDS,
    DINING_CHAIR_ROLE_KEYWORDS,
    DINING_TABLE_ROLE_KEYWORDS,
    SEATING_ROLE_KEYWORDS,
    SOFA_ROLE_KEYWORDS,
)
from app.rules.pipeline_shared import (
    CEILING_HEIGHT_M,
    WALL_MOUNT_Z,
    door_clearance_rect,
    is_ceiling_mounted_asset,
    is_floor_lamp_asset,
    is_floor_only_asset,
    is_table_lamp_asset,
    is_table_lamp_support_asset,
    is_tabletop_support_asset,
    is_wall_mounted_asset,
    matches_category_keywords,
    room_walls,
    tabletop_support_priority,
)

from app.rules.layout.constants import (
    CARDINAL_TOLERANCE,
    COFFEE_TABLE_ROLE_KEYWORDS,
    LOUNGE_CHAIR_ROLE_KEYWORDS,
    MEDIA_DISPLAY_ROLE_KEYWORDS,
    MEDIA_SUPPORT_ROLE_KEYWORDS,
    SIDE_TABLE_ROLE_KEYWORDS,
    TABLETOP_DISPLAY_ROLE_KEYWORDS,
    TABLETOP_SUPPORT_INSET_M,
    TABLETOP_SUPPORT_TOLERANCE_M,
    TASK_CHAIR_ROLE_KEYWORDS,
    WINDOW_SEATING_CLEARANCE_M,
)
from app.rules.layout.metrics import (
    FloorAsset,
    get_asset_by_uid,
)

def floor_assets(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    skip_rugs: bool = False,
    keywords: tuple[str, ...] | None = None,
) -> Iterator[FloorAsset]:
    """Yield floor-level (z <= 0.1) assets with placement and footprint polygon."""
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        if skip_rugs and is_rug(uid, asset):
            continue
        if keywords is not None and not matches_category_keywords(
            asset.get("category", ""),
            uid,
            keywords,
        ):
            continue
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        if pos[2] > 0.1:
            continue
        yield FloorAsset(
            uid,
            asset,
            pos,
            rot_z,
            width,
            depth,
            asset_polygon(pos, rot_z, width, depth),
        )

def _gather_floor_seating(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> list[tuple[str, Polygon]]:
    return [
        (item.uid, item.poly)
        for item in floor_assets(layout, assets, keywords=SEATING_ROLE_KEYWORDS)
    ]

def _floor_asset_list(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    keywords: tuple[str, ...] | None = None,
    skip_rugs: bool = False,
) -> list[FloorAsset]:
    return list(
        floor_assets(
            layout,
            assets,
            keywords=keywords,
            skip_rugs=skip_rugs,
        )
    )

def _matches_item(item: FloorAsset, keywords: tuple[str, ...]) -> bool:
    return matches_category_keywords(
        item.asset.get("category", ""),
        item.uid,
        keywords,
    )

def _is_generic_chair(item: FloorAsset) -> bool:
    category = str(item.asset.get("category") or "").strip().lower()
    # Catalog ID prefixes can survive reclassification as office/accent chairs.
    return category == "chair" or (category in {"", "unknown"} and item.uid.startswith("chair_"))

def _largest_floor_asset(items: list[FloorAsset]) -> FloorAsset | None:
    if not items:
        return None
    return max(items, key=lambda item: item.width * item.depth)

def _angle_to(source: list[float], target: list[float]) -> float:
    return math.atan2(target[1] - source[1], target[0] - source[0]) % (2 * math.pi)

def _angle_delta_deg(rotation_z: float, target_angle: float) -> float:
    delta = abs(((rotation_z - target_angle + math.pi) % (2 * math.pi)) - math.pi)
    return math.degrees(delta)

def _facing_delta_to_item(source: FloorAsset, target: FloorAsset) -> float:
    return _angle_delta_deg(source.rot_z, _angle_to(source.pos, target.pos))

def _nearest_floor_asset(
    item: FloorAsset,
    candidates: list[FloorAsset],
) -> tuple[FloorAsset, float] | None:
    if not candidates:
        return None
    best = min(candidates, key=lambda candidate: float(item.poly.distance(candidate.poly)))
    return best, float(item.poly.distance(best.poly))

def _functional_group_assets(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, list[FloorAsset]]:
    floor_items = _floor_asset_list(layout, assets)
    rugs = [item for item in floor_items if is_rug(item.uid, item.asset)]
    dining_tables = [
        item for item in floor_items if _matches_item(item, DINING_TABLE_ROLE_KEYWORDS)
    ]
    dining_chairs = [
        item for item in floor_items if _matches_item(item, DINING_CHAIR_ROLE_KEYWORDS)
    ]
    if dining_tables:
        dining_chairs.extend(
            item
            for item in floor_items
            if _is_generic_chair(item)
            and str(item.asset.get("functional_role") or "")
            in {"", "dining_seating"}
            and item.uid not in {chair.uid for chair in dining_chairs}
        )
    dining_chair_uids = {item.uid for item in dining_chairs}
    sofas = [item for item in floor_items if _matches_item(item, SOFA_ROLE_KEYWORDS)]
    lounge_chairs = []
    for item in floor_items:
        functional_role = str(item.asset.get("functional_role") or "")
        if item.uid in dining_chair_uids or _matches_item(item, SOFA_ROLE_KEYWORDS):
            continue
        if functional_role and functional_role != "living_seating":
            continue
        if functional_role == "living_seating" or (
            _matches_item(item, LOUNGE_CHAIR_ROLE_KEYWORDS)
            and not _matches_item(
                item,
                DINING_CHAIR_ROLE_KEYWORDS + TASK_CHAIR_ROLE_KEYWORDS,
            )
        ):
            lounge_chairs.append(item)
    return {
        "sofas": sofas,
        "coffee_tables": [
            item for item in floor_items if _matches_item(item, COFFEE_TABLE_ROLE_KEYWORDS)
        ],
        "rugs": rugs,
        "lounge_chairs": lounge_chairs,
        "dining_tables": dining_tables,
        "dining_chairs": dining_chairs,
        "desks": [item for item in floor_items if _matches_item(item, DESK_ROLE_KEYWORDS)],
        "task_chairs": [
            item for item in floor_items if _matches_item(item, TASK_CHAIR_ROLE_KEYWORDS)
        ],
        "side_tables": [
            item for item in floor_items if _matches_item(item, SIDE_TABLE_ROLE_KEYWORDS)
        ],
        "floor_lamps": [
            item
            for item in floor_items
            if is_floor_lamp_asset(item.asset.get("category", ""), item.uid)
        ],
    }

def _placement_mode(asset: dict[str, Any], uid: str = "") -> str:
    return placement_mode_for_asset({**asset, "uid": uid or asset.get("uid")})

def _is_wall_mounted_layout_asset(asset: dict[str, Any], uid: str = "") -> bool:
    return _placement_mode(asset, uid) == "wall_mounted" or is_wall_mounted_asset(
        asset.get("category", ""),
        uid,
    )

def _is_ceiling_mounted_layout_asset(asset: dict[str, Any], uid: str = "") -> bool:
    return _placement_mode(asset, uid) == "ceiling_mounted" or is_ceiling_mounted_asset(
        asset.get("category", ""),
        uid,
    )

def _is_floor_only_layout_asset(asset: dict[str, Any], uid: str = "") -> bool:
    return _placement_mode(asset, uid) == "floor" or is_floor_only_asset(
        asset.get("category", ""),
        uid,
    )

def _wall_mount_z(asset: dict[str, Any]) -> float:
    height = float(asset.get("height", 0.0) or 0.0)
    return min(WALL_MOUNT_Z, max(0.0, CEILING_HEIGHT_M - height))

def _is_near_cardinal(rotation_z: float) -> bool:
    delta = abs(
        (rotation_z - snap_rotation(rotation_z, math.pi / 2) + math.pi)
        % (2 * math.pi)
        - math.pi
    )
    return delta <= CARDINAL_TOLERANCE

def _facing_axis_and_sign(rotation_z: float) -> tuple[str, float]:
    facing_idx = int(round(rotation_z / (math.pi / 2))) % 4
    if facing_idx == 0:
        return "x", 1.0
    if facing_idx == 2:
        return "x", -1.0
    if facing_idx == 1:
        return "y", 1.0
    return "y", -1.0

def _back_wall_name(rotation_z: float) -> str:
    facing_idx = int(round(rotation_z / (math.pi / 2))) % 4
    if facing_idx == 0:
        return "left"
    if facing_idx == 1:
        return "bottom"
    if facing_idx == 2:
        return "right"
    return "top"

def _front_wall_name(rotation_z: float) -> str:
    facing_idx = int(round(rotation_z / (math.pi / 2))) % 4
    if facing_idx == 0:
        return "right"
    if facing_idx == 1:
        return "top"
    if facing_idx == 2:
        return "left"
    return "bottom"

def _inward_rotation_for_wall(wall: str) -> float:
    return {
        "left": 0.0,
        "right": math.pi,
        "bottom": math.pi / 2,
        "top": 3 * math.pi / 2,
    }.get(wall, 0.0)

def _parallel_rotation_delta(a: float, b: float) -> float:
    return min(
        abs((a - b + math.pi) % (2 * math.pi) - math.pi),
        abs((a - b + 2 * math.pi) % (2 * math.pi) - math.pi),
        abs((a - b) % math.pi),
    )

def _long_axis_rotation(rotation_z: float, width: float, depth: float) -> float:
    if float(width) >= float(depth):
        return float(rotation_z) + math.pi / 2
    return float(rotation_z)

def _build_blocker_specs(
    room_doors: list[dict[str, Any]] | None,
    room_windows: list[dict[str, Any]] | None,
    *,
    boundary: list[list[float]],
    room_area: tuple[float, float],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []

    for index, door in enumerate(room_doors or []):
        rect = door_clearance_rect(door, boundary=boundary, room_area=room_area)
        blockers.append(
            {
                "kind": "door",
                "index": index,
                "height": float(door.get("height", 2.1) or 2.1),
                "poly": polygon_from_rect(rect),
                "preferred_directions": {
                    "left": ("right",),
                    "right": ("left",),
                    "bottom": ("up",),
                    "top": ("down",),
                }[rect["wall"]],
            }
        )

    for index, window in enumerate(room_windows or []):
        center = window.get("center") or [0.0, 0.0]
        width = float(window.get("width", 1.0) or 1.0)
        depth = float(window.get("depth", 0.1) or 0.1)
        sill_height = window.get("sill_height")
        sill_height = .9 if sill_height is None else float(sill_height)
        blockers.append(
            {
                "kind": "window",
                "index": index,
                "min_z": sill_height,
                "height": sill_height + float(window.get("height", 1.2)),
                "poly": rect_polygon(center, width, depth),
                "preferred_directions": (),
            }
        )

    return blockers

def _window_wall_line(
    window: dict[str, Any],
    boundary: list[list[float]],
) -> tuple[str, float] | None:
    """The window's wall and that wall's coordinate across its normal at the window."""
    name = str(window.get("wall") or "").lower()
    walls = [wall for wall in room_walls(boundary) if wall.name == name]
    if not walls:
        return None
    center = window.get("center") or [0.0, 0.0]
    x = float(center[0])
    y = float(center[1])
    wall = min(walls, key=lambda wall: wall.distance(x, y))
    return name, wall.line_at(y if wall.vertical else x)

def _window_seating_clearance_poly(
    window: dict[str, Any],
    boundary: list[list[float]],
) -> Polygon:
    center = window.get("center") or [0.0, 0.0]
    window_width = float(window.get("width", 1.0) or 1.0)
    window_depth = float(window.get("depth", 0.1) or 0.1)
    span = max(window_width, window_depth)
    clearance = WINDOW_SEATING_CLEARANCE_M
    wall_line = _window_wall_line(window, boundary)
    if wall_line is None:
        return rect_polygon(center, window_width, window_depth).buffer(clearance / 2)

    wall, line = wall_line
    if wall == "top":
        return rect_polygon([float(center[0]), line - clearance / 2], span, clearance)
    if wall == "bottom":
        return rect_polygon([float(center[0]), line + clearance / 2], span, clearance)
    if wall == "left":
        return rect_polygon([line + clearance / 2, float(center[1])], clearance, span)
    return rect_polygon([line - clearance / 2, float(center[1])], clearance, span)

def _back_wall_window_overlaps_item(
    item: FloorAsset,
    back_wall: str,
    room_windows: list[dict[str, Any]] | None,
) -> bool:
    item_min_x, item_min_y, item_max_x, item_max_y = item.poly.bounds
    for window in room_windows or []:
        if str(window.get("wall") or "").lower() != back_wall:
            continue
        center = window.get("center") or [0.0, 0.0]
        span = max(
            float(window.get("width", 1.0) or 1.0),
            float(window.get("depth", 0.1) or 0.1),
        )
        if back_wall in {"top", "bottom"}:
            win_min = float(center[0]) - span / 2
            win_max = float(center[0]) + span / 2
            if item_max_x >= win_min and item_min_x <= win_max:
                return True
        elif back_wall in {"left", "right"}:
            win_min = float(center[1]) - span / 2
            win_max = float(center[1]) + span / 2
            if item_max_y >= win_min and item_min_y <= win_max:
                return True
    return False

def _parent_uid(placement: dict[str, Any]) -> str:
    value = placement.get("on_top_of")
    return str(value).strip() if isinstance(value, str) else ""

def _is_media_display_asset(asset: dict[str, Any], uid: str = "") -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        MEDIA_DISPLAY_ROLE_KEYWORDS,
    )

def _is_media_support_asset(asset: dict[str, Any], uid: str = "") -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        MEDIA_SUPPORT_ROLE_KEYWORDS,
    )

def _is_supported_media_pair(
    asset: dict[str, Any],
    uid: str,
    parent_asset: dict[str, Any],
    parent_uid: str,
) -> bool:
    return _is_media_display_asset(asset, uid) and _is_media_support_asset(
        parent_asset,
        parent_uid,
    )

def _support_edge_margins(
    item_width: float,
    item_depth: float,
    parent_width: float,
    parent_depth: float,
) -> tuple[float, float]:
    usable_width = max(0.0, parent_width - 2 * TABLETOP_SUPPORT_INSET_M)
    usable_depth = max(0.0, parent_depth - 2 * TABLETOP_SUPPORT_INSET_M)
    return (
        (usable_width - item_width) / 2,
        (usable_depth - item_depth) / 2,
    )

def _support_footprint_fits(
    item_width: float,
    item_depth: float,
    parent_width: float,
    parent_depth: float,
) -> bool:
    margin_x, margin_y = _support_edge_margins(
        item_width,
        item_depth,
        parent_width,
        parent_depth,
    )
    return (
        margin_x >= -TABLETOP_SUPPORT_TOLERANCE_M
        and margin_y >= -TABLETOP_SUPPORT_TOLERANCE_M
    )

def _support_child_world_position(
    parent_pos: list[float],
    parent_rot_z: float,
    parent_width: float,
    parent_depth: float,
    item_width: float,
    item_depth: float,
    child_index: int,
) -> tuple[float, float]:
    margin_x, margin_y = _support_edge_margins(
        item_width,
        item_depth,
        parent_width,
        parent_depth,
    )
    max_x = max(0.0, margin_x)
    max_y = max(0.0, margin_y)
    slots = (
        (-0.45, 0.0),
        (0.45, 0.0),
        (0.0, 0.45),
        (0.0, -0.45),
        (-0.45, 0.45),
        (0.45, -0.45),
    )
    slot = slots[child_index % len(slots)]
    if child_index >= len(slots):
        slot = (-slot[0], -slot[1])
    if max_x < 0.04 and max_y < 0.04:
        local_x = 0.0
        local_y = 0.0
    else:
        local_x = slot[0] * max_x
        local_y = slot[1] * max_y
    object_rotation = yaw_to_object_rotation(parent_rot_z)
    cos_a = math.cos(object_rotation)
    sin_a = math.sin(object_rotation)
    return (
        float(parent_pos[0]) + local_x * cos_a - local_y * sin_a,
        float(parent_pos[1]) + local_x * sin_a + local_y * cos_a,
    )

def _nearest_media_support_uid(
    uid: str,
    placement: dict[str, Any],
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> str:
    asset = get_asset_by_uid(uid, assets)
    if not _is_media_display_asset(asset, uid):
        return ""

    pos, _, width, depth, _ = extract_placement(placement, asset)
    best_uid = ""
    best_distance = None
    for candidate_uid, candidate_placement in layout.items():
        if candidate_uid == uid:
            continue
        candidate_asset = get_asset_by_uid(candidate_uid, assets)
        if not _is_media_support_asset(candidate_asset, candidate_uid):
            continue
        candidate_pos, _, candidate_width, candidate_depth, _ = extract_placement(
            candidate_placement,
            candidate_asset,
        )
        if not _support_footprint_fits(
            width,
            depth,
            candidate_width,
            candidate_depth,
        ):
            continue
        distance = math.hypot(pos[0] - candidate_pos[0], pos[1] - candidate_pos[1])
        if best_distance is None or distance < best_distance:
            best_uid = candidate_uid
            best_distance = distance
    return best_uid

def _is_non_media_tabletop_item(
    asset: dict[str, Any],
    uid: str,
    placement: dict[str, Any],
) -> bool:
    if _is_media_display_asset(asset, uid):
        return False
    if is_table_lamp_asset(asset.get("category", ""), uid) or _is_tabletop_display_asset(
        asset.get("category", ""),
        uid,
        asset,
    ):
        return True
    category = asset.get("category", "")
    return (
        bool(_parent_uid(placement))
        and matches_category_keywords(category, uid, TABLETOP_DISPLAY_ROLE_KEYWORDS)
    ) and not (
        _is_floor_only_layout_asset(asset, uid)
        or _is_wall_mounted_layout_asset(asset, uid)
        or _is_ceiling_mounted_layout_asset(asset, uid)
    )

def _nearest_tabletop_support_uid(
    uid: str,
    placement: dict[str, Any],
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> str:
    asset = get_asset_by_uid(uid, assets)
    if not _is_non_media_tabletop_item(asset, uid, placement):
        return ""

    pos, _, width, depth, _ = extract_placement(placement, asset)
    best_uid = ""
    best_score: tuple[int, float] | None = None
    for candidate_uid, candidate_placement in layout.items():
        if candidate_uid == uid:
            continue
        candidate_asset = get_asset_by_uid(candidate_uid, assets)
        candidate_category = candidate_asset.get("category", "")
        if is_table_lamp_asset(asset.get("category", ""), uid):
            eligible = is_table_lamp_support_asset(candidate_category, candidate_uid)
        else:
            eligible = _is_tabletop_support_asset(candidate_asset, candidate_uid)
        if not eligible:
            continue
        candidate_pos, _, candidate_width, candidate_depth, _ = extract_placement(
            candidate_placement,
            candidate_asset,
        )
        if not _support_footprint_fits(
            width,
            depth,
            candidate_width,
            candidate_depth,
        ):
            continue
        distance = math.hypot(pos[0] - candidate_pos[0], pos[1] - candidate_pos[1])
        score = (
            tabletop_support_priority(candidate_category, candidate_uid),
            distance,
        )
        if best_score is None or score < best_score:
            best_uid = candidate_uid
            best_score = score
    return best_uid

def _supported_parent_uid(
    uid: str,
    placement: dict[str, Any],
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> str:
    asset = get_asset_by_uid(uid, assets)
    if _is_media_display_asset(asset, uid) and _placement_mode(asset, uid) in {
        "floor",
        "wall_mounted",
        "ceiling_mounted",
    }:
        return ""
    parent = _parent_uid(placement)
    preferred_parent = str(
        asset.get("paired_support_uid") or asset.get("support_uid") or ""
    ).strip()
    if (
        (not parent or parent == uid or parent not in layout)
        and preferred_parent
        and preferred_parent != uid
        and preferred_parent in layout
    ):
        parent = preferred_parent
    if not parent or parent == uid or parent not in layout:
        return _nearest_media_support_uid(
            uid,
            placement,
            layout,
            assets,
        ) or _nearest_tabletop_support_uid(uid, placement, layout, assets)

    parent_asset = get_asset_by_uid(parent, assets)
    _, _, width, depth, _ = extract_placement(placement, asset)
    _, _, parent_width, parent_depth, _ = extract_placement(
        layout[parent],
        parent_asset,
    )
    if _is_media_display_asset(asset, uid):
        if _is_supported_media_pair(
            asset,
            uid,
            parent_asset,
            parent,
        ) and _support_footprint_fits(width, depth, parent_width, parent_depth):
            return parent
        return _nearest_media_support_uid(uid, placement, layout, assets)
    if not _is_wall_mounted_layout_asset(asset, uid):
        tabletop_item = _is_non_media_tabletop_item(asset, uid, placement)
        if tabletop_item:
            if not _is_tabletop_support_asset(
                parent_asset,
                parent,
            ) or not _support_footprint_fits(
                width,
                depth,
                parent_width,
                parent_depth,
            ):
                return _nearest_tabletop_support_uid(uid, placement, layout, assets)
            parent_rank = tabletop_support_priority(parent_asset.get("category", ""), parent)
            if parent_rank >= 3:
                better_parent = _nearest_tabletop_support_uid(
                    uid,
                    placement,
                    layout,
                    assets,
                )
                if better_parent and better_parent != parent:
                    better_asset = get_asset_by_uid(better_parent, assets)
                    if (
                        tabletop_support_priority(
                            better_asset.get("category", ""),
                            better_parent,
                        )
                        < parent_rank
                    ):
                        return better_parent
        return parent

    return ""

def _resolve_stack_chain(layout: dict[str, Any]) -> list[str]:
    remaining = set(layout.keys())
    ordered: list[str] = []
    while remaining:
        progress = False
        for uid in sorted(remaining):
            parent = _parent_uid(layout[uid])
            if not parent or parent not in layout or parent in set(ordered):
                ordered.append(uid)
                remaining.discard(uid)
                progress = True
        if not progress:
            ordered.extend(sorted(remaining))
            break
    return ordered

def _best_coffee_table_candidate(
    sofa_uid: str,
    sofa_asset: dict[str, Any],
    sofa_placement: dict[str, Any],
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    sofa_pos, sofa_rot_z, sofa_width, sofa_depth, _ = extract_placement(
        sofa_placement,
        sofa_asset,
    )
    sofa_poly = asset_polygon(sofa_pos, sofa_rot_z, sofa_width, sofa_depth)
    axis, sign = _facing_axis_and_sign(sofa_rot_z)
    target_gap = sum(SOFA_COFFEE_TABLE_DISTANCE_RANGE_M) / 2

    best = None
    best_score = None
    for item in floor_assets(layout, assets, keywords=COFFEE_TABLE_ROLE_KEYWORDS):
        if item.uid == sofa_uid:
            continue
        uid, pos, poly = item.uid, item.pos, item.poly
        forward = sign * (
            (pos[0] - sofa_pos[0]) if axis == "x" else (pos[1] - sofa_pos[1])
        )
        lateral = abs(
            (pos[1] - sofa_pos[1]) if axis == "x" else (pos[0] - sofa_pos[0])
        )
        sofa_min_x, sofa_min_y, sofa_max_x, sofa_max_y = sofa_poly.bounds
        poly_min_x, poly_min_y, poly_max_x, poly_max_y = poly.bounds
        gap = (
            forward
            - (
                (sofa_max_x - sofa_min_x) / 2
                if axis == "x"
                else (sofa_max_y - sofa_min_y) / 2
            )
            - (
                (poly_max_x - poly_min_x) / 2
                if axis == "x"
                else (poly_max_y - poly_min_y) / 2
            )
        )
        score = (
            0 if forward > 0 else 1,
            lateral,
            abs(gap - target_gap),
            abs(forward),
        )
        if best_score is None or score < best_score:
            best_score = score
            best = {
                "table": uid,
                "gap": float(gap),
                "forward": float(forward),
                "lateral": float(lateral),
                "axis": axis,
                "sign": sign,
            }
    return best

def _is_tabletop_display_asset(
    category: str = "",
    uid: str = "",
    asset: dict[str, Any] | None = None,
) -> bool:
    asset = asset or {"category": category, "uid": uid}
    return _placement_mode(asset, uid) == "tabletop" or is_table_lamp_asset(
        category,
        uid,
    )

def _is_tabletop_support_asset(parent_asset: dict[str, Any], parent_uid: str) -> bool:
    parent_category = parent_asset.get("category", "")
    if _is_tabletop_display_asset(parent_category, parent_uid):
        return False
    return is_tabletop_support_asset(parent_category, parent_uid)

def _inset_support_polygon(
    parent_poly: Polygon,
    parent_width: float,
    parent_depth: float,
) -> Polygon:
    max_inset = max(0.0, min(float(parent_width), float(parent_depth)) / 2 - 0.01)
    inset = min(TABLETOP_SUPPORT_INSET_M, max_inset)
    if inset <= 0:
        return parent_poly
    inset_poly = parent_poly.buffer(-inset)
    return parent_poly if inset_poly.is_empty else inset_poly
