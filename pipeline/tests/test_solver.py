"""The code layout solver on the parity fixture rooms, a benchmark room, and an L-shaped room."""

import json
import math
import time
from pathlib import Path

import pytest
from shapely.ops import unary_union
from test_rules_parity import GOLDEN, _golden_id, _golden_inputs

from app.rules.geometry.primitives import asset_polygon
from app.rules.layout.analysis import analyze_layout, findings_by_level
from app.rules.layout.comfort import media_viewing_measurements
from app.rules.layout.dining import dining_layout_measurements
from app.rules.layout.solver import solve_layout
from app.rules.layout.validation_functional import compute_sofa_wall_gap_violations
from app.rules.layout.validation_geometry import (
    compute_door_violations,
    compute_protected_path_violations,
)
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context

BENCHMARK = {entry["label"]: entry["request"] for entry in
             json.loads((Path(__file__).parents[1] / "scripts" / "benchmark_requests.json").read_text())}
# Recorded legacy variant whose selection does not fit its room: legacy's final layout failed on it too.
UNFIT = ("run0", "1")


def blocking(layout, assets, room):
    issues = analyze_layout(layout, assets, room["room_vertices"], room["room_doors"], tuple(room["room_area"]),
                            room_windows=room["room_windows"], protected_paths=room["protected_paths"],
                            room_type=room["room_type"])
    levels = findings_by_level(issues)
    return {**levels["P0"], **levels["P1"], **levels["critical_p2"]}


def item(uid, category, width, depth, height, placement_mode="floor", **fields):
    return {"uid": uid, "instance_key": uid, "category": category, "width": width, "depth": depth, "height": height,
            "placement_mode": placement_mode, "price": 100.0, "is_decor_item": False, **fields}


def dining_room(table, *extra):
    """The benchmark dining room with this table (width, depth, height), four 0.59 m chairs, and extra instances."""
    request = BENCHMARK["dining_room"]
    intent = coerce_intent_packet({"normalized_prompt": request["user_intent"],
                                   "requested_categories": ["dining_table", "dining_chair"]}, room_type="dining_room")
    room = build_room_context(room_type="dining_room", room_area=request["room_area"], room_vertices=request["room_vertices"],
                              wall_height=request["wall_height"], room_doors=request["room_doors"],
                              room_windows=request["room_windows"], intent=intent)
    chairs = [item(f"dining_chair_{n}", "dining_chair", 0.59, 0.57, 0.74) for n in range(1, 5)]
    return intent, room, [item("dining_table_1", "dining_table", *table), *chairs, *extra]


@pytest.mark.parametrize("case", GOLDEN["seeds"], ids=_golden_id)
def test_solver_places_every_item_without_blocking_findings(case):
    intent, room, assets = _golden_inputs(case)

    layout, report = solve_layout(assets, room, intent)

    assert set(layout) == {asset["uid"] for asset in assets}
    if (case["room"], case["variant"]) == UNFIT:
        # Four chairs need 3.91 m of pull-out envelope both ways, which the sleeper sofa leaves no room for.
        assert set(report["unplaceable"]) == {"dining_table_688", *(f"dining_chair_723_{n}" for n in range(1, 5))}
        assert blocking(layout, assets, room)
    else:
        assert report["unplaceable"] == [] and report["score"][:2] == [0, 0]
        assert blocking(layout, assets, room) == {}


def test_solver_keeps_a_protected_path_and_its_doorways_clear():
    # A straight path between the two side doors splits the room; the living group fits only beside it.
    intent, room, assets = _golden_inputs({"room": "hallway_living"})

    layout, _ = solve_layout(assets, room, intent)

    assert room["protected_paths"] and len(room["room_doors"]) == 2
    assert compute_protected_path_violations(layout, assets, room["protected_paths"]) == []
    assert compute_door_violations(layout, assets, room["room_doors"], room["room_vertices"], tuple(room["room_area"])) == []


def test_media_pulls_off_its_wall_to_bring_the_tv_into_its_viewing_range():
    # A 1.2 m TV prefers 1.44 to 4.2 m; across the 5 m side of the benchmark living room a flush stand sees it from about 4.3 m.
    request = BENCHMARK["living_room"]
    spec = GOLDEN["rooms"]["living"]
    assets = [{**asset, "width": 1.2, "placement_mode": "tabletop", "paired_support_uid": "tv_stand_1"}
              if asset["category"] == "tv" else asset for asset in spec["instances"]]
    intent = coerce_intent_packet(spec["intent"], room_type="living_room")
    room = build_room_context(room_type="living_room", room_area=request["room_area"], room_vertices=request["room_vertices"],
                              wall_height=request["wall_height"], room_doors=request["room_doors"],
                              room_windows=request["room_windows"], intent=intent)

    layout, _ = solve_layout(assets, room, intent)

    [viewing] = media_viewing_measurements(layout, assets)
    assert viewing["viewer"] == "sofa_1" and viewing["within_preferred_range"], viewing
    assert layout["tv_1"]["on_top_of"] == "tv_stand_1"
    assert compute_sofa_wall_gap_violations(layout, assets, room["room_vertices"], room["room_windows"]) == []
    assert blocking(layout, assets, room) == {}


def test_studio_tv_stands_free_on_the_sofa_axis_when_the_far_wall_is_out_of_range():
    # Across the 6.4 m studio a wall-backed 1.2 m TV is seen from 5.3 m, past its 1.44 to 4.2 m range.
    intent, room, assets = _golden_inputs({"room": "studio"})
    assets = [{**asset, "placement_mode": "tabletop", "paired_support_uid": "tv_stand_1"}
              if asset["category"] == "tv" else asset for asset in assets]

    layout, report = solve_layout(assets, room, intent)

    [viewing] = media_viewing_measurements(layout, assets)
    assert viewing["viewer"] == "sofa_1" and viewing["within_preferred_range"], viewing
    assert viewing["viewer_faces_media"] and viewing["media_faces_viewer"]
    assert layout["tv_1"]["on_top_of"] == "tv_stand_1"
    assert report["unplaceable"] == [] and blocking(layout, assets, room) == {}


def test_studio_floor_lamp_stands_by_the_sofa_not_a_dining_chair():
    intent, room, assets = _golden_inputs({"room": "studio"})
    assets = [*assets, item("floor_lamp_1", "floor_lamp", 0.46, 0.3, 1.62)]

    layout, _ = solve_layout(assets, room, intent)

    lamp = layout["floor_lamp_1"]["position"][:2]
    seats = ("sofa_1", "dining_chair_1", "dining_chair_2")
    assert min(seats, key=lambda uid: math.dist(lamp, layout[uid]["position"][:2])) == "sofa_1"


def test_bedside_table_lamps_stand_on_the_nightstands():
    # The seed puts both lamps on the desk, the support nearest a seat (the office chair).
    intent, room, assets = _golden_inputs({"room": "bedroom"})

    layout, _ = solve_layout(assets, room, intent)

    assert {layout[uid]["on_top_of"] for uid in ("table_lamp_1", "table_lamp_2")} == {"nightstand_1", "nightstand_2"}


def test_l_shaped_room_places_every_item_inside_the_outline():
    # Wall candidates assume the bounding box; the room outline filters them (boundary findings would block).
    spec = GOLDEN["rooms"]["living"]
    intent = coerce_intent_packet(spec["intent"], room_type="living_room")
    vertices = [[0, 0], [6, 0], [6, 3], [3.5, 3], [3.5, 5], [0, 5]]
    room = build_room_context(room_type="living_room", room_area=(6, 5), room_vertices=vertices, wall_height=2.7,
                              room_doors=[{"center": [1.0, 0.0], "width": 0.9, "depth": 0.05, "height": 2.08}],
                              room_windows=[], intent=intent)
    assets = spec["instances"]

    layout, report = solve_layout(assets, room, intent)

    assert set(layout) == {asset["uid"] for asset in assets} and report["unplaceable"] == []
    assert blocking(layout, assets, room) == {}


@pytest.mark.parametrize("name", ["living", "studio"])  # the most items, and the largest room
def test_a_solve_takes_under_two_seconds(name):
    intent, room, assets = _golden_inputs({"room": name})

    started = time.perf_counter()
    _, report = solve_layout(assets, room, intent)

    assert time.perf_counter() - started < 2.0
    assert report["elapsed"] < 2.0


@pytest.mark.parametrize("benchmark", [False, True], ids=["golden", "benchmark"])
def test_studio_dining_stands_apart_from_the_sofa_and_the_bed(benchmark):
    # The dining table used to take the middle of the room, 0.45 m from the bed group (0.6 m in the 5.5 x 7 m benchmark studio).
    intent, room, assets = _golden_inputs({"room": "studio"})
    if benchmark:
        request = BENCHMARK["studio"]
        room = build_room_context(room_type="studio", room_area=request["room_area"], room_vertices=request["room_vertices"],
                                  wall_height=request["wall_height"], room_doors=request["room_doors"],
                                  room_windows=request["room_windows"], intent=intent)

    layout, report = solve_layout(assets, room, intent)

    def footprint(*categories):
        return unary_union([asset_polygon(layout[asset["uid"]]["position"], layout[asset["uid"]]["rotation"][2],
                                          asset["width"], asset["depth"]) for asset in assets if asset["category"] in categories])

    dining = footprint("dining_table", "dining_chair")
    assert dining.distance(footprint("sofa", "coffee_table", "bed", "nightstand")) > 1.0
    assert report["unplaceable"] == [] and blocking(layout, assets, room) == {}


def test_studio_layouts_on_offer_differ_in_their_zone_plans():
    # The four final layouts used to share one plan (bed, sofa, and dining table in the same places), differing only in the TV stand.
    intent, room, assets = _golden_inputs({"room": "studio"})

    layout, report = solve_layout(assets, room, intent)

    def plan(layout):
        x, y, _ = layout["dining_table_1"]["position"]
        return (*(round(layout[uid]["rotation"][2] / (math.pi / 2)) % 4 for uid in ("bed_1", "sofa_1")), x > 3.2, y > 3.35)

    ties = [alternative["layout"] for alternative in report["alternatives"] if alternative["score"] == report["score"]]
    assert len({plan(option) for option in (layout, *ties)}) >= 3


def test_a_floor_planter_never_stands_on_the_dining_table():
    # A recorded run put a 1.66 m potted tree on this table; a floor planter, even one the tabletop fits, stays on the floor.
    intent, room, assets = dining_room(
        (2.39, 0.96, 0.77),
        item("planter_1", "planter", 0.67, 0.67, 1.66, is_decor_item=True),
        item("planter_2", "planter", 0.3, 0.3, 0.5, is_decor_item=True),
        item("vase_1", "vase", 0.15, 0.15, 0.3, "tabletop", is_decor_item=True),
    )

    layout, _ = solve_layout(assets, room, intent)

    for uid in ("planter_1", "planter_2"):
        assert layout[uid].get("on_top_of") is None and layout[uid]["position"][2] == 0.0
    assert layout["vase_1"]["on_top_of"] == "dining_table_1"
    assert blocking(layout, assets, room) == {}


def test_four_chairs_take_two_per_long_side_of_a_rectangular_table():
    # Recorded run 1, variant 0: the solver used to put three chairs on one long side of the 2.06 x 1.04 m table and one at an end.
    intent, room, assets = _golden_inputs({"room": "run1", "variant": "0"})

    layout, _ = solve_layout(assets, room, intent)

    edges = {}
    for row in dining_layout_measurements(layout, assets, room["room_vertices"], tuple(room["room_area"]))["seating_edges"]:
        edges.setdefault(row["edge"], []).append(row)
    assert set(edges) in ({"front", "back"}, {"left", "right"})
    for rows in edges.values():
        length = rows[0]["edge_length_m"]
        assert length == pytest.approx(2.0574, abs=1e-3)
        # Centered on the edge with even spacing: the quarter points.
        assert sorted(row["seat_center_m"] for row in rows) == pytest.approx([-length / 4, length / 4], abs=0.01)


def test_chairs_space_evenly_around_a_round_table():
    # Recorded runs paired the four chairs on two opposite sides of this 1.37 m round table.
    intent, room, assets = dining_room((1.37, 1.37, 0.76))

    layout, _ = solve_layout(assets, room, intent)

    x, y, _ = layout["dining_table_1"]["position"]
    chairs = [layout[asset["uid"]]["position"] for asset in assets if asset["category"] == "dining_chair"]
    angles = sorted(math.degrees(math.atan2(cy - y, cx - x)) % 360 for cx, cy, _ in chairs)
    assert [(b - a) % 360 for a, b in zip(angles, angles[1:] + angles[:1])] == pytest.approx([90.0] * 4, abs=1.0)
    assert len({round(math.dist((x, y), (cx, cy)), 3) for cx, cy, _ in chairs}) == 1
    assert blocking(layout, assets, room) == {}
