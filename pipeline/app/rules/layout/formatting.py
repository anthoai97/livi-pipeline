"""Prompt-facing layout and issue formatting."""

from typing import Any
import json
from app.rules.layout.bedroom import BEDROOM_ISSUE_KEYS
from app.rules.layout.constants import DINING_ISSUE_KEYS
from app.rules.layout.studio import STUDIO_ISSUE_KEYS
from app.rules.geometry.primitives import extract_placement, projected_half_extents
from app.rules.protected_paths import PROTECTED_PATH_WIDTH_M

from app.rules.layout.metrics import (
    get_asset_by_uid,
)

def format_issues(issues: dict[str, Any]) -> str:
    lines = []
    for key in STUDIO_ISSUE_KEYS:
        for finding in issues.get(key, []):
            lines.append(f"- STUDIO {key}: {json.dumps(finding, sort_keys=True)}")
    for key in BEDROOM_ISSUE_KEYS:
        for finding in issues.get(key, []):
            lines.append(f"- BEDROOM {key}: {json.dumps(finding, sort_keys=True)}")
    for key in DINING_ISSUE_KEYS:
        for finding in issues.get(key, []):
            lines.append(f"- DINING {key}: {json.dumps(finding, sort_keys=True)}")
    for uid_a, uid_b in issues["overlaps"]:
        lines.append(f"- OVERLAP: {uid_a} and {uid_b} are intersecting")
    for uid in issues["boundary_violations"]:
        lines.append(f"- BOUNDARY: {uid} extends outside the room")
    for uid, door_idx in issues["door_violations"]:
        lines.append(
            f"- DOOR CLEARANCE: {uid} is inside the doorway clear zone for door #{door_idx}"
        )
    for uid in issues.get("obstacle_violations", []):
        lines.append(f"- OBSTACLE: {uid} intersects a window")
    for uid in issues.get("wall_mount_violations", []):
        lines.append(
            f"- WALL MOUNT: {uid} is not snapped to a wall at the correct height"
        )
    for uid in issues.get("ceiling_mount_violations", []):
        lines.append(f"- CEILING MOUNT: {uid} is not hanging at the ceiling height")
    for uid in issues.get("wall_aligned_violations", []):
        lines.append(
            f"- WALL ALIGNMENT: {uid} must stay flush to a wall with non-diagonal rotation"
        )
    for item in issues.get("protected_path_violations", []):
        wall_pair = "↔".join(item.get("wall_pair") or [])
        path_bounds = item.get("path_bounds") or {}
        asset_bounds = item.get("asset_bounds") or {}
        translations = item.get("translations_to_clear_m") or {}
        lines.append(
            "- PROTECTED PATH: "
            f"{item['uid']} overlaps the {wall_pair or item.get('path_id')} "
            f"door-to-door path by {item['overlap_area_sqm']:.2f} sqm; "
            f"keep the {item.get('required_width_m', PROTECTED_PATH_WIDTH_M):.2f}m route clear. "
            f"Path bounds x=[{path_bounds.get('min_x', 0.0):.2f}, "
            f"{path_bounds.get('max_x', 0.0):.2f}], "
            f"y=[{path_bounds.get('min_y', 0.0):.2f}, "
            f"{path_bounds.get('max_y', 0.0):.2f}]; "
            f"asset bounds x=[{asset_bounds.get('min_x', 0.0):.2f}, "
            f"{asset_bounds.get('max_x', 0.0):.2f}], "
            f"y=[{asset_bounds.get('min_y', 0.0):.2f}, "
            f"{asset_bounds.get('max_y', 0.0):.2f}]. "
            "Exact center translations that fully clear the route are "
            f"left {translations.get('left', 0.0):.2f}m, "
            f"right +{translations.get('right', 0.0):.2f}m, "
            f"down {translations.get('down', 0.0):.2f}m, or "
            f"up +{translations.get('up', 0.0):.2f}m; choose a feasible "
            "direction for the whole functional group."
        )
    for item in issues.get("sofa_coffee_table_violations", []):
        table_translation = item.get("table_center_translation_to_valid_gap_m") or {}
        sofa_translation = item.get("sofa_center_translation_to_valid_gap_m") or {}
        lines.append(
            "- SOFA/TABLE GAP: "
            f"{item['sofa']} ↔ {item['table']} is {item['gap']:.2f}m, expected "
            f"{item['min_gap']:.2f}–{item['max_gap']:.2f}m. "
            f"To make the gap valid without estimating rotated bounds, translate "
            f"the table center on world {table_translation.get('axis', item.get('axis'))} "
            f"by [{table_translation.get('min', 0.0):+.2f}, "
            f"{table_translation.get('max', 0.0):+.2f}]m, or translate the sofa "
            f"center on that axis by [{sofa_translation.get('min', 0.0):+.2f}, "
            f"{sofa_translation.get('max', 0.0):+.2f}]m"
        )
    for item in issues.get("sofa_wall_gap_violations", []):
        if item.get("kind") == "window_clearance":
            lines.append(
                "- SOFA/WINDOW CLEARANCE: "
                f"{item['uid']} blocks the window clearance band on the "
                f"{item['back_wall']} wall"
            )
        else:
            lines.append(
                "- SOFA/WALL GAP: "
                f"{item['uid']} back gap is {item['gap']:.2f}m, expected "
                f"{item['min_gap']:.2f}–{item['max_gap']:.2f}m"
            )
    for item in issues.get("media_focal_alignment_violations", []):
        if item.get("kind") == "wall":
            lines.append(
                "- MEDIA ALIGNMENT: "
                f"{item['media']} should sit on the {item['expected_wall']} wall in front of "
                f"{item['sofa']}, not the {item['actual_wall']} wall"
            )
        elif item.get("kind") == "centerline":
            lines.append(
                "- MEDIA CENTERLINE: "
                f"{item['media']} is {item['offset']:.2f}m off {item['sofa']}'s axis, "
                f"max {item['max_offset']:.2f}m"
            )
    for item in issues.get("media_group_violations", []):
        if item.get("kind") == "media_support_floating":
            lines.append("- MEDIA PLACEMENT (advisory, assess exposed back and divider purpose): " + json.dumps(item))
        elif item.get("kind") == "tv_viewing_distance":
            lines.append("- TV COMFORT (advisory, estimated viewer/screen points): " + json.dumps(item))
        elif item.get("kind") == "seating_media_distance":
            lines.append(
                "- MEDIA VIEWING DISTANCE: "
                f"{item['sofa']} is {item['distance']:.2f}m from "
                f"{item['media']}, max {item['max_distance']:.2f}m"
            )
        elif item.get("kind") == "display_support_centerline":
            lines.append(
                "- MEDIA SUPPORT CENTERLINE: "
                f"{item['display']} is {item['offset']:.2f}m off {item['support']}, "
                f"max {item['max_offset']:.2f}m"
            )
        elif item.get("kind") == "display_support_rotation":
            lines.append(
                "- MEDIA SUPPORT ROTATION: "
                f"{item['display']} is rotated {item['delta_deg']:.0f}deg from "
                f"{item['support']}, max {item['max_delta_deg']:.0f}deg"
            )
        else:
            lines.append(
                "- MEDIA SUPPORT GROUP: "
                f"{item['display']} is {item['gap']:.2f}m from {item['support']}, "
                f"max {item['max_gap']:.2f}m"
            )
    for item in issues.get("coffee_table_axis_violations", []):
        lines.append(
            "- TABLE AXIS: "
            f"{item['table']} long axis drifts from {item['sofa']}'s long axis by "
            f"{item['delta']:.2f}rad, max {item['max_delta']:.2f}rad"
        )
    for item in issues.get("living_group_cohesion_violations", []):
        if item.get("kind") == "sofa_console_access":
            lines.append("- SOFA FRONT ACCESS (critical, media console blocks service band): " + json.dumps(item))
        elif item.get("kind") == "seat_off_rug":
            lines.append(
                "- LIVING GROUP RUG: "
                f"{item['seat']} is {item['gap']:.2f}m off {item['rug']}, "
                f"max {item['max_gap']:.2f}m"
            )
        elif item.get("kind") == "coffee_table_off_rug":
            lines.append(
                "- LIVING GROUP RUG: "
                f"{item['table']} is {item['gap']:.2f}m off {item['rug']}, "
                f"max {item['max_gap']:.2f}m"
            )
        elif item.get("kind") in {"seat_faces_away", "seat_misses_table"}:
            lines.append(
                "- LIVING CHAIR ORIENTATION: "
                f"{item['seat']} front ray misses {item['table']}'s footprint by "
                f"{item['front_ray_miss_m']:.2f}m, max "
                f"{item['max_front_ray_miss_m']:.2f}m; "
                f"table-center facing delta {item['facing_delta_deg']:.0f}deg, "
                f"target yaw {item['target_yaw_rad']:.3f}rad. "
                "Aim the editable chair into its table/seating group and recheck clearance."
            )
        else:
            lines.append(
                "- LIVING GROUP DISTANCE: "
                f"{item['seat']} is {item['gap']:.2f}m from {item['table']}, "
                f"max {item['max_gap']:.2f}m"
            )
    for item in issues.get("rug_composition_violations", []):
        lines.append("- RUG COMPOSITION (advisory, respect its functional group): " + json.dumps(item))
    for item in issues.get("dining_group_violations", []):
        if item.get("kind") == "chair_misses_table":
            lines.append(
                f"- DINING TABLETOP AIM: {item['chair']}'s front misses {item['table']} by "
                f"{item['front_ray_miss_m']:.2f}m. Place chairs along usable table edges, "
                "not past its corners. Check table long-axis rotation before distributing seats."
            )
            continue
        lines.append(
            "- DINING CHAIR GROUP: "
            f"{item['chair']} must face and stay near {item['table']}; "
            f"gap {item['gap']:.2f}m/max {item['max_gap']:.2f}m, "
            f"facing {item['facing_delta_deg']:.0f}deg/max {item['max_delta_deg']:.0f}deg"
        )
    for item in issues.get("living_dining_clearance_violations", []):
        lines.append(
            "- LIVING/DINING CLEARANCE: "
            f"{item['dining_table']} is {item['gap']:.2f}m from "
            f"{item['living_item']}, expected at least {item['min_gap']:.2f}m"
        )
    for item in issues.get("task_chair_orientation_violations", []):
        lines.append(
            "- TASK CHAIR GROUP: "
            f"{item['chair']} must face and stay near {item['desk']}; "
            f"gap {item['gap']:.2f}m/max {item['max_gap']:.2f}m, "
            f"facing {item['facing_delta_deg']:.0f}deg/max {item['max_delta_deg']:.0f}deg"
        )
    for item in issues.get("side_table_reach_violations", []):
        if item.get("facing_delta_deg", 0.0) > item.get("max_facing_delta_deg", 180.0):
            lines.append(
                "- SIDE TABLE REACH: "
                f"{item['table']} is behind {item['seat']} "
                f"({item['facing_delta_deg']:.0f}deg from the seating direction)"
            )
        else:
            lines.append(
                "- SIDE TABLE REACH: "
                f"{item['table']} is {item['gap']:.2f}m from {item['seat']}, max "
                f"{item['max_gap']:.2f}m"
            )
    for item in issues.get("floor_lamp_reach_violations", []):
        if item.get("reason") == "wall_proximity":
            lines.append(
                "- FLOOR LAMP WALL PROXIMITY: "
                f"{item['lamp']} is reachable from {item['seat']} but "
                f"{item['wall_gap']:.2f}m from the nearest wall, max "
                f"{item['max_wall_gap']:.2f}m"
            )
        else:
            lines.append(
                "- FLOOR LAMP REACH: "
                f"{item['lamp']} is {item['gap']:.2f}m from {item['seat']}, expected "
                f"{item['min_gap']:.2f}–{item['max_gap']:.2f}m and within "
                f"{item['max_wall_gap']:.2f}m of a wall"
            )
    for item in issues.get("table_lamp_support_violations", []):
        parent = item.get("parent") or "no support"
        item_uid = item.get("item") or item.get("lamp")
        if item.get("reason") == "support_edge":
            lines.append(
                "- TABLETOP SUPPORT: "
                f"{item_uid} must keep its full footprint inset on {parent}; "
                "move it away from the rear/edge of the support surface"
            )
        elif item.get("reason") == "support_height":
            lines.append(
                "- TABLETOP SUPPORT HEIGHT: "
                f"{item_uid} is at z={item.get('z'):.2f}m on {parent}; expected "
                f"z={item.get('expected_z'):.2f}m"
            )
        elif item.get("reason") == "surface_overlap":
            lines.append(
                "- TABLETOP SURFACE OVERLAP: "
                f"{item_uid} overlaps {item.get('other')} on {parent}; "
                "separate objects sharing the same tabletop/console surface"
            )
        else:
            lines.append(
                "- TABLETOP SUPPORT: "
                f"{item_uid} must sit on a side table, nightstand, desk, console, "
                f"shelf, or table; current support is {parent}"
            )
    for item in issues.get("service_corridor_violations", []):
        lines.append(
            "- SERVICE CORRIDOR: "
            f"{item['uid']} is between {item['sofa']} and {item['table']}"
        )
    return "\n".join(lines) if lines else "No issues."

def format_layout(layout: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    lines = []
    for uid, placement in layout.items():
        asset = get_asset_by_uid(uid, assets)
        pos, rot_z, width, depth, _ = extract_placement(placement, asset)
        half_x, half_y = projected_half_extents(width, depth, rot_z)
        xmin, xmax = pos[0] - half_x, pos[0] + half_x
        ymin, ymax = pos[1] - half_y, pos[1] + half_y
        lines.append(
            f"- {uid}: pos=[{pos[0]:.2f}, {pos[1]:.2f}], W×D={width:.2f}×{depth:.2f}m, "
            f"rot_z={rot_z:.2f}rad, bbox=[{xmin:.2f}, {ymin:.2f}, {xmax:.2f}, {ymax:.2f}]"
        )
    return "\n".join(lines)
