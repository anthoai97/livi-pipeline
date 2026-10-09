"""Read-only media comfort and group-owned rug composition evidence.

Viewing points and preferred ranges are product design heuristics, not measured
eye positions or accessibility standards. Only console intrusion into the sofa's
front service band is a blocking functional finding; composition stays advisory.
"""

import math
from typing import Any

from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon, box

from app.rules.categories import matches_category_keywords
from app.rules.geometry.primitives import asset_polygon, extract_placement
from app.rules.layout.constants import MEDIA_SUPPORT_ROLE_KEYWORDS
from app.rules.layout.bedroom import is_bedside_asset
from app.rules.layout.studio import SITTING_CATEGORIES, accessory_group
from app.rules.planner.taxonomy import normalize_category

TV_VIEW_WIDTH_RANGE = (1.2, 3.5)
SOFA_MEDIA_ACCESS_MIN_M = 0.56
SITTING_RUG_MIN_DEPTH_M = 0.90
SITTING_RUG_MIN_WIDTH_RATIO = 0.90


def sitting_rug_selection_errors(assets: list[dict[str, Any]]) -> list[str]:
    """Reject a sitting rug only when neither orientation can serve any selected sofa."""
    sofas = [a for a in assets if normalize_category(a.get("category")) in SITTING_CATEGORIES]
    errors = []
    for rug in assets:
        if normalize_category(rug.get("category")) != "rug" or rug.get("functional_group") != "sitting" or not sofas:
            continue
        width, depth = float(rug.get("width") or 0), float(rug.get("depth") or 0)
        if any(lateral >= SITTING_RUG_MIN_WIDTH_RATIO * float(sofa.get("width") or 0) - 1e-6
               and forward >= SITTING_RUG_MIN_DEPTH_M - 1e-6
               for sofa in sofas for lateral, forward in ((width, depth), (depth, width))):
            continue
        errors.append(
            f"SITTING RUG FIT: {rug.get('uid')} ({width:.3f}m x {depth:.3f}m) cannot span "
            f"{SITTING_RUG_MIN_WIDTH_RATIO:.0%} of a selected sofa's width AND provide "
            f"{SITTING_RUG_MIN_DEPTH_M:.2f}m depth in either rotation. Choose a fitting area rug; "
            "a narrow runner cannot serve this sitting group. Sleeping runners remain allowed."
        )
    return errors


def comfort_placement_facts(assets: list[dict[str, Any]], room_type: str) -> dict[str, Any]:
    return {
        "sofa_console_front_clearance_m": SOFA_MEDIA_ACCESS_MIN_M,
        "estimated_viewing_points": {
            "sitting": "sofa center minus 0.25 * depth along FRONT",
            "bed": "head edge plus 0.40m toward the foot",
            "screen": "TV center plus half its depth along FRONT",
        },
        "tv_preferred_ranges": [
            {"uid": a.get("instance_key") or a.get("uid"),
             "viewing_target": a.get("viewing_target") or ("bed" if room_type == "bedroom" else "sitting"),
             "estimated_view_distance_range_m": [round(float(a["width"]) * factor, 4) for factor in TV_VIEW_WIDTH_RANGE]}
            for a in assets if normalize_category(a.get("category")) == "tv" and float(a.get("width") or 0) > 0
        ],
        "rug_groups": [
            {"rug": a.get("instance_key") or a.get("uid"), "functional_group": a["functional_group"]}
            for a in assets if normalize_category(a.get("category")) == "rug"
            and a.get("functional_group") in {"sitting", "sleeping", "dining"}
        ],
        "bedside_tables": [a.get("instance_key") or a.get("uid") for a in assets if is_bedside_asset(a)],
    }


def _records(layout: dict[str, Any], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for asset in assets:
        uid = str(asset.get("instance_key") or asset.get("uid") or "")
        if uid not in layout:
            continue
        pos, yaw, width, depth, height = extract_placement(layout[uid], asset)
        records.append({"uid": uid, "asset": asset, "category": normalize_category(asset.get("category")),
                        "pos": pos, "yaw": yaw, "width": width, "depth": depth, "height": height,
                        "poly": asset_polygon(pos, yaw, width, depth)})
    return records


def _point(record: dict[str, Any], forward: float) -> list[float]:
    return [record["pos"][0] + forward * math.cos(record["yaw"]),
            record["pos"][1] + forward * math.sin(record["yaw"])]


def _local_polygon(poly, anchor: dict[str, Any]):
    c, s = math.cos(anchor["yaw"]), math.sin(anchor["yaw"])
    x, y = anchor["pos"][:2]
    return affine_transform(poly, [c, s, -s, c, -x * c - y * s, x * s - y * c])


def media_viewing_measurements(
    layout: dict[str, Any], assets: list[dict[str, Any]], room_type: str = "living_room",
) -> list[dict[str, Any]]:
    records = _records(layout, assets)
    result = []
    for display in (r for r in records if r["category"] == "tv"):
        target = display["asset"].get("viewing_target") or ("bed" if room_type == "bedroom" else "sitting")
        for group in (("sitting", "bed") if target == "shared" else (target,)):
            viewers = [r for r in records if (r["category"] == "bed" if group == "bed" else r["category"] in SITTING_CATEGORIES)]
            if not viewers:
                continue
            viewer = min(viewers, key=lambda r: math.dist(r["pos"][:2], display["pos"][:2]))
            offset = -viewer["depth"] / 2 + 0.40 if group == "bed" else -0.25 * viewer["depth"]
            eye, screen = _point(viewer, offset), _point(display, display["depth"] / 2)
            distance = math.dist(eye, screen)
            direction = math.atan2(display["pos"][1] - viewer["pos"][1], display["pos"][0] - viewer["pos"][0])
            angle = lambda yaw: math.degrees(abs((yaw + math.pi) % math.tau - math.pi))
            viewer_delta, display_delta = angle(viewer["yaw"] - direction), angle(display["yaw"] - direction - math.pi)
            low, high = (display["width"] * factor for factor in TV_VIEW_WIDTH_RANGE)
            row = {"viewer": viewer["uid"], "media": display["uid"], "media_uid": display["uid"],
                   "viewer_group": group, "viewing_target": target,
                   "center_distance_m": round(math.dist(viewer["pos"][:2], display["pos"][:2]), 4),
                   "estimated_viewing_point_xy": [round(v, 4) for v in eye],
                   "estimated_screen_point_xy": [round(v, 4) for v in screen],
                   "estimated_view_distance_m": round(distance, 4), "screen_width_m": display["width"],
                   "preferred_distance_range_m": [round(low, 4), round(high, 4)],
                   "within_preferred_range": low - 1e-6 <= distance <= high + 1e-6,
                   "distance_outside_preferred_range_m": round(max(low - distance, distance - high, 0.0), 4),
                   "viewer_facing_delta_deg": round(viewer_delta, 4), "media_facing_delta_deg": round(display_delta, 4),
                   "viewer_faces_media": viewer_delta <= 45.0, "media_faces_viewer": display_delta <= 45.0}
            result.append(row)
    return result


def comfort_layout_measurements(
    layout: dict[str, Any], assets: list[dict[str, Any]], room_type: str = "living_room",
    *, boundary: list[list[float]] | None = None,
) -> dict[str, Any]:
    records = _records(layout, assets)
    viewing = media_viewing_measurements(layout, assets, room_type)
    violations = {"media_group_violations": [], "living_group_cohesion_violations": [], "rug_composition_violations": []}
    for row in viewing:
        if not row["within_preferred_range"]:
            violations["media_group_violations"].append({**row, "kind": "tv_viewing_distance"})

    console_access = []
    # Specific catalog categories win over retained IDs after reclassification.
    supports = [r for r in records if matches_category_keywords(
                    r["category"], r["uid"] if r["category"] in {"", "unknown"} else "", MEDIA_SUPPORT_ROLE_KEYWORDS)
                and r["asset"].get("placement_mode") not in {"wall", "wall_mounted"}]
    for sofa in (r for r in records if r["category"] in SITTING_CATEGORIES):
        front, half_width = sofa["depth"] / 2, sofa["width"] / 2
        band = box(front, -half_width, front + SOFA_MEDIA_ACCESS_MIN_M, half_width)
        for support in supports:
            if support["pos"][2] >= sofa["pos"][2] + sofa["height"]:
                continue
            local = _local_polygon(support["poly"], sofa)
            forward = local.intersection(box(front, -half_width, max(front, local.bounds[2]) + 1, half_width))
            if forward.area <= 1e-6:
                continue
            gap = max(0.0, forward.bounds[0] - front)
            blocked = local.intersection(band).area > 1e-6
            region = asset_polygon(_point(sofa, front + SOFA_MEDIA_ACCESS_MIN_M / 2), sofa["yaw"],
                                   sofa["width"], SOFA_MEDIA_ACCESS_MIN_M)
            row = {"sofa": sofa["uid"], "support": support["uid"], "gap": round(gap, 4),
                   "min_gap": SOFA_MEDIA_ACCESS_MIN_M, "clear": not blocked,
                   "region": list(region.exterior.coords)[:-1]}
            console_access.append(row)
            if blocked:
                violations["living_group_cohesion_violations"].append({**row, "kind": "sofa_console_access"})

    media_placement = []
    if room_type == "studio" and boundary:
        room = Polygon(boundary)
        wall_points = list(room.exterior.coords)
        for support in supports:
            # Measure the entire rear edge against an actual wall segment, not
            # the room bounding box. A freestanding support remains permitted.
            rear = asset_polygon(_point(support, -support["depth"] / 2), support["yaw"],
                                 support["width"], 0.0001)
            rear_gap = min(max(LineString([a, b]).distance(Point(p)) for p in rear.exterior.coords)
                           for a, b in zip(wall_points, wall_points[1:]))
            behind = [r["uid"] for r in records
                      if r["category"] in SITTING_CATEGORIES | {"bed", "dining_table"}
                      and _local_polygon(r["poly"], support).centroid.x < -support["depth"] / 2]
            row = {"support": support["uid"], "rear_wall_gap_m": round(rear_gap, 4),
                   "wall_backed": rear_gap <= 0.20 + 1e-6, "max_wall_gap_m": 0.20,
                   "groups_behind_support": behind}
            media_placement.append(row)
            if not row["wall_backed"]:
                violations["media_group_violations"].append({**row, "kind": "media_support_floating"})

    rugs = []
    for rug in (r for r in records if r["category"] == "rug"):
        group = accessory_group(layout, assets, rug["uid"])
        anchors = [r for r in records if (r["category"] == "bed" if group == "sleeping"
                   else r["category"] in SITTING_CATEGORIES if group == "sitting" else False)]
        if not anchors:
            continue  # Dining coverage is measured by the dining analyzer.
        anchor = min(anchors, key=lambda r: r["poly"].distance(rug["poly"]))
        local = _local_polygon(rug["poly"], anchor)
        lo, left, hi, right = local.bounds
        front, half_width = anchor["depth"] / 2, anchor["width"] / 2
        lateral_overlap = max(0.0, min(right, half_width) - max(left, -half_width))
        center_lateral = local.centroid.y
        # Side runners/bedside rugs need not behave like a centered area rug.
        area_rug = lateral_overlap >= anchor["width"] * 0.5 and abs(center_lateral) <= half_width
        forward_area = local.intersection(box(front, left - 1, max(front, hi) + 1, right + 1)).area
        forward_ratio = forward_area / local.area if local.area else 0.0
        underlap = max(0.0, min(hi, front) - max(lo, -front))
        extension = max(0.0, hi - front)
        row = {"rug": rug["uid"], "anchor": anchor["uid"], "functional_group": group,
               "area_rug": area_rug, "anchor_local_bounds_m": [round(x, 4) for x in local.bounds],
               "anchor_front_gap_m": round(max(0.0, lo - front), 4),
               "anchor_depth_overlap_m": round(underlap, 4), "forward_extension_m": round(extension, 4),
               "area_ahead_of_anchor_front_ratio": round(forward_ratio, 4),
               "lateral_span_to_anchor_width_ratio": round((right - left) / anchor["width"], 4)}
        rugs.append(row)
        if group == "sitting" and ((right - left) < anchor["width"] * SITTING_RUG_MIN_WIDTH_RATIO or hi - lo < SITTING_RUG_MIN_DEPTH_M):
            violations["rug_composition_violations"].append({**row, "kind": "sitting_rug_undersized",
                "min_lateral_span_ratio": SITTING_RUG_MIN_WIDTH_RATIO, "min_depth_m": SITTING_RUG_MIN_DEPTH_M})
        if group == "sitting":
            other_groups = [r["uid"] for r in records if r["category"] in {"bed", "dining_table"}
                            and rug["poly"].intersection(r["poly"]).area > r["poly"].area * 0.10]
            if other_groups:
                violations["rug_composition_violations"].append({**row, "kind": "sitting_rug_crosses_groups",
                    "other_anchors": other_groups, "max_other_anchor_coverage": 0.10})
        if group == "sitting" and lo - front > 0.10:
            violations["rug_composition_violations"].append({**row, "kind": "sitting_rug_detached",
                "gap": round(lo - front, 4), "max_gap": 0.10})
        elif area_rug and group == "sitting" and (extension < 0.30 or forward_ratio < 0.35):
            violations["rug_composition_violations"].append({**row, "kind": "sitting_rug_behind_sofa",
                "min_forward_extension_m": 0.30, "min_forward_area_ratio": 0.35})
        elif area_rug and group == "sleeping" and extension > underlap and forward_ratio > 0.50:
            violations["rug_composition_violations"].append({**row, "kind": "sleeping_rug_beyond_foot"})
    return {"tv_viewing": viewing, "sofa_console_access": console_access,
            "media_placement": media_placement, "rugs": rugs, "violations": violations}
