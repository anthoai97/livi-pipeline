"""Room, selection, and layout rules on recorded legacy runs (livinit_pipeline/runs, Oct 06 2026) and synthetic rooms.

Fixture assets are legacy catalog rows rewritten as pipeline_assets_v2 records,
with the legacy uid kept as asset_id.
"""

import json
import math
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from app import variant_stages
from app.rules.layout.analysis import analyze_layout
from app.rules.layout.normalization import normalize_layout
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.door_geometry import room_walls
from app.rules.planner.room_facts import build_room_context, build_room_facts, format_room_facts_for_prompt
from app.rules.protected_paths import build_protected_paths, protected_path_polygon
from app.rules.selection.catalog import catalog_asset
from app.rules.selection.fit import fit_step_guidance
from app.rules.selection.preflight import _layout_preflight_for_assets
from app.rules.selection.validation import build_instances, validate_selection

# Three recorded legacy runs: each run's request, intent and selected instances per variant; run 1 (a dining room for
# four) also keeps its candidate assets, a passing selection, and a valid final layout.
RUNS = json.loads((Path(__file__).parent / "fixtures" / "legacy_runs.json").read_text())
# The rooms `_golden_inputs` builds: the RUNS variants, and synthetic living, bedroom, studio, and door-to-door rooms
# with their runtime instances.
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
    ("room_area", "warning", "dropped", "kept"),
    [
        # Medium bedrooms the requested pieces overfill (clearance-ring loads against the 55% comfortable load).
        # Only an overcrowded estimate (over 75%) starts selection in the compact fit step; a tight one is advisory.
        ((5.0, 5.5), False, {"desk", "office_chair"}, {"tv", "tv_stand", "wardrobe"}),
        ((4.5, 5.0), True, {"desk", "office_chair", "tv", "tv_stand"}, {"wardrobe"}),
    ],
)
def test_the_fit_estimate_keeps_the_bed_and_drops_whole_lower_priority_groups(room_area, warning, dropped, kept):
    requested = {"bed": 1, "nightstand": 2, "wardrobe": 1, "tv": 1, "tv_stand": 1, "desk": 1, "office_chair": 1}
    _, room = _bedroom(room_area, requested, {"bed"})
    recommended = room["fit"]["counts"]["recommended"]
    assert room["fit"]["severity"] == ("overcrowded" if warning else "tight")
    assert room["fit_warning"] is warning
    assert recommended["bed"] == 1
    assert not dropped & set(recommended)
    assert all(recommended[category] == requested[category] for category in kept)


def test_the_capped_fit_step_keeps_explicitly_requested_items_and_caps_optional_ones():
    # A 3 x 3 m bedroom: the fit estimate fits only the bed and dresser.
    requested = {"bed": 1, "nightstand": 2, "table_lamp": 2, "dresser": 1, "bench": 1}
    intent, room = _bedroom((3.0, 3.0), requested, {"bed", "nightstand", "table_lamp", "dresser"})

    assert room["fit"]["counts"]["recommended"] == {"bed": 1, "dresser": 1}
    assert "Selection target for this run: 1 bed, 1 dresser, 2 nightstands and 2 table lamps\n" in fit_step_guidance(
        "capped", room, intent)


def _golden_inputs(case):
    spec = GOLDEN["rooms"][case["room"]]
    if "run" in spec:
        run = RUNS[spec["run"]]
        intent, room = _room(run)
        return intent, room, [{**catalog_asset(record), "uid": record["uid"]} for record in run["layout_assets"][case["variant"]]]
    intent, room = _room(spec)
    return intent, room, spec["instances"]


def _room_args(room):
    return tuple(room["room_area"]), room["room_vertices"], room["room_doors"], room["room_windows"], room["protected_paths"]


# --- issue #61: the legacy room rules, measured on the fixture rooms


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


@pytest.mark.parametrize(("layout", "key", "flagged"), [
    # Sofa back 0.1 m off the left wall, front at x = 1.05; the table's near face 0.40 m or 0.55 m beyond it (0.46 to
    # 0.61 m passes).
    ({"sofa_1": _pose(0.575, 2.25, 0.0), "coffee_table_1": _pose(1.05 + 0.40 + 0.3, 2.25, 0.0)}, "sofa_coffee_table_violations", True),
    ({"sofa_1": _pose(0.575, 2.25, 0.0), "coffee_table_1": _pose(1.05 + 0.55 + 0.3, 2.25, 0.0)}, "sofa_coffee_table_violations", False),
    # A sofa 0.1 m off the wall passes the wall gap; a flush one does not.
    ({"sofa_1": _pose(0.575, 2.25, 0.0)}, "sofa_wall_gap_violations", False),
    ({"sofa_1": _pose(0.475, 2.25, 0.0)}, "sofa_wall_gap_violations", True),
], ids=["table_0.40", "table_0.55", "wall_0.1", "wall_flush"])
def test_sofa_gaps_to_the_coffee_table_and_the_wall(layout, key, flagged):
    assert bool(_measure("living", layout)[key]) is flagged


@pytest.mark.parametrize(("front", "flagged"), [(0.50, True), (0.60, False)])
def test_storage_needs_0_56_m_in_front(front, flagged):
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


@pytest.mark.parametrize(("height", "error"), [(1.3, True), (0.6, False)])
def test_a_studio_dining_light_hangs_at_least_0_76_m_above_the_table(height, error):
    # Top flush with the 2.7 m ceiling over a 0.75 m table: 0.65 m blocks; 1.35 m passes, with no maximum.
    selected = [{"uid": "dining_table_1", "category": "dining_table", "width": 1.2, "depth": 0.8, "height": 0.75},
                {"uid": "pendant_1", "category": "pendant_light", "width": 0.4, "depth": 0.4, "height": height,
                 "functional_group": "dining"}]
    result = _layout_preflight_for_assets(selected_assets=selected, budget=1000.0, total_cost=500.0,
                                          room={"room_type": "studio", "room_area": (6.0, 6.0), "wall_height": 2.7})
    assert any("dining light pendant_1" in text for text in result["errors"]) is error
    assert not any("dining light pendant_1" in text for text in result["warnings"])


def _record(asset_id, category, width=0.5, depth=0.5, height=0.5, **fields):
    return {"asset_id": asset_id, "category": category, "width_m": width, "depth_m": depth, "height_m": height,
            "placement_type": "floor", "is_purchasable": True, "price": 100, **fields}


PLANT = _record("plant", "planter", source_table="pipeline.decor_items", is_purchasable=False, price=None)
LAMP = _record("lamp", "floor_lamp", 0.4, 0.4, 1.6)
BED = _record("bed", "bed", 1.6, 2.1, 1.1)
RUG = _record("rug", "rug", 2.4, 1.7, 0.01)
CHAIR = _record("chair", "dining_chair", 0.5, 0.5, 0.8)


def _validate(room_type, room_area, selected, pool=(), fit_step=None, **intent):
    """validate_selection of `selected` (one unit per record) from a pool of `selected` and `pool`."""
    intent = coerce_intent_packet({"normalized_prompt": "a room", **intent}, room_type=room_type)
    room = build_room_context(room_type=room_type, room_area=room_area, room_vertices=None, wall_height=2.7,
                              room_doors=[{"center": [room_area[0] / 2, 0.0], "width": 0.9, "depth": 0.05}],
                              room_windows=[], intent=intent)
    return validate_selection(items=[{"asset": record, "quantity": 1} for record in selected], intent=intent, room=room,
                              budget=100000, candidates=[*selected, *pool], fit_step=fit_step)


@pytest.mark.parametrize(("room_type", "room_area", "core"), [
    ("living_room", (5.0, 6.0), [_record("sofa", "sofa", 2.2, 1.0, 0.8), _record("table", "coffee_table", 1.2, 0.6, 0.45),
                                 _record("cabinet", "cabinet", 1.8, 0.5, 0.8), RUG, PLANT]),
    ("bedroom", (4.0, 5.0), [BED, RUG, PLANT]),
    ("dining_room", (4.2, 4.8), [_record("table", "dining_table", 1.8, 0.9, 0.75), *[CHAIR] * 4, PLANT]),
    ("studio", (5.0, 6.0), [BED, _record("sofa", "sofa", 1.8, 0.9, 0.8), _record("table", "dining_table", 1.0, 0.8, 0.75),
                            *[CHAIR] * 2]),
])
def test_the_core_group_alone_passes_and_suggested_pieces_are_not_required(room_type, room_area, core):
    # The pool holds a lamp, nightstands, storage, a sideboard, a pendant and an accent chair; none is required.
    pool = [LAMP, _record("nightstand", "nightstand", 0.5, 0.4, 0.55), _record("dresser", "dresser", 1.2, 0.5, 0.8),
            _record("sideboard", "sideboard", 1.6, 0.45, 0.8), _record("pendant", "pendant_light", 0.4, 0.4, 0.6),
            _record("accent", "accent_chair", 0.85, 0.85, 0.8)]
    result = _validate(room_type, room_area, core, pool)
    assert result["valid"], result["errors"]
    # A living room or bedroom without a lamp only gets the advisory lighting warning.
    assert any(text.startswith("MISSING REQUIRED: No lighting") for text in result["warnings"]) is (
        room_type in {"living_room", "bedroom"})


@pytest.mark.parametrize("room_area", [(3.5, 4.0), (5.0, 6.0)])
def test_a_studio_of_any_size_requires_its_dining_group(room_area):
    studio = [BED, _record("sofa", "sofa", 1.8, 0.9, 0.8)]
    pool = [_record("table", "dining_table", 1.0, 0.8, 0.75), CHAIR]
    result = _validate("studio", room_area, studio, pool)
    assert any(error.startswith("DINING GROUP") for error in result["errors"])


@pytest.mark.parametrize(("room_area", "chairs", "fit_flexibility", "valid"), [
    ((3.5, 4.0), 4, "balanced", True),
    ((4.2, 4.8), 4, "balanced", True),  # spacious: still four by default
    ((4.2, 4.8), 6, "balanced", False),
    ((3.5, 4.0), 2, "balanced", False),
    ((3.5, 4.0), 2, "space_first", True),  # a space-first brief may seat two
])
def test_a_dining_room_seats_four_by_default_and_two_when_space_first(room_area, chairs, fit_flexibility, valid):
    dining = [_record("table", "dining_table", 2.0, 1.0, 0.75), *[CHAIR] * chairs, PLANT]
    result = _validate("dining_room", room_area, dining, fit_flexibility=fit_flexibility)
    assert any(error.startswith("DINING GROUP") for error in result["errors"]) is not valid


LIVING = [_record("sofa", "sofa", 2.2, 1.0, 0.8), _record("table", "coffee_table", 1.2, 0.6, 0.45), RUG, LAMP, PLANT]
LIVING_EXTRAS = [*[_record("chair", "accent_chair", 0.85, 0.85, 0.8)] * 2, _record("cabinet", "cabinet", 1.8, 0.5, 0.8),
                 _record("media", "media_unit", 2.0, 0.5, 0.6)]
COMPACT_BEDROOM = [BED, *[_record("nightstand", "nightstand", 0.5, 0.4, 0.55)] * 2, LAMP,
                   _record("rug", "rug", 2.5, 1.7, 0.01), PLANT]
SIDEBOARD = _record("sideboard", "sideboard", 1.6, 0.45, 0.8)


def _overcrowded(result):
    return [any(text.startswith("OVERCROWDED") for text in result[key]) for key in ("errors", "warnings")]


def test_density_counts_every_piece_but_rugs_at_twice_its_footprint():
    extras = [_record("art", "wall_art", 1.0, 0.05, 0.8, placement_type="wall"),
              _record("lamp2", "table_lamp", 0.3, 0.3, 0.5, placement_type="surface")]
    bare = _validate("living_room", (5.0, 4.4), LIVING)
    decorated = _validate("living_room", (5.0, 4.4), [*LIVING, *extras])
    # The rug adds nothing; wall art and a table lamp count like any other piece.
    assert bare["metrics"]["total_footprint"] == pytest.approx(2 * (2.2 + 0.72 + 0.16 + 0.25))
    assert decorated["metrics"]["total_footprint"] == pytest.approx(bare["metrics"]["total_footprint"] + 2 * (0.05 + 0.09))


@pytest.mark.parametrize(("ottoman", "overcrowded"), [(False, False), (True, True)])
def test_a_living_room_is_overcrowded_when_twice_the_footprint_passes_the_usable_floor(ottoman, overcrowded):
    # 14 m2 with a 0.54 m2 door zone: 13.46 m2 usable; the room covers 6.68 m2 (13.35 m2 doubled), an ottoman 1 m2 more.
    extra = [_record("ottoman", "ottoman", 1.0, 1.0, 0.45)] if ottoman else []
    result = _validate("living_room", (3.5, 4.0), [*LIVING, *LIVING_EXTRAS, *extra])
    assert result["metrics"]["footprint_max"] == pytest.approx(13.46)
    assert _overcrowded(result) == [overcrowded, False]
    assert result["valid"] is not overcrowded, result["errors"]


@pytest.mark.parametrize(("extra", "fit_step", "finding"), [
    ([], None, None),
    ([SIDEBOARD, SIDEBOARD], None, "error"),
    ([SIDEBOARD, SIDEBOARD], "compact", "warning"),  # after a fit step
])
def test_a_compact_bedroom_over_its_usable_floor_is_overcrowded_until_a_fit_step(extra, fit_step, finding):
    # 10.5 m2 with a 0.54 m2 door zone: 9.96 m2 usable; the bed, nightstands, lamp and plant double to 8.34 m2,
    # two sideboards add 2.88 m2.
    result = _validate("bedroom", (3.0, 3.5), [*COMPACT_BEDROOM, *extra], fit_step=fit_step)
    assert _overcrowded(result) == [finding == "error", finding == "warning"]


def test_required_pieces_alone_over_the_usable_floor_are_a_warning():
    # A 2.0 x 2.2 m bed and the plant double to 9.3 m2 of a 2.5 x 3.0 m room's 6.96 m2 usable floor.
    bedroom = [_record("bed", "bed", 2.0, 2.2, 1.1), _record("rug", "rug", 2.0, 1.7, 0.01), PLANT]
    result = _validate("bedroom", (2.5, 3.0), bedroom)
    assert _overcrowded(result) == [False, True]


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


# --- issue #53 phase 1: real walls, openings, area and paths for any room shape


def shape(kind: str, w: float, l: float) -> list[list[float]]:
    """The builder's presets and its Z and U templates at their default proportions (docs/plans/53-room-shapes.md)."""
    if kind == "L":  # top-right corner cut, 0.4w x 0.4l
        keep_w, keep_l = w - 0.4 * w, l - 0.4 * l
        return [[0, 0], [w, 0], [w, keep_l], [keep_w, keep_l], [keep_w, l], [0, l]]
    if kind == "alcove":  # back-wall recess, w/3 wide, 0.3l deep, centred
        x1, x2, inner = w / 2 - w / 6, w / 2 + w / 6, l - 0.3 * l
        return [[0, 0], [w, 0], [w, inner], [x2, inner], [x2, l], [x1, l], [x1, inner], [0, inner]]
    if kind == "T":  # stem 0.4w wide, centred; bar 0.45l deep
        stem_l, stem_r, bar_front = w / 2 - 0.2 * w, w / 2 + 0.2 * w, l - 0.45 * l
        return [[stem_l, 0], [stem_r, 0], [stem_r, bar_front], [w, bar_front], [w, l], [0, l], [0, bar_front], [stem_l, bar_front]]
    if kind == "angled":  # top-left corner cut, 0.27w x 0.37l
        return [[0, 0], [w, 0], [w, l], [0.27 * w, l], [0, l - 0.37 * l]]
    if kind == "Z":  # two opposite corners cut, w/3 x l/3 each
        a, b = w / 3, l / 3
        return [[a, 0], [w, 0], [w, l - b], [w - a, l - b], [w - a, l], [0, l], [0, b], [a, b]]
    if kind == "U":  # back-wall notch w/3 wide, l/2 deep, centred
        a, b = w / 3, l / 2
        return [[0, 0], [w, 0], [w, l], [w / 2 + a / 2, l], [w / 2 + a / 2, l - b], [w / 2 - a / 2, l - b], [w / 2 - a / 2, l], [0, l]]
    return [[0, 0], [w, 0], [w, l], [0, l]]


WALL_LABELS = {
    "rectangle": ["Front", "Right", "Back", "Left"],
    "L": ["Front", "Right", "Back (inner)", "Right (inner)", "Back", "Left"],
    "alcove": ["Front", "Right", "Back (inner right)", "Right (inner)", "Back", "Left (inner)", "Back (inner left)", "Left"],
    "T": ["Front", "Right (inner)", "Front (inner right)", "Right", "Back", "Left", "Front (inner left)", "Left (inner)"],
    "angled": ["Front", "Right", "Back", "Back-left", "Left"],
    "Z": ["Front", "Right", "Back (inner)", "Right (inner)", "Back", "Left", "Front (inner)", "Left (inner)"],
    "U": ["Front", "Right", "Back (right)", "Left (inner)", "Back (inner)", "Right (inner)", "Back (left)", "Left"],
}


def _door(x, y, width=0.9):
    return {"center": [x, y], "width": width, "depth": 0.05, "height": 2.08}


@pytest.mark.parametrize("kind", list(WALL_LABELS))
def test_every_edge_is_a_wall_with_the_builders_label(kind):
    walls = room_walls(shape(kind, 4.0, 5.0))
    assert [wall.label for wall in walls] == WALL_LABELS[kind]
    assert [wall.index for wall in walls] == list(range(len(walls)))
    # A corner wall ("Back-left") faces no cardinal side; every other wall does.
    assert [wall.label for wall in walls if wall.facing is None] == (["Back-left"] if kind == "angled" else [])


def test_z_room_facts_report_eight_walls_and_the_polygon_area_with_a_door_on_an_inner_wall():
    vertices = shape("Z", 4.0, 5.0)
    inner_back_y = 5.0 - 5.0 / 3  # wall 2, Back (inner), runs from (4, y) to (2.67, y)
    facts = build_room_facts(room_area=(4.0, 5.0), room_vertices=vertices, room_doors=[_door(3.3, inner_back_y)])

    assert facts["shape"] == "polygon" and len(facts["walls"]) == 8 and facts["area"] == pytest.approx(15.56, abs=0.01)
    opening = facts["openings"]["doors"][0]["opening"]
    assert (opening["wall"], opening["wall_label"], opening["facing"]) == (2, "Back (inner)", "top")
    assert opening["center"] == pytest.approx([3.3, inner_back_y - 0.025], abs=1e-3) and opening["inward_normal"] == [0.0, -1.0]
    assert facts["walls"][2]["openings"] == [{"kind": "door", "index": 0, "at": 0.7, "span": [0.25, 1.15]}]
    assert "- wall 2 (Back (inner)), 1.33 m, door at 0.70 m" in format_room_facts_for_prompt(facts)
    # The two 3.33 m outer side walls rank first, tied as a rectangle's would be: by facing name, then index.
    assert [wall["label"] for wall in facts["candidate_anchor_walls"][:3]] == ["Left", "Right", "Front"]


def test_opposing_doors_in_different_pockets_of_a_z_room_get_a_route_through_the_throat():
    vertices = shape("Z", 4.0, 5.0)
    # Right wall of the lower-right pocket and left wall of the upper-left pocket: a straight band leaves the room.
    [path] = build_protected_paths(vertices, [_door(4.0, 0.6), _door(0.0, 2.2)])

    assert path["axis"] == "routed" and path["wall_pair"] == [1, 5] and path["wall_labels"] == ["Right", "Left"]
    assert path["required_width"] == 0.91 and path["centerline"][0] == [4.0, 0.6] and path["centerline"][-1] == [0.0, 2.2]
    passage = protected_path_polygon(path)
    assert Polygon(vertices).buffer(1e-6).covers(passage)
    assert passage.intersection(box(4 / 3, 5 / 3, 8 / 3, 10 / 3)).area > 0.91 * 1.0  # crosses the throat

    # Doors near corners (within half the path width) still get a route: it starts at the nearest free point.
    [path] = build_protected_paths(vertices, [_door(4 / 3 + 0.2, 0.0), _door(0.2, 5.0)])
    assert path["axis"] == "routed" and path["wall_labels"] == ["Front", "Back"]
    assert Polygon(vertices).buffer(1e-6).covers(protected_path_polygon(path))


def test_a_rectangle_keeps_the_straight_path_between_opposing_doors_and_adjacent_doors_get_none():
    vertices = shape("rectangle", 4.0, 5.0)
    left, right, bottom = _door(0.0, 2.5), _door(4.0, 2.5), _door(2.0, 0.0)

    [path] = build_protected_paths(vertices, [left, right])
    assert {key: path[key] for key in ("axis", "center", "width", "depth", "required_width", "wall_pair")} == {
        "axis": "x", "center": [2.0, 2.5], "width": 4.0, "depth": pytest.approx(0.91), "required_width": 0.91, "wall_pair": [3, 1]}
    assert "polygon" not in path
    assert build_protected_paths(vertices, [left, bottom]) == []
