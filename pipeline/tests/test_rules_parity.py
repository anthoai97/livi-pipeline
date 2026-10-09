"""Ported rules against recorded legacy runs (livinit_pipeline/runs, Oct 06 2026).

Fixture assets are legacy catalog rows rewritten as pipeline_assets_v2 records,
with the legacy uid kept as asset_id so recorded messages still match.
"""

import json
from pathlib import Path

import pytest

from app.rules.geometry.generation import generate_deterministic_layout_with_report
from app.rules.layout.analysis import analyze_layout, final_layout_check, findings_by_level
from app.rules.layout.cleanup import (
    clear_protected_paths,
    run_deterministic_living_dining_cleanup,
    run_deterministic_p0_cleanup,
    satisfy_sofa_table_gaps,
)
from app.rules.layout.normalization import normalize_layout
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.planner.seed_guidance import build_seed_guidance
from app.rules.selection.catalog import catalog_asset
from app.rules.selection.fit import capped_counts
from app.rules.selection.validation import validate_selection

RUNS = json.loads((Path(__file__).parent / "fixtures" / "legacy_runs.json").read_text())
# Legacy seed and cleanup outputs (livinit_pipeline @ 8ec38ae) on the RUNS variants
# and on synthetic living, bedroom, studio, and door-to-door rooms built as runtime instances.
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "legacy_seed_cleanup.json").read_text())


def _room(run):
    request = run["request"]
    intent = coerce_intent_packet(run["intent"], room_type=request["room_type"])
    room = build_room_context(
        room_type=request["room_type"],
        room_area=request["room_area"],
        room_vertices=request["room_vertices"],
        wall_height=request["wall_height"],
        room_doors=request["room_doors"],
        room_windows=request["room_windows"],
        intent=intent,
    )
    return intent, room


def _cases(key):
    return [
        pytest.param(run, case, id=f"{run['run'][-22:]}-{case.get('label') or 'v%(variant)s-turn%(turn)s' % case}")
        for run in RUNS
        for case in run[key]
    ]


@pytest.mark.parametrize("run", RUNS, ids=[run["run"][-22:] for run in RUNS])
def test_room_context_matches_recorded_digest(run):
    _, room = _room(run)
    assert {key: room["digest"][key] for key in run["digest"]} == run["digest"]


@pytest.mark.parametrize(("run", "turn"), _cases("selection_turns"))
def test_selection_validation_matches_recorded_result(run, turn):
    intent, room = _room(run)
    pool = list(run["assets"].values())
    result = validate_selection(
        items=[{"asset": run["assets"][uid], "quantity": quantity} for uid, quantity in turn["items"]],
        intent=intent,
        room=room,
        budget=run["request"]["budget"],
        candidates=pool,
        fit_satisfaction=turn["fit_satisfaction"],
        constraint_audit=turn["constraint_audit"],
    )
    assert result["valid"] == turn["valid"]
    # Placement now comes from prepared records on every turn, so a turn can
    # report extra layout-preflight errors next to the recorded ones.
    assert set(turn["errors"]) <= set(result["errors"])


@pytest.mark.parametrize(("run", "case"), _cases("layouts"))
def test_analyze_layout_matches_recorded_blocking_findings(run, case):
    request = run["request"]
    assets = [{**catalog_asset(record), "uid": record["uid"]} for record in run["layout_assets"][str(case["variant"])]]
    issues = analyze_layout(
        case["layout"],
        assets,
        request["room_vertices"],
        request["room_doors"],
        tuple(request["room_area"]),
        room_windows=request["room_windows"],
        protected_paths=[],
        room_type=request["room_type"],
    )
    levels = findings_by_level(issues)
    blocking = {**levels["P0"], **levels["P1"], **levels["critical_p2"]}
    assert json.loads(json.dumps(blocking)) == case["blocking"]
    if case["valid"] is not None:
        check = final_layout_check(
            case["layout"],
            assets,
            room_type=request["room_type"],
            room_area=request["room_area"],
            room_vertices=request["room_vertices"],
            room_doors=request["room_doors"],
            room_windows=request["room_windows"],
        )
        assert check["valid"] == case["valid"]


def _bedroom(room_area: tuple[float, float], requested: dict[str, int], required: set[str]) -> tuple[dict, dict]:
    """Bedroom intent and room context; items outside `required` are optional."""
    intent = coerce_intent_packet(
        {
            "normalized_prompt": "a bedroom",
            "requested_items": [{"canonical_category": c, "count": n, "optional": c not in required} for c, n in requested.items()],
        },
        room_type="bedroom",
    )
    room = build_room_context(
        room_type="bedroom",
        room_area=room_area,
        room_vertices=None,
        wall_height=2.7,
        room_doors=[{"center": [room_area[0] / 2, 0.0], "width": 0.9, "depth": 0.05}],
        room_windows=[],
        intent=intent,
    )
    return intent, room


@pytest.mark.parametrize(
    ("room_area", "dropped", "kept"),
    [
        ((5.0, 5.5), {"desk", "office_chair"}, {"tv", "tv_stand", "wardrobe"}),
        ((4.5, 5.0), {"desk", "office_chair", "tv", "tv_stand"}, {"wardrobe"}),
    ],
)
def test_capped_counts_keep_bed_and_drop_whole_lower_priority_groups(room_area, dropped, kept):
    requested = {"bed": 1, "nightstand": 2, "wardrobe": 1, "tv": 1, "tv_stand": 1, "desk": 1, "office_chair": 1}
    intent, room = _bedroom(room_area, requested, {"bed"})
    capped = capped_counts(room, intent)
    assert room["fit_warning"]
    assert capped["bed"] == 1
    assert all(capped[category] == 0 for category in dropped)
    assert all(capped[category] == requested[category] for category in kept)


def test_capped_counts_keep_explicitly_requested_items_and_cap_optional_ones():
    # The benchmark bedroom: the fit estimate fits only the bed and dresser.
    requested = {"bed": 1, "nightstand": 2, "table_lamp": 2, "dresser": 1, "bench": 1}
    intent, room = _bedroom((4.0, 4.5), requested, {"bed", "nightstand", "table_lamp", "dresser"})

    assert room["fit"]["counts"]["recommended"] == {"bed": 1, "dresser": 1}
    assert capped_counts(room, intent) == {**requested, "bench": 0}


def test_requested_gap_is_not_required():
    run = RUNS[1]  # dining room for four, passing selection on turn 1
    intent, room = _room(run)
    turn = run["selection_turns"][0]
    items = [{"asset": run["assets"][uid], "quantity": quantity} for uid, quantity in turn["items"]]
    pool = list(run["assets"].values())
    chairs_only = [item for item in items if item["asset"]["category"] != "dining_table"]
    intent = {**intent, "requested_items": [*intent["requested_items"], {
        "label": "bookcase", "canonical_category": "bookcase", "count": 1,
        "exact": True, "optional": False, "acceptable_substitutes": [],
    }]}

    missing = validate_selection(items=items, intent=intent, room=room, budget=5000, candidates=pool)
    gap = validate_selection(items=items, intent=intent, room=room, budget=5000, candidates=pool, gaps=["bookcase"])
    no_table = validate_selection(items=chairs_only, intent=intent, room=room, budget=5000, candidates=pool, gaps=["bookcase"])

    assert any("bookcase" in error for error in missing["errors"])
    assert gap["valid"], gap["errors"]
    assert not no_table["valid"]


def _golden_inputs(case):
    spec = GOLDEN["rooms"][case["room"]]
    if "run" in spec:
        run = RUNS[spec["run"]]
        intent, room = _room(run)
        return intent, room, [{**catalog_asset(record), "uid": record["uid"]} for record in run["layout_assets"][case["variant"]]]
    intent, room = _room(spec)
    return intent, room, spec["instances"]


def _golden_id(case):
    return "-".join(str(case[key]) for key in ("room", "variant", "label") if key in case)


def _room_args(room):
    return tuple(room["room_area"]), room["room_vertices"], room["room_doors"], room["room_windows"], room["protected_paths"]


@pytest.mark.parametrize("case", GOLDEN["seeds"], ids=_golden_id)
def test_seed_layout_matches_legacy(case):
    intent, room, assets = _golden_inputs(case)
    guidance = build_seed_guidance(room_facts=room["facts"], intent_packet=intent, feasibility_digest=room["digest"], selected_assets=assets)
    layout, report = generate_deterministic_layout_with_report(
        assets,
        tuple(room["room_area"]),
        room_vertices=room["room_vertices"],
        room_doors=room["room_doors"],
        room_windows=room["room_windows"],
        planner_guidance=guidance,
        protected_paths=room["protected_paths"],
    )
    assert json.loads(json.dumps(guidance)) == case["guidance"]
    assert json.loads(json.dumps([layout, report])) == [case["layout"], case["report"]]


def _cleanup_input(case):
    if case["label"].startswith("recorded"):
        run = RUNS[GOLDEN["rooms"][case["room"]]["run"]]
        recorded = [layout["layout"] for layout in run["layouts"] if str(layout["variant"]) == case["variant"]]
        return recorded[int(case["label"].removeprefix("recorded"))]
    seed = next(s["layout"] for s in GOLDEN["seeds"] if (s["room"], s["variant"]) == (case["room"], case["variant"]))
    if case["label"] == "seed_shifted":
        return {uid: {**p, "position": [p["position"][0] + 0.6, *p["position"][1:]]} for uid, p in seed.items()}
    return seed


@pytest.mark.parametrize("case", GOLDEN["cleanups"], ids=_golden_id)
def test_cleanup_matches_legacy(case):
    _, room, assets = _golden_inputs(case)
    room_area, boundary, doors, windows, paths = _room_args(room)
    for key, cleanup in (("p0", run_deterministic_p0_cleanup), ("living_dining", run_deterministic_living_dining_cleanup)):
        layout, report = cleanup(_cleanup_input(case), assets, room_area, boundary, room_doors=doors, room_windows=windows, protected_paths=paths)
        assert json.loads(json.dumps([layout, report])) == [case[key]["layout"], case[key]["report"]], key


def _issues_after(fix, name, layout):
    _, room, assets = _golden_inputs({"room": name})
    room_area, boundary, doors, windows, paths = _room_args(room)
    layout = normalize_layout(layout, assets, boundary, room_windows=None, rehome_service_items=False)

    def analyze(candidate):
        return analyze_layout(candidate, assets, boundary, doors, room_area, room_windows=windows, protected_paths=paths, room_type=room["room_type"])

    before = analyze(layout)
    fixed, report = fix(layout, assets, room_area, boundary, doors, windows, paths, room["room_type"])
    return before, analyze(normalize_layout(fixed, assets, boundary, room_windows=None, rehome_service_items=False)), report


def test_clear_protected_paths_moves_blocker_off_the_path():
    # Cabinet straddles the door-to-door path at y 2.05..2.96.
    cabinet = {"cabinet_1": {"position": [3.2, 2.3, 0.0], "rotation": [0.0, 0.0, 3.141592653589793]}}
    before, after, report = _issues_after(clear_protected_paths, "hallway_living", cabinet)
    assert [item["uid"] for item in before["protected_path_violations"]] == ["cabinet_1"]
    assert not after["protected_path_violations"]
    assert report["planned_moves"][0]["direction"] == "down"


def test_satisfy_sofa_table_gaps_moves_table_into_range():
    # 0.73 m sofa-to-table gap; the valid range is 0.46..0.61 m.
    layout = {
        "sofa_1": {"position": [3.5, 0.5, 0.0], "rotation": [0.0, 0.0, 1.5707963267948966]},
        "coffee_table_1": {"position": [3.5, 2.0, 0.0], "rotation": [0.0, 0.0, 1.5707963267948966]},
    }
    before, after, report = _issues_after(satisfy_sofa_table_gaps, "living", layout)
    assert before["sofa_coffee_table_violations"]
    assert not after["sofa_coffee_table_violations"]
    assert report["planned_moves"][0]["uid"] == "coffee_table_1"
