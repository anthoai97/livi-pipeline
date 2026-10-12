"""Ported rules against recorded legacy runs (livinit_pipeline/runs, Oct 06 2026).

Fixture assets are legacy catalog rows rewritten as pipeline_assets_v2 records,
with the legacy uid kept as asset_id so recorded messages still match.
"""

import json
import math
from pathlib import Path

import pytest

from app import variant_stages
from app.rules.geometry.generation import generate_deterministic_layout_with_report
from app.rules.layout.analysis import analyze_layout, final_layout_check, findings_by_level
from app.rules.layout.cleanup import (
    clear_protected_paths,
    run_deterministic_living_dining_cleanup,
    run_deterministic_p0_cleanup,
    satisfy_sofa_table_gaps,
)
from app.rules.layout.dining import dining_fit_envelopes
from app.rules.layout.metrics import has_blocking_issues
from app.rules.layout.normalization import normalize_layout
from app.rules.planner._util import room_scale
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.planner.seed_guidance import build_seed_guidance
from app.rules.selection.catalog import catalog_asset
from app.rules.selection.fit import capped_counts
from app.rules.selection.preflight import _layout_preflight_for_assets
from app.rules.selection.validation import _size_findings, build_instances, validate_selection

# Recorded digests, blocking findings and validity are re-measured at the issue #54 phase 1 rules: per-room size classes
# (these 20.8 m2 dining rooms are spacious), a door clear zone as deep as the door is wide, a 0.46 m chair pull-out
# (0.56 m with a path behind) and 0.76 m storage fronts. At the phase 2 rules the first run, which states no seat
# count, defaults to six chairs (four to six pass), and its turns without a sideboard or cabinet, or without a
# pendant when the pool holds one, fail; the other two runs are minimal or state their counts and are unchanged.
RUNS = json.loads((Path(__file__).parent / "fixtures" / "legacy_runs.json").read_text())
# Legacy seed and cleanup outputs (livinit_pipeline @ 8ec38ae) on the RUNS variants
# and on synthetic living, bedroom, studio, and door-to-door rooms built as runtime instances,
# re-recorded at the issue #54 phase 1 rules (the door clear zone, 1.5 m wall mounts, the new clearances and TV
# range, TV support checks, the desk check in every room, and the window_front count).
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
        # Medium bedrooms the requested pieces overfill (real footprint against the 35% target).
        ((3.8, 4.0), {"desk", "office_chair"}, {"tv", "tv_stand", "wardrobe"}),
        ((3.6, 3.8), {"desk", "office_chair", "tv", "tv_stand"}, {"wardrobe"}),
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
    # A 3 x 3 m bedroom: the fit estimate fits only the bed and dresser.
    requested = {"bed": 1, "nightstand": 2, "table_lamp": 2, "dresser": 1, "bench": 1}
    intent, room = _bedroom((3.0, 3.0), requested, {"bed", "nightstand", "table_lamp", "dresser"})

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
    # 0.73 m sofa-to-table gap; the valid range is 0.38..0.50 m.
    layout = {
        "sofa_1": {"position": [3.5, 0.5, 0.0], "rotation": [0.0, 0.0, 1.5707963267948966]},
        "coffee_table_1": {"position": [3.5, 2.0, 0.0], "rotation": [0.0, 0.0, 1.5707963267948966]},
    }
    before, after, report = _issues_after(satisfy_sofa_table_gaps, "living", layout)
    assert before["sofa_coffee_table_violations"]
    assert not after["sofa_coffee_table_violations"]
    assert report["planned_moves"][0]["uid"] == "coffee_table_1"


# --- issue #54 phase 1: thresholds and bugs, measured on the fixture rooms


def _pose(x, y, yaw, z=0.0, **fields):
    return {"position": [x, y, z], "rotation": [0.0, 0.0, yaw], **fields}


def _measure(name, layout, *extra):
    """analyze_layout on a fixture room, selecting only the placed pieces; `extra` instances are added, or replace
    the fixture's of the same uid."""
    _, room, assets = _golden_inputs({"room": name})
    assets = [asset for asset in [*(asset for asset in assets if asset["uid"] not in {item["uid"] for item in extra}), *extra]
              if asset["uid"] in layout]
    room_area, boundary, doors, windows, paths = _room_args(room)
    return analyze_layout(layout, assets, boundary, doors, room_area, room_windows=windows, protected_paths=paths,
                          room_type=room["room_type"], wall_height=room["wall_height"])


@pytest.mark.parametrize(("gap", "flagged"), [(0.42, False), (0.55, True)])
def test_sofa_to_coffee_table_gap_is_0_38_to_0_50_m(gap, flagged):
    # Sofa back on the left wall, front at x = 0.95; the table's near face `gap` beyond it.
    layout = {"sofa_1": _pose(0.475, 2.25, 0.0), "coffee_table_1": _pose(0.95 + gap + 0.3, 2.25, 0.0)}
    assert bool(_measure("living", layout)["sofa_coffee_table_violations"]) is flagged


@pytest.mark.parametrize(("back_gap", "flagged"), [(0.0, False), (0.2, True)])
def test_a_flush_sofa_passes_the_wall_gap(back_gap, flagged):
    layout = {"sofa_1": _pose(0.475 + back_gap, 2.25, 0.0)}
    assert bool(_measure("living", layout)["sofa_wall_gap_violations"]) is flagged


@pytest.mark.parametrize(("front", "flagged"), [(0.60, True), (0.80, False)])
def test_storage_needs_0_76_m_in_front(front, flagged):
    # Dresser on the bottom wall facing up, the desk `front` metres in front of it, the bed against the top wall.
    layout = {"bed_1": _pose(1.2, 2.95, 3 * math.pi / 2), "dresser_1": _pose(3.2, 0.25, math.pi / 2),
              "desk_1": _pose(3.2, 0.5 + front + 0.3, 3 * math.pi / 2)}
    issues = _measure("bedroom", layout)
    assert [row["uid"] for row in issues["storage_access_violations"]] == (["dresser_1"] if flagged else [])


def test_a_tv_on_a_stand_gets_the_support_checks():
    # A tv_stand used to count as a display too, which skipped every TV-to-support check.
    tv = {**next(asset for asset in GOLDEN["rooms"]["living"]["instances"] if asset["uid"] == "tv_1"),
          "placement_mode": "tabletop", "paired_support_uid": "tv_stand_1"}
    layout = {"tv_stand_1": _pose(2.5, 4.275, 3 * math.pi / 2),
              "tv_1": _pose(2.5, 4.275, 0.0, 0.55, on_top_of="tv_stand_1")}  # turned a quarter off its stand
    kinds = [row.get("kind") for row in _measure("living", layout, tv)["media_group_violations"]]
    assert "display_support_rotation" in kinds


def test_a_studio_without_a_dining_table_gets_no_dining_findings_and_one_storage_front():
    # The sofa stands 0.3 m in front of a cabinet; the studio has no dining group.
    cabinet = {"uid": "cabinet_1", "category": "cabinet", "width": 1.0, "depth": 0.45, "height": 0.9, "placement_mode": "floor"}
    layout = {"bed_1": _pose(4.5, 5.65, 3 * math.pi / 2), "sofa_1": _pose(5.0, 1.2, math.pi / 2),
              "cabinet_1": _pose(5.0, 0.225, math.pi / 2)}
    issues = _measure("studio", layout, cabinet)
    assert not any(findings for key, findings in issues.items() if key.startswith("dining_"))
    assert [row["uid"] for row in issues["storage_access_violations"]] == ["cabinet_1"]


def test_room_size_classes_follow_the_room_type():
    _, room = _bedroom((3.75, 4.0), {"bed": 1}, {"bed"})
    assert room["digest"]["room_scale"] == "medium"  # 15 m2: a normal bedroom
    assert room_scale(15.0, "living_room") == "compact"


def test_a_piece_taller_than_the_sill_in_front_of_a_window_is_a_non_blocking_finding():
    # The living room's window is on the top wall, x 2.45 to 3.55, sill 0.9 m: a 1.8 m bookcase and a 0.85 m sofa under it.
    layout = {"bookcase_1": _pose(3.0, 4.3, 3 * math.pi / 2), "sofa_1": _pose(1.2, 4.025, 3 * math.pi / 2)}
    issues = _measure("living", layout)
    assert [row["uid"] for row in issues["window_front_violations"]] == ["bookcase_1"]
    assert not has_blocking_issues(issues)


def test_ceiling_mounts_hang_from_the_rooms_wall_height():
    pendant = {"uid": "pendant_1", "category": "pendant_light", "width": 0.5, "depth": 0.5, "height": 0.6,
               "placement_mode": "ceiling_mounted"}
    _, room, assets = _golden_inputs({"room": "living"})
    room = {**room, "wall_height": 3.2}
    layout = normalize_layout({"pendant_1": _pose(2.5, 2.25, 0.0)}, [*assets, pendant], room["room_vertices"],
                              wall_height=room["wall_height"])
    assert layout["pendant_1"]["position"][2] == pytest.approx(3.2 - 0.6)

    def ceiling_findings(z):
        return analyze_layout({"pendant_1": _pose(2.5, 2.25, 0.0, z)}, [*assets, pendant], room["room_vertices"], [],
                              tuple(room["room_area"]), wall_height=room["wall_height"])["ceiling_mount_violations"]

    assert ceiling_findings(2.6) == [] and ceiling_findings(2.8 - 0.6) == ["pendant_1"]


@pytest.mark.parametrize("with_table", [True, False])
def test_a_generic_chair_with_a_dining_table_is_a_dining_chair(with_table):
    def record(asset_id, category):
        return {"asset_id": asset_id, "category": category, "width_m": 0.5, "depth_m": 0.5, "height_m": 0.8}

    items = [{"asset": record("chair_7", "chair"), "quantity": 2}]
    if with_table:
        items.append({"asset": record("table_3", "dining_table"), "quantity": 1})
    instances = build_instances(items, {"room_type": "dining_room" if with_table else "living_room"})
    chairs = [instance for instance in instances if instance["asset_id"] == "chair_7"]
    expected = "dining_chair" if with_table else "accent_chair"
    assert [instance["uid"] for instance in chairs] == [f"{expected}_1", f"{expected}_2"]


@pytest.mark.parametrize(("height", "error", "warning"), [(1.3, True, False), (0.6, False, True)])
def test_a_studio_dining_light_hangs_0_76_to_0_90_m_above_the_table(height, error, warning):
    # Top flush with the 2.7 m ceiling over a 0.75 m table: 0.65 m (too low, blocks) or 1.35 m (too high, advisory).
    selected = [{"uid": "dining_table_1", "category": "dining_table", "width": 1.2, "depth": 0.8, "height": 0.75},
                {"uid": "pendant_1", "category": "pendant_light", "width": 0.4, "depth": 0.4, "height": height,
                 "functional_group": "dining"}]
    result = _layout_preflight_for_assets(selected_assets=selected, budget=1000.0, total_cost=500.0,
                                          room={"room_type": "studio", "room_area": (6.0, 6.0), "wall_height": 2.7})
    assert any("dining light pendant_1" in text for text in result["errors"]) is error
    assert any("dining light pendant_1" in text for text in result["warnings"]) is warning


# --- issue #54 phase 2: completeness and selection sizes


def _record(asset_id, category, width=0.5, depth=0.5, height=0.5, **fields):
    return {"asset_id": asset_id, "category": category, "width_m": width, "depth_m": depth, "height_m": height,
            "placement_type": "floor", "is_purchasable": True, "price": 100, **fields}


PLANT = _record("plant", "planter", source_table="pipeline.decor_items", is_purchasable=False, price=None)
LAMP = _record("lamp", "floor_lamp", 0.4, 0.4, 1.6)
NIGHTSTANDS = [_record("nightstand", "nightstand", 0.5, 0.4, 0.55)] * 2
BED = _record("bed", "bed", 1.6, 2.1, 1.1)


def _validate(room_type, room_area, selected, pool=(), fit_step=None, **intent):
    """validate_selection of `selected` (one unit per record) from a pool of `selected` and `pool`."""
    intent = coerce_intent_packet({"normalized_prompt": "a room", **intent}, room_type=room_type)
    room = build_room_context(room_type=room_type, room_area=room_area, room_vertices=None, wall_height=2.7,
                              room_doors=[{"center": [room_area[0] / 2, 0.0], "width": 0.9, "depth": 0.05}],
                              room_windows=[], intent=intent)
    return validate_selection(items=[{"asset": record, "quantity": 1} for record in selected], intent=intent, room=room,
                              budget=100000, candidates=[*selected, *pool], fit_step=fit_step)


def _missing(result, piece):
    return any(error.startswith(f"MISSING REQUIRED: Select 1 {piece} ") for error in result["errors"])


@pytest.mark.parametrize(("selected", "pool", "missing"), [
    ([], [LAMP], True),
    ([LAMP], [], False),
    ([], [], False),  # no lamp in the pool: a catalog gap, not an error every turn
])
def test_a_living_room_requires_a_lamp(selected, pool, missing):
    living = [_record("sofa", "sofa", 2.0, 0.9, 0.8), _record("table", "coffee_table", 1.1, 0.6, 0.45),
              _record("rug", "rug", 2.4, 1.7, 0.01), PLANT]
    result = _validate("living_room", (4.0, 4.0), [*living, *selected], pool)  # 16 m2: compact, no accent seat required
    assert _missing(result, "lighting") is missing
    assert not missing or not result["valid"]


@pytest.mark.parametrize(("room_area", "missing"), [((3.75, 4.0), True), ((3.0, 3.5), False)])
def test_a_medium_bedroom_requires_a_dresser_or_wardrobe_and_a_compact_one_does_not(room_area, missing):
    bedroom = [BED, *NIGHTSTANDS, LAMP, _record("rug", "rug", 2.5, 1.7, 0.01), PLANT]
    result = _validate("bedroom", room_area, bedroom, [_record("dresser", "dresser", 1.2, 0.5, 0.8)])
    assert _missing(result, "clothes storage") is missing
    assert not any(error.startswith("MISSING REQUIRED") for error in result["errors"] if "clothes storage" not in error)


def test_a_medium_dining_room_requires_a_sideboard_and_a_pendant():
    dining = [_record("table", "dining_table", 1.8, 0.9, 0.75), *[_record("chair", "dining_chair", 0.5, 0.5, 0.8)] * 4, PLANT]
    sideboard, pendant = _record("sideboard", "sideboard", 1.6, 0.45, 0.8), _record("pendant", "pendant", 0.4, 0.4, 0.6)
    bare = _validate("dining_room", (3.5, 4.0), dining, [sideboard, pendant])  # 14 m2: medium
    furnished = _validate("dining_room", (3.5, 4.0), [*dining, sideboard, pendant])
    assert _missing(bare, "serving storage") and _missing(bare, "overhead lighting") and not bare["valid"]
    assert not _missing(furnished, "serving storage") and not _missing(furnished, "overhead lighting")


@pytest.mark.parametrize(("chairs", "valid"), [(3, False), (4, True), (6, True), (7, False)])
def test_a_spacious_dining_room_seats_four_to_six_by_default(chairs, valid):
    dining = [_record("table", "dining_table", 2.4, 1.0, 0.75), *[_record("chair", "dining_chair", 0.5, 0.5, 0.8)] * chairs,
              _record("sideboard", "sideboard", 1.6, 0.45, 0.8), _record("pendant", "pendant", 0.4, 0.4, 0.6), PLANT]
    result = _validate("dining_room", (4.2, 4.8), dining)  # 20 m2: spacious
    assert any(error.startswith("DINING GROUP") and "4–6 dining_chairs" in error for error in result["errors"]) is not valid


@pytest.mark.parametrize(("room_area", "dining_required"), [((4.0, 5.0), False), ((5.0, 6.0), True)])
def test_a_studio_requires_a_dining_group_from_25_m2(room_area, dining_required):
    studio = [BED, NIGHTSTANDS[0], LAMP, _record("sofa", "sofa", 1.8, 0.9, 0.8)]
    pool = [_record("table", "dining_table", 1.0, 0.8, 0.75), _record("chair", "dining_chair", 0.5, 0.5, 0.8)]
    result = _validate("studio", room_area, studio, pool)
    assert any(error.startswith("DINING GROUP") for error in result["errors"]) is dining_required


def _piece(uid, category, width, depth, height):
    return {"uid": uid, "asset_id": uid, "category": category, "width": width, "depth": depth, "height": height}


SOFA_2M = _piece("sofa_1", "sofa", 2.0, 0.9, 0.8)
DINING_TABLE = _piece("dining_table_1", "dining_table", 1.8, 0.9, 0.75)
BED_160 = _piece("bed_1", "bed", 1.6, 2.1, 1.1)
# A passing product of each kind, so a failing piece is an error rather than a warning.
PASSING = [_piece("coffee_table_9", "coffee_table", 1.2, 0.6, 0.45), _piece("side_table_9", "side_table", 0.5, 0.5, 0.55),
           _piece("pendant_9", "pendant_light", 0.4, 0.4, 0.5), _piece("rug_9", "rug", 0.8, 2.0, 0.01)]


@pytest.mark.parametrize(("room_type", "piece", "context", "finding"), [
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.2, 0.6, 0.35), [SOFA_2M], None),
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.2, 0.6, 0.34), [SOFA_2M], "error"),
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.2, 0.6, 0.50), [SOFA_2M], None),
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.2, 0.6, 0.51), [SOFA_2M], "error"),
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.0, 0.6, 0.45), [SOFA_2M], None),  # 0.5 x the sofa
    ("living_room", _piece("coffee_table_1", "coffee_table", 0.99, 0.6, 0.45), [SOFA_2M], "warning"),  # width is advisory
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.5, 0.6, 0.45), [SOFA_2M], None),  # 0.75 x the sofa
    ("living_room", _piece("coffee_table_1", "coffee_table", 1.51, 0.6, 0.45), [SOFA_2M], "warning"),
    ("living_room", _piece("side_table_1", "side_table", 0.5, 0.5, 0.45), [], None),
    ("living_room", _piece("side_table_1", "side_table", 0.5, 0.5, 0.44), [], "error"),
    ("living_room", _piece("side_table_1", "side_table", 0.5, 0.5, 0.65), [], None),
    ("living_room", _piece("side_table_1", "side_table", 0.5, 0.5, 0.66), [], "error"),
    ("dining_room", _piece("pendant_1", "pendant_light", 1.5, 0.6, 0.5), [DINING_TABLE], None),  # 0.15 m inside each edge
    ("dining_room", _piece("pendant_1", "pendant_light", 0.61, 0.61, 0.5), [DINING_TABLE], "error"),
    ("bedroom", _piece("rug_1", "rug", 2.5, 1.7, 0.01), [BED_160], None),  # the bed width plus 0.9 m
    ("bedroom", _piece("rug_1", "rug", 2.49, 1.7, 0.01), [BED_160], "error"),
    ("bedroom", _piece("rug_1", "rug", 0.9, 2.0, 0.01), [BED_160], None),  # a runner
])
def test_selection_size_rules_hold_at_their_boundaries(room_type, piece, context, finding):
    errors, warnings = _size_findings([piece, *context], [piece, *PASSING], room_type)
    assert [bool(errors), bool(warnings)] == [finding == "error", finding == "warning"]
    # With no passing product in the pool, a failing piece is only a warning.
    errors, warnings = _size_findings([piece, *context], [piece], room_type)
    assert [bool(errors), bool(warnings)] == [False, finding is not None]


@pytest.mark.parametrize(("depth", "seats_five"), [(0.75, True), (0.74, False)])
def test_a_table_under_0_75_m_deep_seats_one_long_side(depth, seats_five):
    # 1.2 m long sides seat two each at a 0.60 m pitch, the ends one each: five chairs need both long sides.
    assert bool(dining_fit_envelopes({"width": 1.2, "depth": depth}, [{"width": 0.5, "depth": 0.5}] * 5)) is seats_five


# --- issue #54 phase 4: crowding


RUG = _record("rug", "rug", 2.4, 1.7, 0.01)
LIVING = [_record("sofa", "sofa", 2.2, 1.0, 0.8), _record("table", "coffee_table", 1.2, 0.6, 0.45), RUG, LAMP, PLANT]
LIVING_EXTRAS = [*[_record("chair", "accent_chair", 0.85, 0.85, 0.8)] * 2, _record("cabinet", "cabinet", 1.8, 0.5, 0.8),
                 _record("media", "media_unit", 2.0, 0.5, 0.6)]
COMPACT_BEDROOM = [BED, *NIGHTSTANDS, LAMP, _record("rug", "rug", 2.5, 1.7, 0.01), PLANT]
SIDEBOARD = _record("sideboard", "sideboard", 1.6, 0.45, 0.8)


def _overcrowded(result):
    return [any(text.startswith("OVERCROWDED") for text in result[key]) for key in ("errors", "warnings")]


@pytest.mark.parametrize(("ottoman", "overcrowded"), [(False, False), (True, True)])
def test_a_medium_living_room_over_its_maximum_is_overcrowded(ottoman, overcrowded):
    # 22 m2 with a 0.81 m2 door zone: target 0.27 x 21.19 = 5.72 m2, maximum 0.35 x 21.19 = 7.42 m2.
    extra = [_record("ottoman", "ottoman", 1.0, 1.0, 0.45)] if ottoman else []
    result = _validate("living_room", (5.0, 4.4), [*LIVING, *LIVING_EXTRAS, *extra])
    assert result["metrics"]["total_footprint"] == (7.67 if ottoman else 6.67)
    assert _overcrowded(result) == [overcrowded, False]
    assert result["valid"] is not overcrowded, result["errors"]


@pytest.mark.parametrize(("extra", "fit_step", "finding"), [
    ([], None, None),
    ([SIDEBOARD], None, "error"),
    ([SIDEBOARD], "compact", "warning"),  # after a fit step, as the requirement errors
])
def test_a_compact_bedroom_fits_a_queen_bed_and_two_nightstands_but_not_a_sideboard_too(extra, fit_step, finding):
    # 10.5 m2: maximum 0.50 x 9.69 = 4.84 m2; the bed, nightstands, lamp and plant cover 4.17 m2, the sideboard 0.72 m2.
    result = _validate("bedroom", (3.0, 3.5), [*COMPACT_BEDROOM, *extra], fit_step=fit_step)
    assert _overcrowded(result) == [finding == "error", finding == "warning"]
    assert result["valid"] is (finding != "error"), result["errors"]


def test_required_pieces_alone_over_the_maximum_are_a_warning():
    # A 2.0 x 2.2 m bed with one nightstand, a lamp and the plant cover 5.01 m2 of the 4.84 m2 maximum.
    bedroom = [_record("bed", "bed", 2.0, 2.2, 1.1), NIGHTSTANDS[0], LAMP, _record("rug", "rug", 2.9, 1.7, 0.01), PLANT]
    result = _validate("bedroom", (3.0, 3.5), bedroom)
    assert _overcrowded(result) == [False, True]
    assert result["valid"], result["errors"]


def test_footprint_counts_floor_furniture_only():
    extras = [_record("art", "wall_art", 1.0, 0.05, 0.8, placement_type="wall"), _record("stand", "tv_stand", 1.6, 0.4, 0.55),
              _record("tv", "tv", 1.2, 0.08, 0.7, placement_type="surface"),
              _record("lamp2", "table_lamp", 0.3, 0.3, 0.5, placement_type="surface")]
    bare = _validate("living_room", (5.0, 4.4), LIVING)
    decorated = _validate("living_room", (5.0, 4.4), [*LIVING, *extras])
    # The rug, the wall art, the TV on its stand and the table lamp add nothing; the stand adds 1.6 x 0.4.
    assert bare["metrics"]["total_footprint"] == pytest.approx(2.2 + 0.72 + 0.16 + 0.25)
    assert decorated["metrics"]["total_footprint"] == pytest.approx(bare["metrics"]["total_footprint"] + 0.64)


@pytest.mark.parametrize(("room_area", "missing"), [((5.0, 4.4), False), ((6.0, 5.5), True)])
def test_a_medium_living_room_needs_no_accent_seat_and_a_spacious_one_needs_one(room_area, missing):
    result = _validate("living_room", room_area, LIVING, [_record("chair", "accent_chair", 0.85, 0.85, 0.8)])
    assert _missing(result, "accent seating") is missing


def test_the_final_check_never_removes_the_anchor():
    # The bed blocks the bedroom door and a nightstand overlaps it: the nightstand goes, the bed stays with its findings.
    _, room, assets = _golden_inputs({"room": "bedroom"})
    instances = [asset for asset in assets if asset["uid"] in {"bed_1", "nightstand_1"}]
    state = {"shared": {"room": room}, "instances": instances}
    state |= variant_stages._measure(state, {"bed_1": _pose(1.05, 0.8, 0.0), "nightstand_1": _pose(1.05, 0.8, 0.0)})
    current, removed = variant_stages._drop_items(state)
    assert removed == ["nightstand_1"]
    assert [asset["uid"] for asset in current["instances"]] == ["bed_1"]
    assert {finding["finding"]["uid"] for finding in current["blocking_findings"]} == {"bed_1"}
