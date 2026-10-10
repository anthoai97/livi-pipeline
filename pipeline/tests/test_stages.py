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
from test_rules_parity import RUNS, _room
from test_runtime import FakeJevClient, of_type, parse

from app import shared_stages, variant_stages
from app.contracts import PipelineRequest
from app.graph import MAX_SELECTION_TURNS
from app.jev import Jev, switches
from app.llm import GeminiModel
from app.main import create_app
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.selection import preflight
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


REVIEW = "the plant balances the sideboard"


def arrangement(choice: str = "A", *moves: tuple) -> dict:
    """A final review answer that moves each (uid, x, y) of `moves`, turned to a fourth value when given, else unturned."""
    return {"choice": choice, "review": REVIEW,
            "adjustments": [{"uid": uid, "x": x, "y": y, "rotation_z": turn[0] if turn else PLACEMENTS[uid]["rotation"][2]}
                            for uid, x, y, *turn in moves]}


# A chair pushed into the table overlaps it (P0); the final review moves it back.
OVERLAPPING = {**PLACEMENTS, "dining_chair_1": {**PLACEMENTS["dining_chair_1"], "position": TABLE_CENTER}}
FIXED = arrangement("A", ("dining_chair_1", *PLACEMENTS["dining_chair_1"]["position"][:2]))


def solve_as(monkeypatch, placements: dict, *alternatives: dict) -> None:
    """Make the place stage's solver return `placements`, and `alternatives` as its other layouts, for the instances it is given."""

    def solve(instances, room, intent):
        keys = {variant_stages._key(asset) for asset in instances}

        def only(layout: dict) -> dict:
            return {key: placement for key, placement in layout.items() if key in keys}

        return only(placements), {"unplaceable": [], "score": [0, 0, 0], "candidates": 0, "scored": 0, "elapsed": 0.0,
                                  "alternatives": [{"layout": only(layout), "score": [0, 0, 0], "unplaceable": []}
                                                   for layout in alternatives]}

    monkeypatch.setattr(variant_stages, "solve_layout", solve)


# What a call that used its attempts returns, as in a benchmark run.
DEADLINE = RuntimeError("504 DEADLINE_EXCEEDED")


class FakeGenai:
    """genai.Client stand-in that answers each schema with `responses[schema title]`.

    A response is a dict, a function of the prompt that returns one, or an exception that fails the call.
    A prompt with image parts is recorded as its text parts; `images` counts the image parts of each call by title.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.prompts: list[tuple[str, str]] = []
        self.images: list[tuple[str, int]] = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model: str, contents: str | list[str | types.Part], config: types.GenerateContentConfig):
        title = config.response_json_schema["title"]
        parts = [contents] if isinstance(contents, str) else contents
        text = "\n".join(part for part in parts if isinstance(part, str))
        self.prompts.append((title, text))
        self.images.append((title, sum(isinstance(part, types.Part) and part.inline_data is not None for part in parts)))
        answer = self.responses[title]
        if isinstance(answer, Exception):
            raise answer
        answer = answer(text) if callable(answer) else answer
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=json.dumps(answer))]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10),
            model_version=model,
        )

    def of(self, title: str) -> list[str]:
        return [prompt for name, prompt in self.prompts if name == title]


@pytest.fixture(autouse=True)
def default_switches(monkeypatch):
    for switch in ("JEV_USES", "PRODUCT_REUSE_RATE", "LLM_STAGE_MODELS"):
        monkeypatch.delenv(switch, raising=False)


def search_pool(monkeypatch, rows: list[dict]) -> None:
    """Fixed search results: the `rows` in the slot's categories with its strict attributes."""

    def search(connection, vector, **filters):
        return [
            row for row in rows
            if row["category"] in filters["categories"]
            and all(value in row[field] for field in ("colors", "styles", "materials") for value in filters.get(field, []))
        ]

    monkeypatch.setattr(shared_stages, "search_assets", search)
    monkeypatch.setattr(shared_stages, "embed_query", lambda client, text: [0.0])


@pytest.fixture
def searches(monkeypatch):
    search_pool(monkeypatch, POOL)


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
    solve_as(monkeypatch, OVERLAPPING)
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    monkeypatch.setitem(sideboard, "mount_type", "wall_secured")
    monkeypatch.setitem(sideboard, "features", ["soft close drawers"])
    # The ottoman has no catalog product, so it is a gap and the run continues without it.
    ottoman = {"label": "velvet ottoman", "canonical_category": "ottoman", "count": 1, "exact": False,
               "optional": False, "acceptable_substitutes": [], "descriptors": ["velvet"]}
    genai = FakeGenai({"IntentPacket": intent_packet(ottoman), "Selection": selection(), "Arrangement": FIXED})

    events, record = run_pipeline(tmp_path, genai)

    for stage in ("interpret", "room", "retrieve", "rank"):
        assert stage_runs(record, stage, None) == 1
    for index in range(3):
        for stage in ("select", "place", "finish"):
            assert stage_runs(record, stage, index) == 1
    calls = {}
    for call in record["model_calls"]:
        calls[call["stage"]] = calls.get(call["stage"], 0) + 1
    assert calls == {"interpret": 1, "select": 3, "finish": 3}
    # Jev: one rank request per non-empty slot and direction, and one selection check per variant.
    uses = {(call["use"], call["variant_index"]) for call in record["jev_calls"]}
    assert uses == {(use, index) for use in ("rank", "check") for index in range(3)}
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        # The final review moved the chair out of the table.
        [review] = [note for note in notes if note.startswith(f"final review: A of 1: {REVIEW}; adjusted dining_chair_1: kept, score [")]
        assert "-> [0, " in review

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert sorted(variant["variant_index"] for variant in ready) == [0, 1, 2]
    for variant in ready:
        assert variant["total_cost"] == 899 + 4 * 510 + 899
        assert sorted(variant["render_manifest"]["layout"]) == sorted(PLACEMENTS)
        assert len(variant["selected_assets"]) == 7
        assert variant["render_manifest"]["assets"]["sideboard_1"]["mount_type"] == "wall_secured"
        assert variant["render_manifest"]["assets"]["sideboard_1"]["features"] == ["soft close drawers"]
    assert [(v["outcome"], v["reason"]) for v in record["variants"]] == [("ready", None)] * 3
    # The record keeps each ready variant's compact result for benchmark review, without URLs.
    assert record["request"]["room_doors"] == REQUEST["room_doors"]
    assert record["request"]["room_vertices"] == REQUEST["room_vertices"]
    manifests = {variant["variant_index"]: variant["render_manifest"] for variant in ready}
    for variant in record["variants"]:
        assert variant["direction"] == variant_stages.direction(variant["variant_index"], "dining_room").strip()
        assert variant["total_cost"] == 899 + 4 * 510 + 899
        assert {product["instance_key"] for product in variant["products"]} == set(PLACEMENTS)
        for key, pose in variant["layout"].items():
            assert pose["position"] == manifests[variant["variant_index"]]["layout"][key]["position"]
            assert pose["rotation"] == manifests[variant["variant_index"]]["layout"][key]["rotation"]
        sideboard_product = next(p for p in variant["products"] if p["instance_key"] == "sideboard_1")
        assert sideboard_product["mount_type"] == "wall_secured" and sideboard_product["price"] == 899
        assert sideboard_product["width_m"] > 0 and sideboard_product["placement_mode"] == "floor"
    assert "example.com" not in json.dumps(record["variants"])

    [gap] =[slot for slot in record["slots"] if slot["slot"] == "ottoman"]
    assert gap["gap"] is True and gap["candidates"] == 0 and "gap" in gap["note"]
    assert all("CATALOG GAPS" in prompt and "velvet ottoman" in prompt for prompt in genai.of("Selection"))
    for prompt in genai.of("Selection"):
        assert "wall_secured" in prompt and "soft close drawers" in prompt
    assert all("sideboard_1 (sideboard)" in prompt and "mount_type=wall_secured" in prompt for prompt in genai.of("Arrangement"))
    # Variant directions: none for variant 0, the legacy dining texts for 1 and 2.
    directions = ["warm, rounded dining table", "compact, structured dining table"]
    assert [sum(text in prompt for prompt in genai.of("Selection")) for text in directions] == [1, 1]


def test_oversized_selection_steps_to_compact_then_capped_counts_then_drops_the_product(tmp_path, searches):
    # Ten sideboards overcrowd the room on every turn.
    oversized = [(uid, 10 if uid == "sideboard_1" else count) for uid, count in TURN["items"]]
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(oversized)})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all(len(variant["selected_assets"]) == 6 for variant in ready)
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
        assert [note for note in notes if "fit step" in note] == ["selection turn 2: fit step compact", "selection turn 3: fit step capped"]
        turns = [note for note in notes if note.startswith("selection turn") and "fit step" not in note]
        assert [note.split(":")[0] for note in turns[:-1]] == [f"selection turn {turn} failed" for turn in range(1, MAX_SELECTION_TURNS)]
        # The last turn drops the product that fails it instead of failing the variant.
        assert turns[-1] == f"selection turn {MAX_SELECTION_TURNS}: dropped sideboard_1; passes"
        assert stage_runs(record, "select", index) == MAX_SELECTION_TURNS


def fail_fit_estimate(monkeypatch, estimate: str) -> None:
    """Make one fit estimate fail every selection of the dining room; no other rule fails."""
    if estimate == "footprint":
        build = shared_stages.build_room_context
        monkeypatch.setattr(shared_stages, "build_room_context", lambda **kwargs: {**build(**kwargs), "furniture_area_sqm": 1.0})
    else:  # the dining cluster does not fit a 1 x 1 m room clear area
        dimensions = preflight._room_preflight_dimensions
        monkeypatch.setattr(preflight, "_room_preflight_dimensions", lambda room: (*dimensions(room)[:2], 1.0, 1.0))


@pytest.mark.parametrize(("estimate", "overruled"), [
    ("footprint", "overruled OVER CROWDED: Footprint is"),
    ("cluster", "overruled LAYOUT PREFLIGHT: dining table/chair cluster needs about"),
])
def test_selection_failing_only_a_fit_estimate_passes_when_the_solver_places_it(tmp_path, searches, monkeypatch, estimate, overruled):
    fail_fit_estimate(monkeypatch, estimate)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    for index in range(3):
        assert stage_runs(record, "select", index) == 1
        [note] = [note["text"] for note in record["notes"] if note["variant_index"] == index and note["text"].startswith("fit estimate")]
        assert note.startswith("fit estimate overruled by the solver: dining_chair 4, dining_table 1, planter 1, sideboard 1; " + overruled)


def test_selection_failing_a_fit_estimate_the_solver_cannot_place_steps_down(tmp_path, searches, monkeypatch):
    fail_fit_estimate(monkeypatch, "footprint")
    solve = variant_stages.solve_layout
    monkeypatch.setattr(variant_stages, "solve_layout", lambda instances, room, intent: (
        solve(instances, room, intent)[0], {"unplaceable": ["sideboard_1"], "score": [1, 0, 0], "candidates": 0, "scored": 0, "elapsed": 0.0, "alternatives": []}))
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    # Dropping a product cannot clear the footprint estimate, so the last selection is placed with it.
    assert len(of_type(events, "variant_ready")) == 3
    for index in range(3):
        assert stage_runs(record, "select", index) == MAX_SELECTION_TURNS
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert not [note for note in notes if note.startswith("fit estimate")]
        assert [note for note in notes if "fit step" in note] == ["selection turn 2: fit step compact", "selection turn 3: fit step capped"]
        # Every failed turn is noted with its errors.
        failures = [note for note in notes if " failed: " in note]
        assert [note.split(":")[0] for note in failures] == [f"selection turn {turn} failed" for turn in range(1, MAX_SELECTION_TURNS + 1)]
        assert all("OVER CROWDED: Footprint is" in note and len(note) <= 640 for note in failures)


def test_lamp_too_large_for_its_side_table_keeps_the_fit_step(tmp_path, monkeypatch):
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    lamp = {**sideboard, "asset_id": "table_lamp_1", "category": "table_lamp", "width_m": 0.6, "depth_m": 0.6,
            "placement_type": "tabletop", "price": 100}
    side_table = {**sideboard, "asset_id": "side_table_1", "category": "side_table", "width_m": 0.5, "depth_m": 0.5, "price": 150}
    search_pool(monkeypatch, [*POOL, lamp, side_table])
    # The solver cannot place the lamp either, so every turn fails on the lamp alone.
    solve = variant_stages.solve_layout
    monkeypatch.setattr(variant_stages, "solve_layout", lambda instances, room, intent: (
        solve(instances, room, intent)[0], {"unplaceable": ["table_lamp_1"], "score": [1, 0, 0], "candidates": 0, "scored": 0, "elapsed": 0.0, "alternatives": []}))
    packet = intent_packet(item("table lamp", "table_lamp"), item("side table", "side_table"))
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection([*TURN["items"], ("table_lamp_1", 1), ("side_table_1", 1)])})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and not [asset for variant in ready for asset in variant["selected_assets"] if asset["category"] == "table_lamp"]
    # The room fit estimate starts at compact; the lamp failures never step down to capped counts.
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert [note for note in notes if "fit step" in note] == ["selection turn 1: fit step compact"]
        assert f"selection turn {MAX_SELECTION_TURNS}: dropped table_lamp_1; passes" in notes
        assert stage_runs(record, "select", index) == MAX_SELECTION_TURNS
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3 * (MAX_SELECTION_TURNS - 1)
    for prompt in retries:
        assert "tabletop asset table_lamp_1 (0.60m x 0.60m) does not fit" in prompt
        assert "select a smaller tabletop asset or a wider/deeper support from the same slots" in prompt
        assert "=== ACCEPTED FIT DECISION ===" not in prompt


def over_budget_chairs(monkeypatch, **cheaper: object) -> None:
    """Chairs at $1,200 put TURN at $6,598, over the $5,500 allowance; the chair slot also holds a $300 chair with `cheaper` fields."""
    chair = next(record for record in POOL if record["asset_id"] == "dining_chair_39")
    monkeypatch.setitem(chair, "price", 1200)
    search_pool(monkeypatch, [*POOL, {**chair, "asset_id": "dining_chair_8", "price": 300, **cheaper}])


def test_over_budget_selection_is_repaired_with_cheaper_products_from_the_same_slot(tmp_path, monkeypatch):
    over_budget_chairs(monkeypatch)
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        # The requested four chairs stay; the anchor table and the sideboard are unchanged.
        assert [asset["asset_id"] for asset in variant["selected_assets"] if asset["category"] == "dining_chair"] == ["dining_chair_8"] * 4
        assert variant["total_cost"] == 899 + 4 * 300 + 899
    for index in range(3):
        assert stage_runs(record, "select", index) == 1
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert "budget repair: $6598.00 -> $2998.00 (dining_chair_39 -> dining_chair_8)" in notes
        assert not [note for note in notes if " failed: " in note]


def test_over_budget_selection_the_repair_cannot_validate_is_retried_keeping_requested_items(tmp_path, monkeypatch):
    # The cheaper chair is not a usable single chair, so the repaired selection fails validation.
    over_budget_chairs(monkeypatch, width_m=2.0)
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    # The last turn drops the sideboard to cut the cost; the requested chairs stay, so the design is delivered over budget.
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all(variant["total_cost"] == 899 + 4 * 1200 for variant in ready)
    dropped = [note["text"] for note in record["notes"] if " dropped " in note["text"]]
    assert dropped == [f"selection turn {MAX_SELECTION_TURNS}: dropped sideboard_1; still fails: OVER BUDGET: "
                       "Selection costs $5699.00 (Flex limit: $5500.00)."] * 3
    rejected = [note["text"] for note in record["notes"] if note["text"].startswith("budget repair")]
    assert len(rejected) == 3 * MAX_SELECTION_TURNS
    assert all(note.startswith("budget repair rejected (dining_chair_39 -> dining_chair_8): ") and "DINING CHAIR DIMENSIONS" in note
               for note in rejected)
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3 * (MAX_SELECTION_TURNS - 1)
    assert all("OVER BUDGET: Selection costs $6598.00" in prompt and "Keep every requested item and its count" in prompt
               and "Do not drop requested items to save money." in prompt for prompt in retries)


def test_required_item_searched_without_its_limits_can_pass_selection(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS)
    # No catalog dining table is tagged scandinavian, so the required table slot drops
    # that limit, and selection no longer requires it.
    table, chairs = RUN["intent"]["requested_items"]
    packet = {**intent_packet(), "requested_items": [
        {**table, "colors": [], "styles": ["scandinavian"], "materials": []},
        {**chairs, "colors": [], "styles": [], "materials": []},
    ]}
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    [slot] = [slot for slot in record["slots"] if slot["slot"] == "dining_table"]
    assert slot["candidates"] == 2 and "without them" in slot["note"]
    assert all("candidates ignore them" in prompt for prompt in genai.of("Selection"))


def test_layout_that_still_fails_the_final_check_loses_the_failing_item_and_is_delivered(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, OVERLAPPING)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    # The review moves nothing, so the chair stays inside the table; it is removed, not the anchor table.
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        assert sorted(variant["render_manifest"]["layout"]) == sorted(set(PLACEMENTS) - {"dining_chair_1"})
        assert variant["total_cost"] == 899 + 3 * 510 + 899
    checks = [event["data"] for event in of_type(events, "node_complete") if event["node"] == "render_scene"]
    assert [(check["valid"], check["dropped"]) for check in checks] == [(True, 1)] * 3
    for index in range(3):
        assert [stage_runs(record, stage, index) for stage in ("select", "place", "finish")] == [1, 1, 1]
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert notes[-1] == "dropped dining_chair_1 for the final check"
    assert events[-1]["type"] == "complete" and len(events[-1]["data"]["variants"]) == 3


def test_jev_style_and_attribute_failures_reject_the_selection(tmp_path, searches, monkeypatch):
    def answer(text: str) -> float:
        if "style and palette" in text and "sideboard_1" in text:
            return 0.1
        return 0.2 if 'requirement "soft neutral"' in text else 0.9

    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai, FakeJevClient(answer))

    # The last turn drops the mismatched sideboard; the requested chairs stay, so their attribute failure remains.
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all("sideboard_1" not in variant["render_manifest"]["layout"] for variant in ready)
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        failures = [note for note in notes if note.startswith("selection turn") and " failed: " in note]
        assert all("SELECTION CONSTRAINT AUDIT: sideboard_1 does not match the style and palette of the anchor dining_table_3" in note
                   for note in failures[:-1])
        [dropped] = [note for note in notes if " dropped " in note]
        assert dropped.startswith(f"selection turn {MAX_SELECTION_TURNS}: dropped sideboard_1; still fails: SELECTION CONSTRAINT AUDIT: "
                                  "attribute constraint \"soft neutral\" is not satisfied by ['dining_chair_39']")
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
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai, FakeJevClient(fail=RuntimeError("jev down")))

    assert len(of_type(events, "variant_ready")) == 3
    assert record["jev_calls"] and all("jev down" in call["error"] for call in record["jev_calls"])
    for index in range(3):
        notes = {note["text"].split(",")[0] for note in record["notes"] if note["variant_index"] == index}
        assert {"jev rank failed", "jev check failed"} <= notes


# --- solver placement -------------------------------------------------------

LARGEST_PLANT = "decor_047ff49d-65d8-4f1e-bd46-256e38ee5da4"  # 1.03 x 1.11 m; the decor plant slot also holds 1.02 x 1.07 and 0.97 x 1.09


def test_solver_places_the_selection_and_the_final_review_picks_among_its_ties(tmp_path, searches):
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    # The solver's beam ends with tied layouts; one review call per variant chooses among them.
    assert {call["stage"] for call in record["model_calls"]} == {"interpret", "select", "finish"}
    assert len([title for title, count in genai.images if title == "Arrangement" and count > 1]) == 3
    for index in range(3):
        assert [stage_runs(record, stage, index) for stage in ("place", "finish")] == [1, 1]
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert any(note.startswith("solver: score [0, 0, 0],") for note in notes)
        assert any(note.startswith("final review: A of ") for note in notes)


def test_solver_swaps_an_unplaceable_item_for_the_next_smaller_product_in_its_slot(tmp_path, searches, monkeypatch):
    solve = variant_stages.solve_layout

    def no_room_for_the_largest_plant(instances, room, intent):
        layout, report = solve(instances, room, intent)
        if next(a["asset_id"] for a in instances if a["uid"] == "planter_1") != LARGEST_PLANT:
            return layout, report
        table = layout["dining_table_1"]["position"]
        return {**layout, "planter_1": {**layout["planter_1"], "position": [table[0], table[1], 0.0]}}, {
            **report, "unplaceable": ["planter_1"]}

    monkeypatch.setattr(variant_stages, "solve_layout", no_room_for_the_largest_plant)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        plants = [asset["asset_id"] for asset in variant["selected_assets"] if asset["category"] == "planter"]
        assert plants == ["decor_130b1ed4-b579-481b-a8ee-aaeee5c6e6ef"]
        assert variant["selection_validation"]["valid"]
    swaps = [note["text"] for note in record["notes"] if note["text"].startswith("swap ")]
    assert len(swaps) == 3
    assert all(note.startswith(f"swap {LARGEST_PLANT} -> decor_130b1ed4-b579-481b-a8ee-aaeee5c6e6ef in slot ")
               and ": kept, score [1," in note for note in swaps)


def test_final_review_fixes_blocking_findings_the_solver_left(tmp_path, searches, monkeypatch):
    solve = variant_stages.solve_layout
    solved: dict = {}

    def chair_on_the_plant(instances, room, intent):
        layout, report = solve(instances, room, intent)
        solved.setdefault("layout", layout)  # the selected table's layout, not the reverted swap's
        return {**layout, "dining_chair_1": {**layout["dining_chair_1"], "position": list(layout["planter_1"]["position"])}}, report

    def restore(prompt: str) -> dict:
        pose = solved["layout"]["dining_chair_1"]
        return {"choice": "A", "review": "a chair stands on the plant",
                "adjustments": [{"uid": "dining_chair_1", "x": pose["position"][0], "y": pose["position"][1],
                                 "rotation_z": pose["rotation"][2]}]}

    monkeypatch.setattr(variant_stages, "solve_layout", chair_on_the_plant)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": restore})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all(len(variant["selected_assets"]) == 7 for variant in ready)
    assert delivered(events, "dining_chair_1") == [solved["layout"]["dining_chair_1"]["position"][:2]] * 3
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        # A smaller table does not free the chair, so the swap is reverted.
        assert any(note.startswith("swap dining_table_3 -> dining_table_552 in slot dining_table: reverted,") for note in notes)
        assert any(note.startswith("final review: A of 1: a chair stands on the plant; adjusted dining_chair_1: kept,")
                   for note in notes)


# The plant in the other front corner: the checks score it the same as PLACEMENTS.
CORNER = {**PLACEMENTS, "planter_1": {**PLACEMENTS["planter_1"], "position": [3.6, 0.6, 0.0]}}


def delivered(events: list[dict], key: str) -> list[list[float]]:
    return [event["data"]["variant"]["render_manifest"]["layout"][key]["position"][:2] for event in of_type(events, "variant_ready")]


def test_final_review_picks_among_the_solver_layouts_that_tie_the_best(tmp_path, searches, monkeypatch):
    # OVERLAPPING scores worse than PLACEMENTS, so only PLACEMENTS (A) and CORNER (B) are offered.
    solve_as(monkeypatch, PLACEMENTS, OVERLAPPING, CORNER)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement("B")})

    events, record = run_pipeline(tmp_path, genai)

    assert delivered(events, "planter_1") == [[3.6, 0.6]] * 3
    assert [count for title, count in genai.images if title == "Arrangement"] == [2, 2, 2]
    for prompt in genai.of("Arrangement"):
        assert "variant A:" in prompt and "variant B:" in prompt and "variant C:" not in prompt
        assert REQUEST["user_intent"] in prompt and "planter_1 (planter)" in prompt and "door_0" in prompt
        assert "REQUESTED ITEMS: light wood dining table for four x1, soft neutral dining chairs x4" in prompt
        assert "STYLE HINTS: scandinavian, cozy, minimalist" in prompt
        assert "P0 — ARCHITECTURE" in prompt and "DINING ROOM APPLICABILITY" in prompt and "COORDINATES:" in prompt
    places = [event["data"] for event in of_type(events, "node_complete") if event["node"] == "layout_initial"]
    assert [data["layout_options"] for data in places] == [2] * 3
    finishes = [event["data"] for event in of_type(events, "node_complete") if event["node"] == "render_scene"]
    assert [(data["layout_pick"], data["review"], data["valid"]) for data in finishes] == [("B", REVIEW, True)] * 3
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert f"final review: B of 2: {REVIEW}" in notes


def test_a_single_layout_still_gets_a_final_review(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS, OVERLAPPING)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    assert [count for title, count in genai.images if title == "Arrangement"] == [1, 1, 1]
    assert all("as variant A" in prompt and "variant B:" not in prompt for prompt in genai.of("Arrangement"))
    assert [event["data"]["layout_options"] for event in of_type(events, "node_complete") if event["node"] == "layout_initial"] == [1] * 3
    assert sum(note["text"] == f"final review: A of 1: {REVIEW}" for note in record["notes"]) == 3


@pytest.mark.parametrize(("moves", "outcome", "planter"), [
    ([("planter_1", 0.7, 0.6)], "adjusted planter_1: kept, score [0, 0, 0] -> [0, 0, 0]", [0.7, 0.6]),  # a tie keeps it
    ([("planter_1", 3.6, 0.6)], "adjusted planter_1: kept, score [0, 0, 0] -> [0, 0, 0]", [3.6, 0.6]),  # a 3 m move is allowed
    ([("dining_chair_1", 2.535, 2.8)], "adjusted dining_chair_1: reverted, score [0, 0, 0] -> [1, ", [0.6, 0.6]),  # into the table
    ([("sofa_9", 1.0, 1.0, 0.0), ("planter_1", 0.7, 0.6, PLACEMENTS["planter_1"]["rotation"][2] + 0.5)],
     "rejected unknown, non-finite, or not a quarter turn: sofa_9, planter_1", [0.6, 0.6]),
])
def test_final_review_adjustments_are_kept_only_when_not_worse(tmp_path, searches, monkeypatch, moves, outcome, planter):
    solve_as(monkeypatch, PLACEMENTS, CORNER)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement("A", *moves)})

    events, record = run_pipeline(tmp_path, genai)

    assert delivered(events, "planter_1") == [planter] * 3
    assert delivered(events, "dining_chair_1") == [PLACEMENTS["dining_chair_1"]["position"][:2]] * 3
    reviews = [note["text"] for note in record["notes"] if note["text"].startswith(f"final review: A of 2: {REVIEW}")]
    assert len(reviews) == 3 and all(outcome in note for note in reviews)


@pytest.mark.parametrize(("answer", "note"), [
    (DEADLINE, "final review failed, kept the solver's best: ModelCallError"),
    (arrangement("E", ("planter_1", 0.7, 0.6)), "final review: unknown choice 'E', kept A of 2"),
])
def test_failed_final_review_delivers_the_solver_layout(tmp_path, searches, monkeypatch, answer, note):
    solve_as(monkeypatch, PLACEMENTS, CORNER)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": answer})

    events, record = run_pipeline(tmp_path, genai)

    assert delivered(events, "planter_1") == [[0.6, 0.6]] * 3
    assert [call["error"] is not None for call in record["model_calls"] if call["stage"] == "finish"] == [answer is DEADLINE] * 3
    assert sum(text["text"].startswith(note) for text in record["notes"]) == 3
    finishes = [event["data"] for event in of_type(events, "node_complete") if event["node"] == "render_scene"]
    assert [(data["layout_pick"], data["valid"]) for data in finishes] == [(None, True)] * 3


# Recorded run 0, variant 1: a sleeper sofa leaves no room for four chairs around the 1.37 m table.
SOFA_AND_DINING = next(turn["items"] for turn in RUNS[0]["selection_turns"] if turn["variant"] == 1)
FOUR_CHAIRS = {"label": "four dining chairs", "canonical_category": "dining_chair", "count": 4, "exact": True,
               "optional": False, "acceptable_substitutes": [], "descriptors": []}


def place_sofa_and_dining(intent_fields: dict, *requested: dict) -> tuple[dict, list[str]]:
    """Run the solver place stage on SOFA_AND_DINING with these intent fields; return its update and notes."""
    intent, room = _room(RUNS[0])
    intent = {**intent, **intent_fields, "requested_items": [*intent["requested_items"], *requested]}
    request = PipelineRequest(user_intent="a dining room with a sleeper sofa",
                              **{key: RUNS[0]["request"][key] for key in ("budget", "room_type", "room_area", "room_vertices",
                                                                          "wall_height", "room_doors", "room_windows")})
    slots, pool = [], {}
    for uid, _ in SOFA_AND_DINING:
        record = RUNS[0]["assets"][uid]
        if record["category"] not in pool:
            slots.append({"id": record["category"], "kind": "requested", "category": record["category"], "label": record["category"],
                          "gap": False, "relaxed": False})
            pool[record["category"]] = []
        pool[record["category"]].append(record)
    state = {"variant_index": 0, "direction": "", "pool": pool, "fit_step": None,
             "shared": {"request": request, "intent": intent, "room": room, "slots": slots}}
    selected = [{"uid": uid, "functional_group": None} for uid, count in SOFA_AND_DINING for _ in range(count)]
    validation = variant_stages._validated(state, selected, intent, None, variant_stages._code_audit(), 1.0)
    assert validation["valid"], validation["errors"]
    state |= {"selection": {"selected_assets": selected, "gaps": ""}, "instances": validation.pop("instances"),
              "selection_validation": validation}
    run = RunContext("run", request, SimpleNamespace(client=None), 3)
    update = asyncio.run(variant_stages.place(state, StageContext(run, "place", 0)))
    return {**state, **update}, [note["text"] for note in run.notes]


def chairs(state: dict) -> int:
    return sum(asset["category"] == "dining_chair" for asset in state["instances"])


def test_solver_drops_a_dining_chair_when_four_do_not_fit():
    # With space-first seating the validator accepts 2 to 4 chairs, so dropping one is allowed.
    state, notes = place_sofa_and_dining({"fit_flexibility": "space_first"})

    assert chairs(state) == 3 and state["blocking_findings"] == []
    assert state["selection_validation"]["valid"]
    assert sum(asset["uid"] == "dining_chair_723" for asset in state["selection"]["selected_assets"]) == 3
    [drop] = [note for note in notes if note.startswith("dropped")]
    assert drop.startswith("dropped dining chair dining_chair_4 (4 -> 3 chairs): kept, score [0, 1, 1] -> [0, 0, 0];")


def test_solver_never_drops_below_an_exact_seat_count():
    state, notes = place_sofa_and_dining({"fit_flexibility": "space_first"}, FOUR_CHAIRS)

    assert chairs(state) == 4 and state["blocking_findings"]
    assert not [note for note in notes if note.startswith("dropped")]


def test_a_dropped_chair_that_does_not_improve_the_layout_is_reverted(monkeypatch):
    solve = variant_stages.solve_layout

    def sofa_outside_with_fewer_chairs(instances, room, intent):
        layout, report = solve(instances, room, intent)
        if sum(asset["category"] == "dining_chair" for asset in instances) < 4:
            layout = {**layout, "sleeper_sofa_1": {**layout["sleeper_sofa_1"], "position": [-2.0, -2.0, 0.0]}}
        return layout, report

    monkeypatch.setattr(variant_stages, "solve_layout", sofa_outside_with_fewer_chairs)

    state, notes = place_sofa_and_dining({"fit_flexibility": "space_first"})

    assert chairs(state) == 4
    assert [note.split(";")[0] for note in notes if note.startswith("dropped")] == [
        "dropped dining chair dining_chair_4 (4 -> 3 chairs): reverted, score [0, 1, 1] -> [1, 0, 1]"]


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
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": cloned_selection(False)})

    events, _ = run_pipeline(tmp_path, genai)

    chosen = selected_ids(events)
    assert sorted(chosen) == [0, 1, 2]
    for index, assets in chosen.items():
        others = set().union(*(chosen[other] for other in chosen if other != index))
        assert len(assets & others) <= len(assets) // 2
    assert all("At most 50% of your distinct products may be shared ones." in prompt for prompt in genai.of("Selection"))


def test_selection_over_the_reuse_rate_is_rejected_and_retried(tmp_path, monkeypatch):
    cloned_searches(monkeypatch, 12)
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": cloned_selection(True)})

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
    # TVs never count toward the budget and have no prepared price, so TV categories are exempt from the price filters.
    [tv_filters] = [f for f in calls if f["categories"] == ["television", "tv"]]
    assert tv_filters["price_exempt"] == ["television", "tv"]
    assert all(f["max_price"] <= 4400 and f["known_price"] for f in calls)
    assert all(f["price_exempt"] == [] for f in calls if f is not tv_filters)
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


def test_requested_tv_with_a_substitute_keeps_unpriced_tvs(monkeypatch):
    packet = {"normalized_prompt": "a sofa and a TV for movie nights",
              "requested_items": [item("sofa", "sofa"), item("TV for movie nights", "tv", acceptable_substitutes=["tv_stand"])]}
    rows = [
        product("tv_unpriced", "tv", 1.2, 0.08, price=None),
        product("stand_priced", "tv_stand", 1.5, 0.4),
        product("stand_unpriced", "tv_stand", 1.5, 0.4, price=None),
        product("stand_too_costly", "tv_stand", 1.5, 0.4, price=9000),
    ]

    def search(filters):
        """Fixed rows that honor the category and price filters, as search_assets applies them."""
        def priced(row):
            if row["category"] in filters.get("price_exempt", []) or not row["is_purchasable"]:
                return True
            return row["price"] is not None and row["price"] <= filters["max_price"]
        return [row for row in rows if row["category"] in filters["categories"] and priced(row)]

    slots, pool, _, _, calls = retrieve(monkeypatch, "living_room", packet, search)

    assert set(slots["tv"]["categories"]) >= {"tv", "tv_stand"} and not slots["tv"]["gap"]
    assert sorted(pool["tv"]) == ["stand_priced", "tv_unpriced"]
    [tv_filters] = [f for f in calls if "tv_stand" in f["categories"] and "tv" in f["categories"]]
    assert tv_filters["known_price"] and tv_filters["price_exempt"] == ["television", "tv"]


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
