"""Real stages end to end with a fake genai client and fixed search results.

The room, candidates, selection, and layout come from the recorded legacy dining
room for four (fixtures/legacy_runs.json), which passed selection and the final
layout check.
"""

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from test_runtime import FakeJevClient, of_type, parse

from app import shared_stages, variant_stages
from app.contracts import PipelineRequest
from app.graph import MAX_RESELECTIONS, MAX_SELECTION_TURNS
from app.jev import Jev, switches
from app.llm import GeminiModel
from app.main import create_app
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.run import RunContext, StageContext

RUN = json.loads((Path(__file__).parent / "fixtures" / "legacy_runs.json").read_text())[1]
REQUEST = {
    "user_intent": RUN["intent"]["normalized_prompt"],
    **{key: RUN["request"][key] for key in ("budget", "room_type", "room_area", "room_vertices", "wall_height", "room_doors", "room_windows")},
}
POOL = [
    {"image_url": "https://example.com/a.png", "model_url": "https://example.com/a.glb", "colors": [], "styles": [],
     "materials": [], "brand": None, "description": "", "currency": "USD", **record}
    for record in RUN["assets"].values()
]
TURN = RUN["selection_turns"][0]  # table, four chairs, sideboard, decor plant
LAYOUT = RUN["layouts"][0]["layout"]  # variant 0 final layout, valid
INSTANCE_KEYS = {
    "dining_table_3": "dining_table_1",
    "dining_chair_39_1": "dining_chair_1",
    "dining_chair_39_2": "dining_chair_2",
    "dining_chair_39_3": "dining_chair_3",
    "dining_chair_39_4": "dining_chair_4",
    "sideboard_1": "sideboard_1",
    "decor_047ff49d-65d8-4f1e-bd46-256e38ee5da4": "planter_1",
}
PLACEMENTS = {INSTANCE_KEYS[uid]: pose for uid, pose in LAYOUT.items()}
TABLE_CENTER = PLACEMENTS["dining_table_1"]["position"]


def intent_packet(*extra_items: dict) -> dict:
    items = [
        {"colors": [], "styles": [], "materials": [], **item}
        for item in [*RUN["intent"]["requested_items"], *extra_items]
    ]
    return {**RUN["intent"], "requested_items": items}


def selection(items: list[tuple[str, int]] = TURN["items"]) -> dict:
    return {"selected_assets": [{"uid": uid} for uid, count in items for _ in range(count)], "gaps": ""}


def layout(moved: dict[str, list[float]] | None = None) -> dict:
    """Poses that move every item from the seed to the legacy layout, with `moved` positions overriding."""
    moved = moved or {}
    return {"poses": [
        {"uid": key, "x": moved.get(key, pose["position"])[0], "y": moved.get(key, pose["position"])[1],
         "rotation_z": pose["rotation"][2]}
        for key, pose in PLACEMENTS.items()
    ]}


def chair_pose(position: list[float]) -> dict:
    return {"poses": [{"uid": "dining_chair_1", "x": position[0], "y": position[1],
                       "rotation_z": PLACEMENTS["dining_chair_1"]["rotation"][2]}]}


# A chair pushed into the table overlaps it (P0); the correction moves it back.
OVERLAPPING = layout({"dining_chair_1": TABLE_CENTER})
FIXED = chair_pose(PLACEMENTS["dining_chair_1"]["position"])
NO_CHANGES = {"poses": []}
# Prompt openings of the three stages that answer with Correction poses.
LAYOUT_PROMPTS = {"place": "Edit the rule-based seed layout", "correct": "Solve the measurable placement violations",
                  "refine": "Review this layout after collision fixing"}


def poses(place: dict, correct: dict | Exception = FIXED, refine: dict | Exception = NO_CHANGES):
    """The Correction answer for each layout stage, told apart by its prompt. An exception fails that call."""
    answers = {"place": place, "correct": correct, "refine": refine}

    def answer(prompt: str) -> dict:
        found = next(answers[stage] for stage, opening in LAYOUT_PROMPTS.items() if prompt.startswith(opening))
        if isinstance(found, Exception):
            raise found
        return found

    return answer


# What a call that used its attempts returns, as in a benchmark run.
DEADLINE = RuntimeError("504 DEADLINE_EXCEEDED")


class FakeGenai:
    """genai.Client stand-in that answers each schema with `responses[schema title]`.

    A response is a dict, or a function of the prompt that returns one.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.prompts: list[tuple[str, str]] = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model: str, contents: str, config: types.GenerateContentConfig):
        title = config.response_json_schema["title"]
        self.prompts.append((title, contents))
        answer = self.responses[title]
        answer = answer(contents) if callable(answer) else answer
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=json.dumps(answer))]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10),
            model_version=model,
        )

    def of(self, title: str) -> list[str]:
        return [prompt for name, prompt in self.prompts if name == title]


@pytest.fixture(autouse=True)
def default_switches(monkeypatch):
    for switch in ("JEV_USES", "REFINEMENT", "PRODUCT_REUSE_RATE", "LLM_STAGE_MODELS"):
        monkeypatch.delenv(switch, raising=False)


@pytest.fixture
def searches(monkeypatch):
    """Fixed search results: the pool records in the slot's categories with its strict attributes."""

    def search(connection, vector, **filters):
        return [
            row for row in POOL
            if row["category"] in filters["categories"]
            and all(value in row[field] for field in ("colors", "styles", "materials") for value in filters.get(field, []))
        ]

    monkeypatch.setattr(shared_stages, "search_assets", search)
    monkeypatch.setattr(shared_stages, "embed_query", lambda client, text: [0.0])


def run_pipeline(tmp_path: Path, genai: FakeGenai, jev: FakeJevClient | None = None) -> tuple[list[dict], dict]:
    app = create_app(model=GeminiModel(genai), jev=Jev(jev or FakeJevClient()), connect=lambda: None, runs_dir=tmp_path)

    async def main() -> str:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return (await client.post("/pipeline", json=REQUEST)).text

    events = parse(asyncio.run(main()))
    [record] = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    return events, record


def stage_runs(record: dict, stage: str, index: int | None) -> int:
    return sum(1 for s in record["stages"] if s["stage"] == stage and s["variant_index"] == index)


def test_shared_stages_run_once_and_each_variant_runs_its_stages(tmp_path, searches, monkeypatch):
    monkeypatch.setenv("REFINEMENT", "jev")
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    monkeypatch.setitem(sideboard, "mount_type", "wall_secured")
    monkeypatch.setitem(sideboard, "features", ["soft close drawers"])
    # The ottoman has no catalog product, so it is a gap and the run continues without it.
    ottoman = {"label": "velvet ottoman", "canonical_category": "ottoman", "count": 1, "exact": False,
               "optional": False, "acceptable_substitutes": [], "descriptors": ["velvet"]}
    genai = FakeGenai({"IntentPacket": intent_packet(ottoman), "Selection": selection(), "Correction": poses(OVERLAPPING)})

    events, record = run_pipeline(tmp_path, genai)

    for stage in ("interpret", "room", "retrieve", "rank"):
        assert stage_runs(record, stage, None) == 1
    for index in range(3):
        for stage in ("select", "place", "repair", "correct", "refine", "validate"):
            assert stage_runs(record, stage, index) == 1
    calls = {}
    for call in record["model_calls"]:
        calls[call["stage"]] = calls.get(call["stage"], 0) + 1
    assert calls == {"interpret": 1, "select": 3, "place": 3, "correct": 3, "refine": 3}
    # Jev: one rank request per non-empty slot and direction, one selection check, and one refinement trigger per variant.
    uses = {(call["use"], call["variant_index"]) for call in record["jev_calls"]}
    assert uses == {(use, index) for use in ("rank", "check", "refine") for index in range(3)}
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        # Repair moved the chair out of the table; the correction finished the fix.
        assert [note for note in notes if note.startswith("repair: p0 cleanup moved, kept;")]
        assert "refinement applied: 0 poses, score [0, 0, 0] -> [0, 0, 0]" in notes

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert sorted(variant["variant_index"] for variant in ready) == [0, 1, 2]
    for variant in ready:
        assert variant["total_cost"] == 899 + 4 * 510 + 899
        assert sorted(variant["render_manifest"]["layout"]) == sorted(PLACEMENTS)
        assert len(variant["selected_assets"]) == 7
        assert variant["render_manifest"]["assets"]["sideboard_1"]["mount_type"] == "wall_secured"
        assert variant["render_manifest"]["assets"]["sideboard_1"]["features"] == ["soft close drawers"]
    assert [(v["outcome"], v["reason"]) for v in record["variants"]] == [("ready", None)] * 3

    [gap] = [slot for slot in record["slots"] if slot["slot"] == "ottoman"]
    assert gap["gap"] is True and gap["candidates"] == 0 and "gap" in gap["note"]
    assert all("CATALOG GAPS" in prompt and "velvet ottoman" in prompt for prompt in genai.of("Selection"))
    placing = [prompt for prompt in genai.of("Correction") if not prompt.startswith(LAYOUT_PROMPTS["refine"])]
    for prompt in genai.of("Selection") + placing:
        assert "wall_secured" in prompt and "soft close drawers" in prompt
    # Variant directions: none for variant 0, the legacy dining texts for 1 and 2.
    directions = ["warm, rounded dining table", "compact, structured dining table"]
    assert [sum(text in prompt for prompt in genai.of("Selection")) for text in directions] == [1, 1]


def test_oversized_selection_steps_to_compact_then_capped_counts(tmp_path, searches):
    # Ten sideboards overcrowd the room on every turn.
    oversized = [(uid, 10 if uid == "sideboard_1" else count) for uid, count in TURN["items"]]
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(oversized), "Correction": poses(layout())})

    events, record = run_pipeline(tmp_path, genai)

    assert {e["reason"] for e in of_type(events, "variant_failed")} == {"asset_selection_failed"}
    compact, capped = "=== ADVISORY FIT ESTIMATE ===", "=== ACCEPTED FIT DECISION ==="
    marker = "VARIANT DIRECTION: Choose a warm, rounded dining table"
    prompts = [prompt for prompt in genai.of("Selection") if marker in prompt]
    assert len(prompts) == MAX_SELECTION_TURNS
    assert compact not in prompts[0] and capped not in prompts[0]
    assert compact in prompts[1] and capped not in prompts[1]
    assert all(capped in prompt for prompt in prompts[2:])
    assert "Selection target for this run: 4 dining chairs and 1 dining table" in prompts[2]
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert notes == ["selection turn 2: fit step compact", "selection turn 3: fit step capped"]
        assert stage_runs(record, "select", index) == MAX_SELECTION_TURNS


def test_required_item_searched_without_its_limits_can_pass_selection(tmp_path, searches):
    # No catalog dining table is tagged scandinavian, so the required table slot drops
    # that limit, and selection no longer requires it.
    table, chairs = RUN["intent"]["requested_items"]
    packet = {**intent_packet(), "requested_items": [
        {**table, "colors": [], "styles": ["scandinavian"], "materials": []},
        {**chairs, "colors": [], "styles": [], "materials": []},
    ]}
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection(), "Correction": poses(layout())})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    [slot] = [slot for slot in record["slots"] if slot["slot"] == "dining_table"]
    assert slot["candidates"] == 2 and "without them" in slot["note"]
    assert all("candidates ignore them" in prompt for prompt in genai.of("Selection"))


def test_blocking_layout_reselects_once_with_placement_feedback_then_fails(tmp_path, searches):
    # The correction pushes the chair back into the table, so it never improves the layout.
    genai = FakeGenai({
        "IntentPacket": intent_packet(),
        "Selection": selection(),
        "Correction": poses(OVERLAPPING, correct=chair_pose(TABLE_CENTER)),
    })

    events, record = run_pipeline(tmp_path, genai)

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"layout_validation_failed"}
    assert all("unresolved blocking issues" in e["errors"][0] for e in failed)
    for index in range(3):
        for stage in ("select", "place", "repair", "validate"):
            assert stage_runs(record, stage, index) == 1 + MAX_RESELECTIONS
        # Each layout stops correcting at its first proposal that does not improve.
        assert stage_runs(record, "correct", index) == 1 + MAX_RESELECTIONS
        assert stage_runs(record, "refine", index) == 0
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert any(note.startswith("reselection after the layout failed: Final layout has unresolved") for note in notes)
        assert "selection turn 2: fit step compact" in notes
    reselections = [prompt for prompt in genai.of("Selection") if "LAYOUT FAILED THE FINAL CHECK" in prompt]
    assert len(reselections) == 3
    for prompt in reselections:
        assert "Items named in the remaining blocking findings: dining_chair_39 (dining_chair)" in prompt
        assert "=== ADVISORY FIT ESTIMATE ===" in prompt and "FAILED VALIDATION" not in prompt
    assert events[-1]["type"] == "complete" and events[-1]["data"]["variants"] == []


def test_place_must_pose_every_item_the_seed_skipped(tmp_path, searches, monkeypatch):
    seed = variant_stages.generate_deterministic_layout_with_report

    def seed_without_sideboard(instances, *args, **kwargs):
        layout, report = seed(instances, *args, **kwargs)
        layout.pop("sideboard_1", None)
        return layout, {**report, "skipped": [{"uid": "sideboard_1", "category": "sideboard",
                                               "status": "skipped_no_comfortable_position", "reason": "no comfortable position"}]}

    monkeypatch.setattr(variant_stages, "generate_deterministic_layout_with_report", seed_without_sideboard)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Correction": poses(NO_CHANGES)})

    events, record = run_pipeline(tmp_path, genai)

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"variant_error"} and len(failed) == 3
    assert all("missing=['sideboard_1']" in e["message"] for e in failed)
    places = [prompt for prompt in genai.of("Correction") if prompt.startswith(LAYOUT_PROMPTS["place"])]
    assert len(places) == 3 * 2  # one retry each
    assert all("- sideboard_1 (sideboard): no comfortable position" in prompt for prompt in places)
    assert sum("YOUR PREVIOUS RESPONSE WAS REJECTED" in prompt for prompt in places) == 3

    # A pose for the skipped item completes the layout.
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Correction": poses(layout())})
    events, record = run_pipeline(tmp_path / "posed", genai)
    assert len(of_type(events, "variant_ready")) == 3
    assert [note["text"] for note in record["notes"] if note["text"].startswith("seed skipped")] == [
        "seed skipped sideboard_1 (skipped_no_comfortable_position)"] * 3


def test_refinement_rolls_back_a_worse_layout(tmp_path, searches, monkeypatch):
    monkeypatch.setenv("REFINEMENT", "jev")
    # The refinement pushes the chair into the table, which adds a blocking overlap.
    genai = FakeGenai({
        "IntentPacket": intent_packet(),
        "Selection": selection(),
        "Correction": poses(OVERLAPPING, refine=chair_pose(TABLE_CENTER)),
    })

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        position = variant["render_manifest"]["layout"]["dining_chair_1"]["position"]
        assert position[:2] == pytest.approx(PLACEMENTS["dining_chair_1"]["position"][:2], abs=0.05)
    notes = [note["text"] for note in record["notes"] if note["text"].startswith("refinement")]
    assert len(notes) == 3 and all(note.startswith("refinement rolled back: score [0, 0, 0] -> [1,") for note in notes)


def test_failed_refinement_call_keeps_the_layout(tmp_path, searches, monkeypatch):
    monkeypatch.setenv("REFINEMENT", "jev")
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(),
                       "Correction": poses(OVERLAPPING, refine=DEADLINE)})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        position = variant["render_manifest"]["layout"]["dining_chair_1"]["position"]
        assert position[:2] == pytest.approx(PLACEMENTS["dining_chair_1"]["position"][:2], abs=0.05)
    assert all("504 DEADLINE_EXCEEDED" in call["error"] for call in record["model_calls"] if call["stage"] == "refine")
    notes = [note["text"] for note in record["notes"] if note["text"].startswith("refinement")]
    assert len(notes) == 3
    assert all(note.startswith("refinement call failed, kept the layout: ModelCallError") for note in notes)


def test_failed_correction_call_ends_correction_with_the_best_layout(tmp_path, searches):
    # Repair leaves a blocking finding, so each layout needs a correction, and every correction call fails.
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(),
                       "Correction": poses(OVERLAPPING, correct=DEADLINE)})

    events, record = run_pipeline(tmp_path, genai)

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"layout_validation_failed"} and len(failed) == 3
    for index in range(3):
        # One failed call per layout ends its correction; the failed layout then reselects as usual.
        assert stage_runs(record, "correct", index) == 1 + MAX_RESELECTIONS
        assert stage_runs(record, "validate", index) == 1 + MAX_RESELECTIONS
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert sum(note.startswith("correction call failed, kept the best layout: ModelCallError") for note in notes) == 2
        assert any(note.startswith("reselection after the layout failed") for note in notes)


def test_correction_escalates_once_per_layout_to_the_correct_escalate_model(tmp_path, searches, monkeypatch):
    # Every correction call fails, so each layout's first proposal escalates and the escalated one stops correction.
    monkeypatch.setenv("LLM_STAGE_MODELS", "correct=gemini-3.5-flash-lite:minimal,correct_escalate=gemini-3.8-flash")
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(),
                       "Correction": poses(OVERLAPPING, correct=DEADLINE)})

    events, record = run_pipeline(tmp_path, genai)

    assert {e["reason"] for e in of_type(events, "variant_failed")} == {"layout_validation_failed"}
    for index in range(3):
        models = [call["model"] for call in record["model_calls"] if call["stage"] == "correct" and call["variant_index"] == index]
        # Lite first, then the escalation model, for the layout and again after its reselection.
        assert models == ["gemini-3.5-flash-lite", "gemini-3.8-flash"] * (1 + MAX_RESELECTIONS)
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert notes.count("correction escalated to gemini-3.8-flash") == 1 + MAX_RESELECTIONS


def test_jev_style_and_attribute_failures_reject_the_selection(tmp_path, searches, monkeypatch):
    def answer(text: str) -> float:
        if "style and palette" in text and "sideboard_1" in text:
            return 0.1
        return 0.2 if 'requirement "soft neutral"' in text else 0.9

    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Correction": poses(layout())})

    events, record = run_pipeline(tmp_path, genai, FakeJevClient(answer))

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"asset_selection_failed"}
    for event in failed:
        assert "SELECTION CONSTRAINT AUDIT: sideboard_1 does not match the style and palette of the anchor dining_table_3" in " ".join(event["errors"])
        assert "SELECTION CONSTRAINT AUDIT: attribute constraint \"soft neutral\" is not satisfied by ['dining_chair_39']" in event["errors"]
    rejections = [note["text"] for note in record["notes"] if note["text"].startswith("jev check rejected")]
    assert len(rejections) == 3 * MAX_SELECTION_TURNS
    assert rejections[0] == "jev check rejected: style ['sideboard_1']; attributes \"soft neutral\" by ['dining_chair_39']"
    # Each check is one Jev request: the chairs and sideboard against the table, and four attribute constraints.
    [check] = [call for call in record["jev_calls"] if call["use"] == "check" and call["variant_index"] == 0][:1]
    assert check["questions"] == 2 + 4

    # With the check switched off, code checks alone pass the same selection.
    monkeypatch.setenv("JEV_USES", "rank")
    events, _ = run_pipeline(tmp_path / "check_off", genai, FakeJevClient(answer))
    assert len(of_type(events, "variant_ready")) == 3


def test_jev_failure_never_fails_a_variant(tmp_path, searches, monkeypatch):
    monkeypatch.setenv("REFINEMENT", "jev")
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Correction": poses(layout())})

    events, record = run_pipeline(tmp_path, genai, FakeJevClient(fail=RuntimeError("jev down")))

    assert len(of_type(events, "variant_ready")) == 3
    assert record["jev_calls"] and all("jev down" in call["error"] for call in record["jev_calls"])
    for index in range(3):
        notes = {note["text"].split(",")[0] for note in record["notes"] if note["variant_index"] == index}
        assert {"jev rank failed", "jev check failed", "jev refine failed"} <= notes
    # A failed refinement trigger skips refinement.
    assert stage_runs(record, "refine", 0) == 1 and not [call for call in record["model_calls"] if call["stage"] == "refine"]


# --- rank -------------------------------------------------------------------


def candidate(asset_id: str, category: str, **fields) -> dict:
    """A pool record whose title is the asset id with spaces, as Jev questions show it."""
    return {"asset_id": asset_id, "title": asset_id.replace("_", " "), "category": category, "width_m": 1.0,
            "depth_m": 1.0, "height_m": 1.0, "price": 500, "brand": None, **fields}


# 12 sofas, sofa_0 from the preferred brand, and 5 planters.
SOFAS = [candidate(f"sofa_{n}", "sofa", brand="Acme" if n == 0 else None) for n in range(12)]
PLANTERS = [candidate(f"plant_{n}", "planter") for n in range(5)]


def by_number(text: str) -> float:
    """Yes probability grows with the product number, the same for every direction."""
    return int(text.split("Product: ")[1].split(" (")[0].split()[-1]) / 20


def rank_pools(slots: list[tuple[str, str, list[dict]]], jev: FakeJevClient, *, rate: float = 0.0,
         uses: frozenset[str] = frozenset({"rank"})) -> tuple[list[dict[str, list[dict]]], RunContext]:
    """Run the shared rank stage on slots given as (id, kind, candidates) in plan order."""
    request = PipelineRequest(**REQUEST)
    run = RunContext("run", request, SimpleNamespace(client=None), 3, jev=Jev(jev), jev_uses=uses, product_reuse_rate=rate)
    state = {"request": request, "intent": {"normalized_prompt": "a cozy dining room", "style_hints": ["cozy"]},
             "room": {"room_type": "dining_room"},
             "slots": [{"id": slot_id, "kind": kind, "label": slot_id, "preferred_brands": ["Acme"] if slot_id == "sofa" else []}
                       for slot_id, kind, _ in slots],
             "pool": {slot_id: rows for slot_id, _, rows in slots}}
    update = asyncio.run(variant_stages.rank(state, StageContext(run, "rank", None)))
    return update["pools"], run


def ids(pools: list[dict[str, list[dict]]], slot_id: str) -> list[list[str]]:
    return [[record["asset_id"] for record in pool[slot_id]] for pool in pools]


def test_rank_deals_disjoint_pools_with_rotating_first_pick_and_shares_small_slots():
    jev = FakeJevClient(by_number)

    pools, run = rank_pools([("sofa", "requested", SOFAS), ("planter", "decor", PLANTERS)], jev)

    # Every direction ranks sofa_0 (preferred brand), then sofa_11 down to sofa_1. Round r starts at variant r.
    assert ids(pools, "sofa") == [["sofa_0", "sofa_7", "sofa_5", "sofa_3"],
                                  ["sofa_11", "sofa_9", "sofa_4", "sofa_2"],
                                  ["sofa_10", "sofa_8", "sofa_6", "sofa_1"]]
    # Five planters are fewer than two per variant: each variant gets its own ranked list.
    assert ids(pools, "planter") == [["plant_4", "plant_3", "plant_2"]] * 3
    assert not any(record["shared"] for pool in pools for rows in pool.values() for record in rows)
    assert [note["text"] for note in run.notes] == ["slot planter shared across variants: only 5 eligible products"]
    # One request per slot and direction, each recorded under its direction's variant.
    assert sorted(call["variant_index"] for call in run.jev_calls) == [0, 0, 1, 1, 2, 2]
    assert {state["variant_direction"] for state, _ in jev.requests} == {
        variant_stages.direction(index, "dining_room").strip() or "none" for index in range(3)}


def test_rank_keeps_a_product_found_by_two_slots_with_one_variant():
    scores = {"floor 0": 0.9, "shared lamp": 0.8, "table 0": 0.1}
    floor = [candidate(f"floor_{n}", "floor_lamp") for n in range(6)] + [candidate("shared_lamp", "floor_lamp")]
    table = [candidate("shared_lamp", "table_lamp")] + [candidate(f"table_{n}", "table_lamp") for n in range(6)]
    jev = FakeJevClient(lambda text: scores.get(text.split("Product: ")[1].split(" (")[0], 0.5))

    pools, _ = rank_pools([("floor_lamp", "optional", floor), ("table_lamp", "optional", table)], jev)

    # Variant 1 takes the lamp in the first slot; in the second, variant 0 picks first but skips it.
    holders = [index for index, pool in enumerate(pools) for rows in pool.values() for r in rows if r["asset_id"] == "shared_lamp"]
    assert holders == [1, 1]
    assert ids(pools, "table_lamp")[1][0] == "shared_lamp"


def test_rank_falls_back_to_embedding_order_for_a_failed_direction():
    class WarmDirectionDown(FakeJevClient):
        async def system_one(self, *, state, questions, model):
            if "warm" in state["variant_direction"]:
                raise RuntimeError("timeout")
            return await super().system_one(state=state, questions=questions, model=model)

    pools, run = rank_pools([("sofa", "requested", SOFAS)], WarmDirectionDown(by_number))

    # Variant 1 deals from the shared order with KEEP; variants 0 and 2 from Jev's order with RANKED_KEEP.
    assert ids(pools, "sofa") == [["sofa_0", "sofa_9", "sofa_7", "sofa_6"],
                                  ["sofa_1", "sofa_2", "sofa_3", "sofa_4"],
                                  ["sofa_11", "sofa_10", "sofa_8", "sofa_5"]]
    assert [(note["variant_index"], note["text"]) for note in run.notes] == [
        (1, "jev rank failed, using the Jev-off behavior: RuntimeError: timeout")]


def test_rank_reuse_rate_bounds_how_much_variants_share():
    slots = [("sofa", "requested", SOFAS)]
    keep = shared_stages.RANKED_KEEP["requested"]

    half, _ = rank_pools(slots, FakeJevClient(by_number), rate=0.5)
    for index, pool in enumerate(half):
        others = {r["asset_id"] for other in half[:index] + half[index + 1:] for r in other["sofa"]}
        assert len(pool["sofa"]) == keep
        assert all(record["shared"] == (record["asset_id"] in others) for record in pool["sofa"])
        assert sum(not record["shared"] for record in pool["sofa"]) >= math.ceil(keep / 2)

    # With no limit, each variant keeps its direction's top list, as when variants ranked alone.
    unlimited, _ = rank_pools(slots, FakeJevClient(by_number), rate=1.0)
    assert ids(unlimited, "sofa") == [["sofa_0", "sofa_11", "sofa_10", "sofa_9", "sofa_8", "sofa_7"]] * 3
    assert all(record["shared"] for pool in unlimited for record in pool["sofa"])

    # With rank off, the deal uses the shared order and KEEP.
    off_jev = FakeJevClient()
    off, run = rank_pools(slots, off_jev, uses=frozenset())
    assert ids(off, "sofa") == [["sofa_0", "sofa_5", "sofa_7", "sofa_9"], ["sofa_1", "sofa_3", "sofa_8", "sofa_10"],
                                ["sofa_2", "sofa_4", "sofa_6", "sofa_11"]]
    assert not off_jev.requests and not run.jev_calls


@pytest.mark.parametrize("value", ["1.5", "-0.1", "half"])
def test_invalid_product_reuse_rate_is_rejected(monkeypatch, value):
    monkeypatch.setenv("PRODUCT_REUSE_RATE", value)
    with pytest.raises(ValueError, match="PRODUCT_REUSE_RATE"):
        switches()


def cloned_searches(monkeypatch, copies: int) -> None:
    """Fixed search results with `copies` clones (<uid>_c<n>) of each selected product, so every slot is dealt."""
    selected = dict(TURN["items"])
    rows = [{**record, "asset_id": f"{record['asset_id']}_c{n}"} for record in POOL if record["asset_id"] in selected
            for n in range(copies)]
    monkeypatch.setattr(shared_stages, "search_assets", lambda connection, vector, **filters: [
        row for row in rows if row["category"] in filters["categories"]])
    monkeypatch.setattr(shared_stages, "embed_query", lambda client, text: [0.0])


def cloned_selection(shared_first: bool):
    """Select TURN's items from the clones in the prompt: the first one not marked shared, or on the
    first turn with `shared_first`, the first one marked shared."""
    def answer(prompt: str) -> dict:
        rows = [line.split(",") for line in prompt.splitlines() if "_c" in line.split(",")[0]]
        shared = shared_first and "FAILED VALIDATION" not in prompt
        return selection([(next(cells[0] for cells in rows if cells[0].startswith(f"{uid}_c") and (cells[-1] == "yes") == shared),
                           count) for uid, count in TURN["items"]])
    return answer


def selected_ids(events: list[dict]) -> dict[int, set[str]]:
    return {event["data"]["variant"]["variant_index"]: {a["asset_id"] for a in event["data"]["variant"]["selected_assets"]}
            for event in of_type(events, "variant_ready")}


def test_variants_select_different_products_within_the_reuse_rate(tmp_path, monkeypatch):
    cloned_searches(monkeypatch, 12)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": cloned_selection(False), "Correction": poses(layout())})

    events, _ = run_pipeline(tmp_path, genai)

    chosen = selected_ids(events)
    assert sorted(chosen) == [0, 1, 2]
    for index, assets in chosen.items():
        others = set().union(*(chosen[other] for other in chosen if other != index))
        assert len(assets & others) <= len(assets) // 2
    assert all("At most 50% of your distinct products may be shared ones." in prompt for prompt in genai.of("Selection"))


def test_selection_over_the_reuse_rate_is_rejected_and_retried(tmp_path, monkeypatch):
    cloned_searches(monkeypatch, 12)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": cloned_selection(True), "Correction": poses(layout())})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    for index in range(3):
        assert stage_runs(record, "select", index) == 2
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3
    assert all("REUSE LIMIT: 4 of 4 products are shared with other variants; at most 2 may be." in p for p in retries)


# --- retrieve ---------------------------------------------------------------


def product(asset_id: str, category: str, width: float, depth: float, **fields) -> dict:
    return {"asset_id": asset_id, "source_table": "catalog.assets", "title": asset_id, "category": category,
            "brand": None, "description": "", "width_m": width, "depth_m": depth, "height_m": 0.8,
            "placement_type": "floor", "is_purchasable": True, "price": 500, "currency": "USD", **fields}


def item(label: str, category: str, **fields) -> dict:
    return {"label": label, "canonical_category": category, "count": 1, "exact": False, "optional": False,
            "acceptable_substitutes": [], "descriptors": [], **fields}


def retrieve(monkeypatch, room_type: str, packet: dict, results, room_area=(3.5, 4.0)):
    """Run the retrieve stage on fixed results: `results(filters)` returns the rows for one search."""
    request = PipelineRequest(
        user_intent=packet["normalized_prompt"], budget=4000, room_type=room_type, room_area=room_area,
        room_vertices=[(0, 0), (room_area[0], 0), room_area, (0, room_area[1])], wall_height=2.7,
        room_doors=[{"center": [room_area[0] / 2, 0.0], "width": 0.9, "depth": 0.05}],
    )
    intent = coerce_intent_packet(packet, room_type=room_type)
    room = build_room_context(room_type=room_type, room_area=room_area, room_vertices=None, wall_height=2.7,
                              room_doors=request.room_doors, room_windows=[], intent=intent)
    texts, calls = [], []

    def search(connection, vector, **filters):
        calls.append(filters)
        return results(filters)

    monkeypatch.setattr(shared_stages, "search_assets", search)
    monkeypatch.setattr(shared_stages, "embed_query", lambda client, text: texts.append(text) or [0.0])
    run = RunContext("run", request, SimpleNamespace(client=None), 3)
    update = asyncio.run(shared_stages.retrieve({"request": request, "intent": intent, "room": room}, StageContext(run, "retrieve", None)))
    slots = {slot["id"]: slot for slot in update["slots"]}
    pool = {slot_id: [row["asset_id"] for row in rows] for slot_id, rows in update["pool"].items()}
    return slots, pool, run, texts, calls


def test_retrieve_applies_the_checks_after_each_slot_search(monkeypatch):
    packet = {
        "normalized_prompt": "a cozy sofa at least 2 m wide, ideally from Acme, and a rug up to 2 x 3 m",
        "style_hints": ["cozy"],
        "requested_items": [
            item("cream sofa", "sofa", descriptors=["cream", "low"], min_width_m=2.0, max_depth_m=1.0, colors=["cream"]),
            item("wool rug", "rug", max_width_m=2.0, max_depth_m=3.0),
        ],
        "asset_attribute_constraints": [
            {"category": "sofa", "attribute_type": "brand", "value": "Acme", "required": False, "source_label": "Acme"},
        ],
    }
    rows = [
        product("sofa_too_long", "sofa", 4.2, 0.9),  # fits the 3.5 x 4.0 m floor in neither orientation
        product("sofa_too_narrow", "sofa", 1.6, 0.9),  # below the 2 m minimum width
        product("sofa_other", "sofa", 2.2, 0.9, brand="Other"),
        product("sofa_acme", "sofa", 2.1, 0.9, brand="Acme"),
        product("rug_turned", "area_rug", 3.0, 2.0),  # fits 2 x 3 m when turned
        product("rug_too_big", "area_rug", 3.0, 3.0),
        product("plant_retail", "planter", 0.5, 0.5),
        product("plant_design", "planter", 0.5, 0.5, source_table="pipeline.decor_items", is_purchasable=False, price=None),
    ]

    slots, pool, run, texts, calls = retrieve(
        monkeypatch, "living_room", packet,
        lambda filters: [row for row in rows if row["category"] in filters["categories"]
                         and (not filters["design_only"] or row["is_purchasable"] is False)],
    )

    assert pool["sofa"] == ["sofa_acme", "sofa_other"]
    assert pool["rug"] == ["rug_turned"]
    assert pool["decor_plant"] == ["plant_design"]
    # Slots search concurrently, so call order varies: one design-only search (required plant), one not (decor planter).
    assert sorted(f["design_only"] for f in calls if "planter" in f["categories"]) == [False, True]
    assert all(f["per_category"] for f in calls) and len(texts) == len(slots)
    assert slots["sofa"]["required"] and slots["sofa"]["text"] == "cream sofa, low, cozy"
    assert "cream sofa, low, cozy" in texts
    sofa_filters, rug_filters = (next(f for f in calls if c in f["categories"]) for c in ("sofa", "area_rug"))
    assert sofa_filters["max_depth_m"] == 1.0 and sofa_filters["colors"] == ["cream"] and sofa_filters["known_price"]
    assert "max_width_m" not in rug_filters and "max_depth_m" not in rug_filters
    # TVs never count toward the budget and have no prepared price, so their slot has no price filters.
    [tv_filters] = [f for f in calls if f["categories"] == ["television", "tv"]]
    assert "known_price" not in tv_filters and "max_price" not in tv_filters
    assert all(f["max_price"] <= 4400 and f["known_price"] for f in calls if f is not tv_filters)
    assert not any(slot["gap"] for slot in slots.values())
    assert {entry["slot"]: entry["candidates"] for entry in run.slots}["sofa"] == 2


def test_retrieve_merges_categories_round_robin_and_keeps_up_to_fetch(monkeypatch):
    packet = {"normalized_prompt": "a sofa", "requested_items": [item("sofa", "sofa")]}
    # Distance order: every floor lamp is nearer than the two table lamps.
    lamps = [product(f"floor_{n}", "floor_lamp", 0.4, 0.4) for n in range(40)]
    lamps += [product(f"table_{n}", "table_lamp", 0.3, 0.3) for n in range(2)]

    slots, pool, run, _, _ = retrieve(
        monkeypatch, "living_room", packet, lambda filters: [row for row in lamps if row["category"] in filters["categories"]]
    )

    assert slots["lighting"]["kind"] == "optional"
    assert pool["lighting"][:5] == ["floor_0", "table_0", "floor_1", "table_1", "floor_2"]
    assert len(pool["lighting"]) == shared_stages.FETCH > shared_stages.KEEP["optional"]
    assert {entry["slot"]: entry["candidates"] for entry in run.slots}["lighting"] == shared_stages.FETCH


def test_empty_requested_slot_is_a_gap_and_required_slot_drops_request_limits(monkeypatch):
    packet = {
        "normalized_prompt": "a green bed and a floor mirror",
        "requested_items": [item("green bed", "bed", colors=["green"]), item("floor mirror", "floor_mirror")],
    }
    bed = product("bed_1", "bed", 1.7, 2.1)

    def results(filters):
        if "bed" in filters["categories"]:
            return [] if "colors" in filters else [bed]
        return []

    slots, pool, run, _, calls = retrieve(monkeypatch, "bedroom", packet, results, room_area=(4.0, 4.5))

    assert pool["bed"] == ["bed_1"] and not slots["bed"]["gap"] and slots["bed"]["limits"] == {}
    assert [f.get("colors") for f in calls if "bed" in f["categories"]] == [["green"], None]
    assert slots["floor_mirror"]["gap"] and pool["floor_mirror"] == []
    records = {entry["slot"]: entry for entry in run.slots}
    assert records["floor_mirror"]["gap"] and "gap" in records["floor_mirror"]["note"]
    assert "without them" in records["bed"]["note"] and not records["bed"]["gap"]
    notes = [note["text"] for note in run.notes]
    assert records["bed"]["note"] in notes and records["floor_mirror"]["note"] in notes
