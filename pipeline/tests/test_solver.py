"""The code layout solver on the parity fixture rooms, a benchmark room, and an L-shaped room."""

import json
import time
from pathlib import Path

import pytest
from test_rules_parity import GOLDEN, _golden_id, _golden_inputs

from app.rules.layout.analysis import analyze_layout, findings_by_level
from app.rules.layout.comfort import media_viewing_measurements
from app.rules.layout.solver import solve_layout
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


def test_sofa_moves_off_its_wall_to_bring_the_tv_into_its_viewing_range():
    # A 1.2 m TV prefers 1.44 to 4.2 m; in the 5 x 6 m benchmark living room every wall-backed sofa is farther.
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
    assert blocking(layout, assets, room) == {}


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
