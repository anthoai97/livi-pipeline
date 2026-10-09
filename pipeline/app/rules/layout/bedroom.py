"""Read-only bedroom geometry measurements; never generate or repair poses."""

import math
from typing import Any

from shapely.geometry import LineString, Point, Polygon

from app.rules.geometry.primitives import asset_polygon, extract_placement, room_polygon, service_center_bounds
from app.rules.planner.taxonomy import normalize_category

BED_ACCESS_MIN_M = 0.56
BED_HEADBOARD_MAX_GAP_M = 0.20
BEDSIDE_HEAD_ZONE_M = 0.65
BEDSIDE_MAX_GAP_M = 0.30
BEDROOM_TV_MAX_FACING_DEG = 45.0
BEDROOM_ISSUE_KEYS = (
    "bed_headboard_violations", "bed_access_violations",
    "bedside_violations", "storage_access_violations",
    "bed_tv_viewing_violations", "bed_workstation_violations",
)


def is_bedside_asset(asset: dict[str, Any]) -> bool:
    category = normalize_category(asset.get("category"))
    return (category == "nightstand" or asset.get("functional_role") == "bedside"
            or (category == "side_table" and asset.get("functional_group") == "sleeping"))


def bedroom_layout_measurements(
    layout: dict[str, Any], assets: list[dict[str, Any]],
    boundary: list[list[float]], room_area: tuple[float, float],
    *, mixed_groups: bool = False,
) -> dict[str, Any]:
    """Measure complete beds in their front=foot, back=headboard local frame.

    Access is a continuous clear strip, not center distance. Rugs and supported
    objects do not obstruct floor access. Only associated bedside furniture in
    the head zone is exempt from a side strip; other blockers remain measured.
    Storage clearance is a service band, not a model-specific drawer envelope.
    """
    room = room_polygon(room_area, boundary)
    room_cover = room.buffer(1e-7)
    records = []
    displays = []
    for asset in assets:
        uid = str(asset.get("instance_key") or asset.get("uid") or "")
        if uid not in layout:
            continue
        placement = layout[uid]
        category = normalize_category(asset.get("category"))
        pos, yaw, width, depth, _ = extract_placement(placement, asset)
        record = {"uid": uid, "category": category, "asset": asset,
                  "pos": pos, "yaw": yaw, "width": width, "depth": depth,
                  "poly": asset_polygon(pos, yaw, width, depth)}
        if category == "tv":
            displays.append(record)
        if category == "rug" or placement.get("on_top_of") or float(pos[2]) > 0.1:
            continue
        if asset.get("placement_mode") in {"wall", "wall_mounted", "ceiling", "ceiling_mounted"}:
            continue
        records.append(record)
    beds = [r for r in records if r["category"] == "bed"]
    result = {"beds": [], "bedsides": [], "storage": [], "tv_viewing": [], "workstations": [],
              "violations": {key: [] for key in BEDROOM_ISSUE_KEYS}}
    if not beds:
        return result

    def local_point(record, forward, lateral):
        yaw, pos = record["yaw"], record["pos"]
        return (pos[0] + forward * math.cos(yaw) - lateral * math.sin(yaw),
                pos[1] + forward * math.sin(yaw) + lateral * math.cos(yaw))

    def local_rect(record, front_min, front_max, side_min, side_max):
        return Polygon([local_point(record, f, s) for f, s in (
            (front_min, side_min), (front_max, side_min),
            (front_max, side_max), (front_min, side_max))])

    def facing_delta(record, target):
        direction = math.atan2(target["pos"][1] - record["pos"][1],
                               target["pos"][0] - record["pos"][0])
        return math.degrees(abs((record["yaw"] - direction + math.pi) % math.tau - math.pi))

    bedside_parents = {}
    for record in records:
        if not is_bedside_asset(record["asset"]):
            continue
        bed = min(beds, key=lambda b: b["poly"].distance(record["poly"]))
        dx, dy = record["pos"][0] - bed["pos"][0], record["pos"][1] - bed["pos"][1]
        forward = dx * math.cos(bed["yaw"]) + dy * math.sin(bed["yaw"])
        lateral = -dx * math.sin(bed["yaw"]) + dy * math.cos(bed["yaw"])
        gap = bed["poly"].distance(record["poly"])
        beside_head = (abs(lateral) >= bed["width"] / 2 and
                       -bed["depth"] / 2 - 0.20 <= forward <= -bed["depth"] / 2 + BEDSIDE_HEAD_ZONE_M)
        valid = beside_head and gap <= BEDSIDE_MAX_GAP_M + 1e-6
        row = {"uid": record["uid"], "bed": bed["uid"], "gap": round(gap, 4),
               "max_gap": BEDSIDE_MAX_GAP_M, "forward_offset_m": round(forward, 4),
               "offset": round(abs(forward + bed["depth"] / 2 - 0.225), 4), "max_offset": 0.425,
               "side_overlap_m": round(max(0.0, bed["width"] / 2 - abs(lateral)), 4),
               "lateral_offset_m": round(lateral, 4), "beside_head": beside_head, "valid": valid}
        result["bedsides"].append(row)
        if valid:
            bedside_parents[record["uid"]] = bed["uid"]
        else:
            result["violations"]["bedside_violations"].append({**row, "kind": "bedside_not_at_head"})

    def strip_clearance(record, side, *, exempt_bedside=False):
        half_w, half_d = record["width"] / 2, record["depth"] / 2
        head_zone = local_rect(record, -half_d, -half_d + BEDSIDE_HEAD_ZONE_M,
                               -half_w - 2.0, half_w + 2.0)

        def strip(width):
            if side == "foot":
                return local_rect(record, half_d, half_d + width, -half_w, half_w)
            if side == "back":
                return local_rect(record, -half_d - width, -half_d, -half_w, half_w)
            return local_rect(record, -half_d, half_d,
                              half_w if side == "left" else -half_w - width,
                              half_w + width if side == "left" else -half_w)

        def blockers(region):
            found = []
            for other in records:
                if other["uid"] == record["uid"]:
                    continue
                overlap = region.intersection(other["poly"])
                if exempt_bedside and bedside_parents.get(other["uid"]) == record["uid"]:
                    overlap = overlap.difference(head_zone)
                if overlap.area > 1e-6:
                    found.append(other["uid"])
            return found

        bounds = room.bounds
        low, high = 0.0, math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1])
        for _ in range(20):
            mid = (low + high) / 2
            region = strip(mid)
            if room_cover.covers(region) and not blockers(region):
                low = mid
            else:
                high = mid
        required = strip(BED_ACCESS_MIN_M)
        return {"gap": round(low, 4), "min_gap": BED_ACCESS_MIN_M,
                "clear": low + 1e-5 >= BED_ACCESS_MIN_M,
                "blockers": blockers(required), "boundary_blocked": not room_cover.covers(required),
                "region": list(required.exterior.coords)[:-1], "center_xy": record["pos"][:2],
                "center_bounds_for_room_bbox": service_center_bounds(room, record["poly"], required, record["pos"])}

    walls = list(room.exterior.coords)
    for bed in beds:
        head = local_point(bed, -bed["depth"] / 2, 0)
        ends = [local_point(bed, -bed["depth"] / 2, s * bed["width"] / 2) for s in (-1, 1)]
        wall_rows = []
        for a, b in zip(walls, walls[1:]):
            wall = LineString([a, b])
            if wall.length < 1e-6:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            inward = math.atan2(dx, -dy)
            midpoint = wall.interpolate(0.5, normalized=True)
            if not room.covers(Point(midpoint.x + math.cos(inward) * 0.01, midpoint.y + math.sin(inward) * 0.01)):
                inward += math.pi
            delta = abs((bed["yaw"] - inward + math.pi) % math.tau - math.pi)
            wall_rows.append({"wall_gap": max(wall.distance(Point(p)) for p in ends),
                              "delta_deg": math.degrees(delta), "target_yaw_rad": inward % math.tau})
        wall = min(wall_rows, key=lambda row: row["wall_gap"])
        orientation = {"uid": bed["uid"], "headboard_center": list(head),
                       **{k: round(v, 4) for k, v in wall.items()},
                       "max_wall_gap": BED_HEADBOARD_MAX_GAP_M, "max_delta_deg": 12.0}
        if wall["wall_gap"] > BED_HEADBOARD_MAX_GAP_M + 1e-6 or wall["delta_deg"] > 12.0:
            result["violations"]["bed_headboard_violations"].append({**orientation, "kind": "headboard_not_at_wall"})
        sides = {side: strip_clearance(bed, side, exempt_bedside=side != "foot")
                 for side in ("left", "right", "foot")}
        result["beds"].append({**orientation, "access": sides,
                               "dual_side_access": sides["left"]["clear"] and sides["right"]["clear"]})
        if not (sides["left"]["clear"] or sides["right"]["clear"]):
            best_side = max((sides["left"], sides["right"]), key=lambda row: row["gap"])
            result["violations"]["bed_access_violations"].append({
                "uid": bed["uid"], "kind": "no_usable_side", **best_side,
                "sides": {side: sides[side] for side in ("left", "right")},
                "blockers": sorted(set(sides["left"]["blockers"] + sides["right"]["blockers"]))})
        if not sides["foot"]["clear"]:
            result["violations"]["bed_access_violations"].append({"uid": bed["uid"], "kind": "foot_access_blocked", **sides["foot"]})

    for record in records:
        if record["category"] not in {"dresser", "cabinet", "wardrobe", "storage_unit", "sideboard"}:
            continue
        access = {"uid": record["uid"], "kind": "storage_front_blocked", **strip_clearance(record, "foot")}
        result["storage"].append(access)
        if not access["clear"]:
            result["violations"]["storage_access_violations"].append(access)

    for display in displays:
        if mixed_groups and display["asset"].get("viewing_target") not in {"bed", "shared"}:
            continue
        bed = min(beds, key=lambda b: b["poly"].distance(display["poly"]))
        bed_delta = facing_delta(bed, display)
        display_delta = facing_delta(display, bed)
        viewing = {"uid": display["uid"], "bed": bed["uid"],
                   "bed_facing_delta_deg": round(bed_delta, 4),
                   "tv_facing_delta_deg": round(display_delta, 4),
                   "delta_deg": round(max(bed_delta, display_delta), 4),
                   "max_delta_deg": BEDROOM_TV_MAX_FACING_DEG}
        result["tv_viewing"].append(viewing)
        if max(bed_delta, display_delta) > BEDROOM_TV_MAX_FACING_DEG + 1e-6:
            result["violations"]["bed_tv_viewing_violations"].append({
                **viewing, "kind": "tv_not_viewable_from_bed"})

    desks = [r for r in records if r["category"] == "desk"]
    for chair in records:
        if chair["category"] not in {"office_chair", "task_chair", "desk_chair"} or not desks:
            continue
        desk = min(desks, key=lambda d: d["poly"].distance(chair["poly"]))
        dx, dy = chair["pos"][0] - desk["pos"][0], chair["pos"][1] - desk["pos"][1]
        forward = dx * math.cos(desk["yaw"]) + dy * math.sin(desk["yaw"])
        lateral = -dx * math.sin(desk["yaw"]) + dy * math.cos(desk["yaw"])
        # Shared task-chair validation owns desk distance and facing.
        # Reserve the chair's swept footprint as it pulls away from the desk.
        pullout = strip_clearance(chair, "back")
        row = {"uid": chair["uid"], "desk": desk["uid"],
               "forward_offset_m": round(forward, 4), "lateral_offset_m": round(lateral, 4),
               "in_front": forward >= desk["depth"] / 2 and abs(lateral) <= desk["width"] / 2,
               "pullout": pullout}
        result["workstations"].append(row)
        if not row["in_front"]:
            result["violations"]["bed_workstation_violations"].append({**row, "kind": "chair_not_serving_desk_front"})
        if not pullout["clear"]:
            result["violations"]["bed_workstation_violations"].append({
                **row, **pullout, "kind": "chair_pullout_blocked"})
    return result
