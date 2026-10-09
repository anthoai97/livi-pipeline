"""Ported rules against recorded legacy runs (livinit_pipeline/runs, Oct 06 2026).

Fixture assets are legacy catalog rows rewritten as pipeline_assets_v2 records,
with the legacy uid kept as asset_id so recorded messages still match.
"""

import json
from pathlib import Path

import pytest

from app.rules.layout.analysis import analyze_layout, final_layout_check, findings_by_level
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.selection.catalog import catalog_asset
from app.rules.selection.fit import capped_counts
from app.rules.selection.validation import validate_selection

RUNS = json.loads((Path(__file__).parent / "fixtures" / "legacy_runs.json").read_text())


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


@pytest.mark.parametrize(
    ("room_area", "dropped", "kept"),
    [
        ((5.0, 5.5), {"desk", "office_chair"}, {"tv", "tv_stand", "wardrobe"}),
        ((4.5, 5.0), {"desk", "office_chair", "tv", "tv_stand"}, {"wardrobe"}),
    ],
)
def test_capped_counts_keep_bed_and_drop_whole_lower_priority_groups(room_area, dropped, kept):
    requested = {"bed": 1, "nightstand": 2, "wardrobe": 1, "tv": 1, "tv_stand": 1, "desk": 1, "office_chair": 1}
    intent = coerce_intent_packet(
        {
            "normalized_prompt": "bedroom with wardrobe, TV, and desk",
            "requested_items": [{"canonical_category": c, "count": n} for c, n in requested.items()],
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
    capped = capped_counts(room)
    assert room["fit_warning"]
    assert capped["bed"] == 1
    assert all(capped[category] == 0 for category in dropped)
    assert all(capped[category] == requested[category] for category in kept)


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
