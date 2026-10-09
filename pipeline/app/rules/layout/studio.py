"""Read-only studio group ownership, completeness and shared viewing geometry."""

import math
from typing import Any

from app.rules.geometry.primitives import extract_placement
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.taxonomy import normalize_category

STUDIO_ISSUE_KEYS = ("studio_completeness_violations", "studio_tv_viewing_violations")
SITTING_CATEGORIES = frozenset({"sofa", "loveseat", "sectional", "sleeper_sofa"})


def is_freestanding_studio_media_support(asset: dict[str, Any]) -> bool:
    """Studio media can divide groups without being forced onto a perimeter wall."""
    return (
        asset.get("viewing_target") in {"sitting", "bed", "shared"}
        and normalize_category(asset.get("category")) in {"tv_stand", "media_unit", "media_console", "console_table"}
        and placement_mode_for_asset(asset) == "floor"
    )


def accessory_group(layout: dict[str, Any], assets: list[dict[str, Any]], uid: str) -> str | None:
    """Use the selected group, or measure the nearest anchor for unassigned assets.

    This measures the actual arrangement, not a room-wide largest-rug assumption.
    It never removes another group's furniture from collision/access checks.
    """
    if uid not in layout:
        return None
    asset_map = {str(a.get("instance_key") or a.get("uid") or ""): a for a in assets}
    assigned = asset_map.get(uid, {}).get("functional_group")
    if assigned in {"sleeping", "sitting", "dining"}:
        return assigned
    pos = extract_placement(layout[uid], asset_map.get(uid, {}))[0]
    candidates = []
    for key, asset in asset_map.items():
        category = normalize_category(asset.get("category"))
        group = "sleeping" if category == "bed" else "dining" if category == "dining_table" else "sitting" if category in SITTING_CATEGORIES else None
        if group and key in layout:
            anchor_pos = extract_placement(layout[key], asset)[0]
            candidates.append((math.dist(pos[:2], anchor_pos[:2]), key, group))
    return min(candidates)[2] if candidates else None


def studio_layout_measurements(layout: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any]:
    records = []
    groups = {"sleeping": [], "sitting": [], "dining": []}
    violations: dict[str, list] = {key: [] for key in STUDIO_ISSUE_KEYS}
    for asset in assets:
        uid = str(asset.get("instance_key") or asset.get("uid") or "")
        if uid not in layout:
            continue
        category = normalize_category(asset.get("category"))
        pos, yaw, _, _, _ = extract_placement(layout[uid], asset)
        record = {"uid": uid, "category": category, "pos": pos, "yaw": yaw, "asset": asset}
        records.append(record)
        group = "sleeping" if category == "bed" else "sitting" if category in SITTING_CATEGORIES else "dining" if category in {"dining_table", "dining_chair"} else None
        if group:
            groups[group].append(uid)
    for group in ("sleeping", "sitting"):
        if not groups[group] or (group == "sleeping" and len(groups[group]) != 1):
            violations["studio_completeness_violations"].append({
                "kind": "missing_or_invalid_core_group", "group": group, "uids": groups[group],
            })
    viewing = []
    for display in (row for row in records if row["category"] == "tv"):
        target = display["asset"].get("viewing_target") or "sitting"
        # Bed viewing is measured by the shared bedroom access/viewing analyzer.
        if target == "bed":
            continue
        seats = [row for row in records if row["category"] in SITTING_CATEGORIES]
        if not seats:
            continue
        seat = min(seats, key=lambda row: math.dist(row["pos"][:2], display["pos"][:2]))
        deltas = []
        target_headings = []
        for source, destination in ((seat, display), (display, seat)):
            direction = math.atan2(destination["pos"][1] - source["pos"][1], destination["pos"][0] - source["pos"][0])
            target_headings.append(direction % math.tau)
            deltas.append(math.degrees(abs((source["yaw"] - direction + math.pi) % math.tau - math.pi)))
        row = {"uid": display["uid"], "seat": seat["uid"], "target": target,
               "delta_deg": round(max(deltas), 4), "max_delta_deg": 45.0,
               "seat_delta_deg": round(deltas[0], 4), "display_delta_deg": round(deltas[1], 4),
               "seat_yaw_rad": seat["yaw"], "display_yaw_rad": display["yaw"],
               "seat_target_yaw_rad": target_headings[0], "display_target_yaw_rad": target_headings[1]}
        viewing.append(row)
        if max(deltas) > 45.0 + 1e-6:
            violations["studio_tv_viewing_violations"].append({
                **row, "kind": "tv_not_viewable_from_sitting",
                "repair": "Face the sofa and the complete TV/support group toward each other. Perpendicular headings require an exact diagonal; use opposing headings and reposition the group with clear access.",
            })
    return {"groups": groups, "tv_viewing": viewing, "violations": violations,
            "viewing_targets": {row["uid"]: row["asset"].get("viewing_target", "sitting")
                                for row in records if row["category"] == "tv"},
            "accessory_groups": {row["uid"]: row["asset"]["functional_group"] for row in records
                                 if row["asset"].get("functional_group")}}
