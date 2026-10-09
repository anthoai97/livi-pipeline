"""Read-only dining fit and access measurements; never change selected furniture or poses."""

import math
from itertools import product
from typing import Any

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union

from app.rules.categories import is_ceiling_mounted_asset
from app.rules.geometry.primitives import extract_placement, room_polygon, service_center_bounds
from app.rules.layout.constants import DINING_ACCESS_MIN_M, DINING_CHAIR_GAP_M, DINING_ISSUE_KEYS, DINING_LIGHT_TABLE_MIN_GAP_M
from app.rules.layout.relations import _functional_group_assets, floor_assets
from app.rules.planner.taxonomy import normalize_category
from app.rules.layout.studio import accessory_group


def dining_seat_pitch(chair: dict[str, Any]) -> float:
    return max(0.60, float(chair.get("width") or 0) + 0.05)


def dining_fit_envelopes(table: dict[str, Any], chairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidate rectangular seating envelopes, including chair pull-out space.

    These are necessary coarse fit facts, not certification of openings, table
    legs, seat height, or a final arrangement. Width/depth are full model bounds.
    """
    width, depth = float(table.get("width") or 0), float(table.get("depth") or 0)
    if not chairs or min(width, depth) <= 0:
        return []
    pitch = max(dining_seat_pitch(chair) for chair in chairs)
    allowance = max(float(c.get("depth") or 0) for c in chairs) + DINING_CHAIR_GAP_M + DINING_ACCESS_MIN_M
    count = len(chairs)
    capacities = [min(count, math.floor(side / pitch)) for side in (width, width, depth, depth)]
    options = []
    for sides in product(*(range(capacity + 1) for capacity in capacities)):
        if sum(sides) != count:
            continue
        options.append({
            "width": width + allowance * sum(bool(n) for n in sides[2:]),
            "depth": depth + allowance * sum(bool(n) for n in sides[:2]),
            "chairs_per_edge": list(sides), "seat_pitch_m": round(pitch, 3),
        })
    return sorted(options, key=lambda option: option["width"] * option["depth"])


def dining_placement_facts(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dimensioned edge-normal seating options, not proposed or repaired poses."""
    tables = [a for a in assets if normalize_category(a.get("category")) == "dining_table"]
    chairs = [a for a in assets if normalize_category(a.get("category")) == "dining_chair"
              or (normalize_category(a.get("category")) == "chair"
                  and a.get("functional_role", "") in {"", "dining_seating"})]
    if not chairs:
        return []
    facts = []
    for table in tables:
        seating = []
        for chair in chairs:
            chair_depth = float(chair.get("depth") or 0)
            seating.append({
                "chair": chair.get("instance_key") or chair.get("uid"),
                "center_offset_from_table_m": {
                    "front_or_back": round(float(table.get("depth") or 0) / 2 + chair_depth / 2 + DINING_CHAIR_GAP_M, 4),
                    "left_or_right": round(float(table.get("width") or 0) / 2 + chair_depth / 2 + DINING_CHAIR_GAP_M, 4),
                },
                "table_edge_to_pullout_end_m": round(DINING_CHAIR_GAP_M + chair_depth + DINING_ACCESS_MIN_M, 4),
            })
        facts.append({
            "table": table.get("instance_key") or table.get("uid"),
            "edge_gap_m": DINING_CHAIR_GAP_M,
            "pullout_m": DINING_ACCESS_MIN_M,
            "chairs": seating,
            "fit_envelopes": [
                {**option, "width": round(option["width"], 4), "depth": round(option["depth"], 4),
                 "chairs_per_edge": dict(zip(("front", "back", "left", "right"), option["chairs_per_edge"]))}
                for option in dining_fit_envelopes(table, chairs)
            ],
        })
    return facts


def dining_chair_table_facts(layout: dict[str, Any], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Current chair/table separation in world coordinates, not a layout repair."""
    groups = _functional_group_assets(layout, assets)
    facts = []
    for chair in groups["dining_chairs"]:
        if not groups["dining_tables"]:
            break
        table = min(groups["dining_tables"], key=lambda item: item.poly.distance(chair.poly))
        nx, ny = math.cos(chair.rot_z), math.sin(chair.rot_z)
        if (table.pos[0] - chair.pos[0]) * nx + (table.pos[1] - chair.pos[1]) * ny <= 0:
            continue  # Facing-away validation must be resolved before an edge-normal gap is meaningful.
        ray_length = math.dist(chair.pos[:2], table.pos[:2]) + math.hypot(table.width, table.depth)
        if not LineString([chair.pos[:2], (chair.pos[0] + ray_length * nx,
                                          chair.pos[1] + ray_length * ny)]).intersects(table.poly):
            continue  # A chair beside the table must first aim at an occupied edge.
        table_near_edge = min(x * nx + y * ny for x, y in table.poly.exterior.coords)
        chair_front = chair.pos[0] * nx + chair.pos[1] * ny + chair.depth / 2
        gap = table_near_edge - chair_front
        shift = gap - DINING_CHAIR_GAP_M
        facts.append({
            "chair": chair.uid, "table": table.uid,
            "chair_center_xy": chair.pos[:2], "table_center_xy": table.pos[:2],
            "chair_front_direction_xy": [round(nx, 5), round(ny, 5)],
            "signed_edge_gap_m": round(gap, 5), "target_edge_gap_m": DINING_CHAIR_GAP_M,
            "chair_translation_to_target_gap_xy_m": [round(shift * nx, 5), round(shift * ny, 5)],
            "chair_center_at_target_gap_xy": [round(chair.pos[0] + shift * nx, 5),
                                               round(chair.pos[1] + shift * ny, 5)],
            "required_pullout_m": DINING_ACCESS_MIN_M,
        })
    return facts


def dining_layout_measurements(
    layout: dict[str, Any], assets: list[dict[str, Any]],
    boundary: list[list[float]], room_area: tuple[float, float],
    *, mixed_groups: bool = False,
) -> dict[str, Any]:
    """Measure chair-back and storage-front service bands in each asset's frame.

    Shared functional validation owns chair/table distance and facing. Rugs and
    supported objects do not obstruct floor access. Pull-out bands may share
    circulation space; they may not cross walls or furniture.
    """
    room = room_polygon(room_area, boundary).buffer(1e-7)
    groups = _functional_group_assets(layout, assets)
    tables, chairs = groups["dining_tables"], groups["dining_chairs"]
    result = {"chairs": [], "seating_edges": [], "storage": [], "lights": [], "rugs": [],
              "violations": {key: [] for key in DINING_ISSUE_KEYS}}
    if len(tables) != 1 or not chairs:
        result["violations"]["dining_completeness_violations"].append({
            "kind": "incomplete_dining_group", "table_count": len(tables), "chair_count": len(chairs),
            "uids": [item.uid for item in tables + chairs],
        })
    records = [item for item in floor_assets(layout, assets, skip_rugs=True)
               if not layout[item.uid].get("on_top_of")
               and item.asset.get("placement_mode") not in {"wall", "wall_mounted", "ceiling", "ceiling_mounted"}]

    def band(item, distance, back):
        edge = (-1 if back else 1) * item.depth / 2
        outer = edge + (-distance if back else distance)
        c, s = math.cos(item.rot_z), math.sin(item.rot_z)
        return Polygon([(item.pos[0] + f * c - lateral * s, item.pos[1] + f * s + lateral * c)
                        for f, lateral in ((edge, -item.width / 2), (outer, -item.width / 2),
                                           (outer, item.width / 2), (edge, item.width / 2))])

    def access(item, back):
        required = band(item, DINING_ACCESS_MIN_M, back)
        blockers = [other.uid for other in records if other.uid != item.uid
                    and required.intersection(other.poly).area > 1e-6]
        boundary_blocked = not room.covers(required)
        low, high = 0.0, DINING_ACCESS_MIN_M
        for _ in range(16):
            mid = (low + high) / 2
            region = band(item, mid, back)
            if room.covers(region) and not any(region.intersection(other.poly).area > 1e-6
                                              for other in records if other.uid in blockers):
                low = mid
            else:
                high = mid
        return {"uid": item.uid, "gap": round(low, 4), "min_gap": DINING_ACCESS_MIN_M,
                "clear": not blockers and not boundary_blocked, "blockers": blockers,
                "boundary_blocked": boundary_blocked, "region": list(required.exterior.coords)[:-1],
                "center_xy": item.pos[:2],
                "center_bounds_for_room_bbox": service_center_bounds(room, item.poly, required, item.pos)}

    chair_tables = {chair.uid: min(tables, key=lambda table: table.poly.distance(chair.poly)).uid
                    for chair in chairs if tables}
    for chair in chairs:
        row = {**access(chair, True), "kind": "chair_pullout_blocked"}
        row["clearance_shortfall_m"] = round(max(0.0, row["min_gap"] - row["gap"]), 4)
        if chair.uid in chair_tables:
            table_uid = chair_tables[chair.uid]
            row["table"] = table_uid
            row["group_members"] = [table_uid, *(uid for uid, table in chair_tables.items() if table == table_uid)]
        result["chairs"].append(row)
        if not row["clear"]:
            result["violations"]["dining_access_violations"].append(row)

    # A ray hitting the tabletop does not establish enough usable edge for a
    # diner. Measure each occupied edge in the table's actual rotated frame.
    seats_by_edge = {}
    for chair in chairs:
        if not tables:
            break
        table = min(tables, key=lambda item: item.poly.distance(chair.poly))
        shape = str(table.asset.get("shape") or "").lower()
        if shape in {"round", "circular", "oval", "elliptical"}:
            continue  # Curved perimeters have no four straight seating edges.
        distance = math.dist(chair.pos[:2], table.pos[:2]) + math.hypot(table.width, table.depth)
        ray = LineString([chair.pos[:2], (chair.pos[0] + math.cos(chair.rot_z) * distance,
                                        chair.pos[1] + math.sin(chair.rot_z) * distance)])
        hit = table.poly.boundary.intersection(ray)
        if hit.is_empty:
            continue  # Shared chair/table aim validation reports this failure.
        point = nearest_points(Point(chair.pos[:2]), hit)[1]
        dx, dy = point.x - table.pos[0], point.y - table.pos[1]
        forward = dx * math.cos(table.rot_z) + dy * math.sin(table.rot_z)
        lateral = -dx * math.sin(table.rot_z) + dy * math.cos(table.rot_z)
        if abs(abs(forward) - table.depth / 2) <= abs(abs(lateral) - table.width / 2):
            edge, offset, length = ("front" if forward > 0 else "back"), lateral, table.width
        else:
            edge, offset, length = ("left" if lateral > 0 else "right"), forward, table.depth
        pitch = dining_seat_pitch(chair.asset)
        row = {"uid": chair.uid, "table": table.uid, "edge": edge,
               "edge_length_m": round(length, 4), "seat_center_m": round(offset, 4),
               "seat_pitch_m": round(pitch, 4)}
        result["seating_edges"].append(row)
        seats_by_edge.setdefault((table.uid, edge), []).append(row)
        overhang = abs(offset) + pitch / 2 - length / 2
        if overhang > 0.03:
            result["violations"]["dining_seat_space_violations"].append({
                **row, "kind": "seat_overhangs_table_edge", "overhang_m": round(overhang, 4),
            })
    for rows in seats_by_edge.values():
        rows.sort(key=lambda row: row["seat_center_m"])
        for first, second in zip(rows, rows[1:]):
            gap = second["seat_center_m"] - first["seat_center_m"]
            minimum = (first["seat_pitch_m"] + second["seat_pitch_m"]) / 2
            if gap < minimum - 0.03:
                result["violations"]["dining_seat_space_violations"].append({
                    **first, "other": second["uid"], "kind": "shared_seating_space",
                    "gap": round(gap, 4), "min_gap": round(minimum, 4),
                })
    for item in records:
        if normalize_category(item.asset.get("category")) not in {"sideboard", "cabinet", "storage_unit"}:
            continue
        row = {**access(item, False), "kind": "storage_front_blocked"}
        result["storage"].append(row)
        if not row["clear"]:
            result["violations"]["dining_storage_access_violations"].append(row)
    lights_by_table = {}
    for asset in assets:
        if not tables or not is_ceiling_mounted_asset(asset.get("category", ""), asset.get("uid", "")):
            continue
        uid = str(asset.get("instance_key") or asset.get("uid") or "")
        if uid not in layout:
            continue
        if mixed_groups and accessory_group(layout, assets, uid) != "dining":
            continue
        pos, _, _, _, _ = extract_placement(layout[uid], asset)
        table = min(tables, key=lambda item: math.dist(pos[:2], item.pos[:2]))
        gap = pos[2] - (table.pos[2] + float(table.asset.get("height") or 0))
        offset = math.dist(pos[:2], table.pos[:2])
        row = {"uid": uid, "table": table.uid, "gap": round(gap, 4),
               "min_gap": DINING_LIGHT_TABLE_MIN_GAP_M, "offset_m": round(offset, 4)}
        result["lights"].append(row)
        lights_by_table.setdefault(table.uid, []).append((row, pos, table))
        if gap < DINING_LIGHT_TABLE_MIN_GAP_M - 1e-6:
            result["violations"]["dining_light_clearance_violations"].append({
                **row, "kind": "light_too_low_over_table",
            })
        if not table.poly.covers(Point(pos[:2])):
            result["violations"]["dining_light_alignment_violations"].append({
                **row, "kind": "light_outside_tabletop",
            })
    for lights in lights_by_table.values():
        table = lights[0][2]
        center = [sum(pos[axis] for _, pos, _ in lights) / len(lights) for axis in (0, 1)]
        offset = math.dist(center, table.pos[:2])
        if offset > 0.30:
            result["violations"]["dining_light_alignment_violations"].append({
                "uids": [row["uid"] for row, _, _ in lights], "table": table.uid,
                "kind": "lighting_off_table_center", "offset_m": round(offset, 4), "max_offset_m": 0.30,
            })
    if tables and chairs:
        seated_group = unary_union([item.poly for item in tables + chairs])
        for rug in groups["rugs"]:
            if mixed_groups and accessory_group(layout, assets, rug.uid) != "dining":
                continue
            coverage = rug.poly.intersection(seated_group).area / seated_group.area
            row = {"uid": rug.uid, "kind": "rug_misses_seated_group", "coverage": round(coverage, 4),
                   "min_coverage": 0.95}
            result["rugs"].append(row)
            if coverage < 0.95:
                result["violations"]["dining_rug_violations"].append(row)
    return result
