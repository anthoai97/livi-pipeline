"""Real stages end to end with a fake genai client and fixed search results.

The room, candidates, selection, and layout come from the recorded legacy dining
room for four (fixtures/legacy_runs.json), which passed selection and the final
layout check.
"""

import asyncio
import copy
import json
import math
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from test_rules import _room
from test_runtime import FakeJevClient, of_type, parse

from app import shared_stages, variant_stages
from app.contracts import PipelineRequest
from app.graph import MAX_SELECTION_TURNS
from app.jev import Jev, switches
from app.llm import GeminiModel
from app.main import create_app
from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.selection.constants import COFFEE_TABLE_HEIGHT_M, SIZE_TOLERANCE_M
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
# The recorded sideboard faced the table across 0.60 m; the 0.76 m storage front slides it along its wall past the table.
PLACEMENTS["sideboard_1"] = {**PLACEMENTS["sideboard_1"], "position": [PLACEMENTS["sideboard_1"]["position"][0], 3.6, 0.0]}
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


def arrangement(*moves: tuple) -> dict:
    """A final review answer that moves each (uid, x, y) of `moves`, turned to a fourth value when given, else unturned."""
    return {"review": REVIEW,
            "adjustments": [{"uid": uid, "x": x, "y": y, "rotation_z": turn[0] if turn else PLACEMENTS[uid]["rotation"][2]}
                            for uid, x, y, *turn in moves]}


# A chair pushed into the table overlaps it (P0); the final review moves it back.
OVERLAPPING = {**PLACEMENTS, "dining_chair_1": {**PLACEMENTS["dining_chair_1"], "position": TABLE_CENTER}}
FIXED = arrangement(("dining_chair_1", *PLACEMENTS["dining_chair_1"]["position"][:2]))
# Solver plans: the dining table in the front-left or front-right quarter, with the sideboard on the right or left wall.
LEFT_TABLE = [["dining", [False, False]], ["sideboard", "right"]]
RIGHT_TABLE = [["dining", [True, False]], ["sideboard", "right"]]
LEFT_TABLE_LEFT_SIDEBOARD = [["dining", [False, False]], ["sideboard", "left"]]


def solve_as(monkeypatch, placements: dict, *alternatives: dict, plans: tuple[list, ...] = ()) -> None:
    """Make the place stage's solver return `placements`, and `alternatives` as its other layouts, for the instances it is given.

    `plans` holds each layout's plan, result first; a layout without one has LEFT_TABLE.
    """

    def solve(instances, room, intent):
        keys = {variant_stages._key(asset) for asset in instances}
        plan = [*plans, *[LEFT_TABLE] * (1 + len(alternatives) - len(plans))]

        def only(layout: dict) -> dict:
            return {key: placement for key, placement in layout.items() if key in keys}

        return only(placements), {"unplaceable": [], "score": [0, 0, 0], "candidates": 0, "scored": 0, "elapsed": 0.0,
                                  "plan": plan[0],
                                  "alternatives": [{"layout": only(layout), "score": [0, 0, 0], "unplaceable": [], "plan": plan[n + 1]}
                                                   for n, layout in enumerate(alternatives)]}

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


SOLVES: dict[str, tuple[dict, dict]] = {}
SOLVES_LOCK = threading.Lock()


@pytest.fixture
def cached_solver(monkeypatch):
    """The real solver, memoized by its inputs: designs and turns that place the same instances solve once."""
    solve = variant_stages.solve_layout

    def cached(instances, room, intent):
        key = json.dumps([instances, room, intent], sort_keys=True, default=repr)
        with SOLVES_LOCK:
            if key not in SOLVES:
                SOLVES[key] = solve(instances, room, intent)
            return copy.deepcopy(SOLVES[key])

    monkeypatch.setattr(variant_stages, "solve_layout", cached)


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
        [review] = [note for note in notes if note.startswith(f"final review: {REVIEW}; adjusted dining_chair_1: kept, score [")]
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


def oversized_table(monkeypatch) -> list[tuple[str, int]]:
    """Add a 4.0 x 2.6 m dining table to the pool: the solver cannot fit it and four chairs, although the cluster estimate fits.
    Returns TURN with that table."""
    table = next(record for record in POOL if record["asset_id"] == "dining_table_3")
    search_pool(monkeypatch, [*POOL, {**table, "asset_id": "dining_table_9", "title": "Lane Dining Table", "width_m": 4.0, "depth_m": 2.6}])
    return [("dining_table_9" if uid == "dining_table_3" else uid, count) for uid, count in TURN["items"]]


def test_selection_the_solver_cannot_place_steps_to_compact_then_capped_counts_then_loses_what_does_not_fit(tmp_path, monkeypatch,
                                                                                                            cached_solver):
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(oversized_table(monkeypatch))})

    events, record = run_pipeline(tmp_path, genai)

    compact, capped = "=== ADVISORY FIT ESTIMATE ===", "=== ACCEPTED FIT DECISION ==="
    marker = "VARIANT DIRECTION: Choose a warm, rounded dining table"
    prompts = [prompt for prompt in genai.of("Selection") if marker in prompt]
    assert len(prompts) == MAX_SELECTION_TURNS
    assert compact not in prompts[0] and capped not in prompts[0]
    assert compact in prompts[1] and capped not in prompts[1]
    assert all(capped in prompt for prompt in prompts[2:])
    assert "Selection target for this run: 4 dining chairs and 1 dining table" in prompts[2]
    # The error names what the solver left out, with sizes.
    assert all("LAYOUT FIT: the layout solver could not place 4 x dining_chair_39 (dining_chair_39, 0.53m x 0.55m), "
               "dining_table_9 (Lane Dining Table, 4.00m x 2.60m). Choose products from the same slots that fit together, "
               "keeping the requested items and counts." in prompt for prompt in prompts[1:])
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert [note for note in notes if "fit step" in note] == ["selection turn 2: fit step compact", "selection turn 3: fit step capped"]
        failures = [note for note in notes if " failed: " in note]
        assert [note.split(": LAYOUT FIT: ")[0] for note in failures] == [
            f"selection turn {turn} failed" for turn in range(1, MAX_SELECTION_TURNS + 1)]
        # Removing products cannot fix what the solver leaves out, so select drops nothing; finish removes the items left out.
        assert not [note for note in notes if note.startswith("selection turn") and " dropped " in note]
        # The oversized anchor table is never removed: the design is delivered with the findings naming only it.
        [dropped] = [note for note in notes if note.startswith("dropped ")]
        assert "dining_table_1" not in dropped and "blocking findings that name no removable item" in dropped
    assert len(of_type(events, "variant_ready")) == 3


def test_lamp_too_large_for_its_side_table_keeps_the_fit_step(tmp_path, monkeypatch, cached_solver):
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    lamp = {**sideboard, "asset_id": "table_lamp_1", "title": "Big Table Lamp", "category": "table_lamp", "width_m": 0.6,
            "depth_m": 0.6, "placement_type": "surface", "price": 100}
    side_table = {**sideboard, "asset_id": "side_table_1", "category": "side_table", "width_m": 0.5, "depth_m": 0.5, "price": 150}
    search_pool(monkeypatch, [*POOL, lamp, side_table])
    packet = intent_packet(item("table lamp", "table_lamp"), item("side table", "side_table"))
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection([*TURN["items"], ("table_lamp_1", 1), ("side_table_1", 1)])})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    # The room fits its pieces, so no fit step; the solver cannot place the lamp, but a left-out tabletop item never
    # steps down to compact or capped counts.
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert [note for note in notes if "fit step" in note] == []
        assert stage_runs(record, "select", index) == MAX_SELECTION_TURNS
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3 * (MAX_SELECTION_TURNS - 1)
    for prompt in retries:
        assert "LAYOUT FIT: the layout solver could not place table_lamp_1 (Big Table Lamp, 0.60m x 0.60m)." in prompt
        assert "=== ACCEPTED FIT DECISION ===" not in prompt


def over_budget_chairs(monkeypatch, *cheaper: dict) -> None:
    """Chairs at $1,200 put TURN at $6,598, over the $5,500 allowance; the chair slot also holds the `cheaper` chairs
    after it, in order, each a copy of it with its fields."""
    chair = next(record for record in POOL if record["asset_id"] == "dining_chair_39")
    monkeypatch.setitem(chair, "price", 1200)
    search_pool(monkeypatch, [*POOL, *({**chair, "title": fields["asset_id"], **fields} for fields in cheaper)])


def test_over_budget_selection_is_repaired_with_cheaper_products_from_the_same_slot(tmp_path, monkeypatch):
    over_budget_chairs(monkeypatch, {"asset_id": "dining_chair_8", "price": 300}, {"asset_id": "dining_chair_9", "price": 350})
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})
    # Jev rejects the style of the first cheaper chair, so the repair takes the second.
    jev = FakeJevClient(lambda text: 0.1 if "style and palette" in text and "dining_chair_8" in text else 0.9)

    events, record = run_pipeline(tmp_path, genai, jev)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    for variant in ready:
        # The requested four chairs stay; the anchor table and the sideboard are unchanged.
        assert [asset["asset_id"] for asset in variant["selected_assets"] if asset["category"] == "dining_chair"] == ["dining_chair_9"] * 4
        assert variant["total_cost"] == 899 + 4 * 350 + 899
    for index in range(3):
        assert stage_runs(record, "select", index) == 1
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert "swap repair: dining_chair_39 -> dining_chair_9 (budget)" in notes
        assert not [note for note in notes if " failed: " in note]


def test_selection_under_the_budget_floor_is_filled_with_pricier_products_from_the_same_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(variant_stages, "BUDGET_FLOOR_PCT", 0.8)
    search_pool(monkeypatch, POOL)
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    # Each design's passing selection now spends at least the floor and never more than the budget.
    budget = REQUEST["budget"]
    selections = [event["data"] for event in of_type(events, "node_complete") if "items" in event.get("data", {})]
    assert len(selections) == 3
    assert all(data["valid"] and budget * 0.8 <= data["total_cost"] <= budget for data in selections)
    repairs = [note["text"] for note in record["notes"] if note["text"].startswith("swap repair")]
    assert len(repairs) == 3 and all(note.startswith("swap repair: ") and note.endswith("(budget)") for note in repairs)


def test_over_budget_selection_the_repair_cannot_validate_is_retried_keeping_requested_items(tmp_path, monkeypatch):
    # The cheaper chair is not a usable single chair, so the repaired selection fails validation.
    over_budget_chairs(monkeypatch, {"asset_id": "dining_chair_8", "price": 300, "width_m": 2.0})
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    # The last turn drops the sideboard to cut the cost; the requested chairs stay, so the design is delivered over budget.
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all(variant["total_cost"] == 899 + 4 * 1200 for variant in ready)
    dropped = [note["text"] for note in record["notes"] if " dropped " in note["text"]]
    assert dropped == [f"selection turn {MAX_SELECTION_TURNS}: dropped sideboard_1; still fails: OVER BUDGET: "
                       "Selection costs $5699.00 (Flex limit: $5500.00)."] * 3
    rejected = [note["text"] for note in record["notes"] if note["text"].startswith("swap repair")]
    assert len(rejected) == 3 * MAX_SELECTION_TURNS
    assert all(note.startswith("swap repair rejected: dining_chair_39 -> dining_chair_8 (budget); ") and "DINING CHAIR DIMENSIONS" in note
               for note in rejected)
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3 * (MAX_SELECTION_TURNS - 1)
    assert all("OVER BUDGET: Selection costs $6598.00" in prompt and "Keep every requested item and its count" in prompt
               and "Do not drop requested items to save money." in prompt for prompt in retries)


@pytest.mark.parametrize("matching", [True, False])
def test_style_rejection_is_repaired_from_the_whole_slot_or_kept_as_a_warning(tmp_path, monkeypatch, matching):
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    others = [f"sideboard_{n}" for n in range(2, 6)]
    search_pool(monkeypatch, [*POOL, *({**sideboard, "asset_id": uid, "title": uid} for uid in others)])
    solve_as(monkeypatch, PLACEMENTS)
    # A requested sideboard keeps all five sideboards in its slot.
    genai = FakeGenai({"IntentPacket": intent_packet(item("sideboard", "sideboard")), "Selection": selection()})
    # Jev rejects the style of every sideboard but the fifth, or of all of them.
    styled = {"sideboard_1", *others[:3], *([] if matching else others[3:])}
    jev = FakeJevClient(lambda text: 0.1 if "style and palette" in text and any(uid in text for uid in styled) else 0.9)

    events, record = run_pipeline(tmp_path, genai, jev)

    # One model call per design: the repair asks about all four other sideboards in one request, and swaps the
    # rejected one for the first match, deeper than three in the slot, or keeps it with a warning when none matches.
    assert [call["stage"] for call in record["model_calls"]].count("select") == 3
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    kept = "style kept: sideboard_1 (no candidate in the slot matches the anchor)"
    for variant in ready:
        sideboards = [asset["asset_id"] for asset in variant["selected_assets"] if asset["category"] == "sideboard"]
        assert sideboards == ["sideboard_5" if matching else "sideboard_1"]
        assert any(warning.startswith("STYLE KEPT: sideboard_1 ")
                   for warning in variant["selection_validation"]["warnings"]) is not matching
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert ("swap repair: sideboard_1 -> sideboard_5 (style)" in notes) is matching
        assert (kept in notes) is not matching
        assert not [note for note in notes if " failed: " in note]
        # The audit, one repair request, and, after a swap, the audit of the repaired selection.
        checks = [call["questions"] for call in record["jev_calls"] if call["use"] == "check" and call["variant_index"] == index]
        assert checks[1] == 4 and len(checks) == (3 if matching else 2)


@pytest.mark.parametrize("lights", [("pendant_big", "pendant_small"), ("pendant_big",)])
def test_light_too_large_for_the_table_is_swapped_when_a_fitting_one_exists(tmp_path, monkeypatch, lights):
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    sides = {"pendant_big": 1.0, "pendant_small": 0.5}  # the dining table leaves about 0.74 m inside its edges
    search_pool(monkeypatch, [*POOL, *({**sideboard, "asset_id": uid, "title": uid, "category": "pendant_light",
                                        "placement_type": "ceiling", "width_m": sides[uid], "depth_m": sides[uid],
                                        "height_m": 0.6, "price": 200} for uid in lights)])
    solve_as(monkeypatch, PLACEMENTS)
    # Without the minimal brief this dining room needs a pendant or chandelier.
    packet = {**intent_packet(), "functional_hints": [], "fit_flexibility": "balanced"}
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection([*TURN["items"], ("pendant_big", 1)])})

    events, record = run_pipeline(tmp_path, genai)

    # Swapped when a fitting light exists; otherwise the size finding is a warning. Neither retries the model.
    assert [stage_runs(record, "select", index) for index in range(3)] == [1, 1, 1]
    repairs = [note["text"] for note in record["notes"] if note["text"].startswith("swap repair")]
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    if len(lights) == 2:
        assert repairs == ["swap repair: pendant_big -> pendant_small (size)"] * 3
        assert all("pendant_small" in variant["selection_validation"]["validated_uids"] for variant in ready)
    else:
        assert repairs == []
        assert all(any(warning.startswith("SIZE: pendant_big ") for warning in variant["selection_validation"]["warnings"])
                   for variant in ready)


def test_missing_required_item_retries_the_model_with_the_previous_uids_and_errors(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS)
    plantless = TURN["items"][:3]
    genai = FakeGenai({"IntentPacket": intent_packet(),
                       "Selection": lambda prompt: selection() if "FAILED VALIDATION" in prompt else selection(plantless)})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    assert [stage_runs(record, "select", index) for index in range(3)] == [2, 2, 2]
    firsts = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" not in prompt]
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    # The designs share the prompt up to their catalogs, and a retry is the design's first prompt plus its errors.
    assert len({prompt.split("CATALOG (")[0] for prompt in firsts}) == 1
    uids = json.dumps([uid for uid, count in plantless for _ in range(count)])
    for retry in retries:
        [tail] = [retry[len(first):] for first in firsts if retry.startswith(first)]
        assert tail.startswith(f"\n\nPREVIOUS SELECTION (turn 1) FAILED VALIDATION:\n{uids}\nERRORS:\n- ")
        assert "\n- MISSING REQUIRED: Include at least one non-shoppable decor plant" in tail
        assert "metrics" not in tail and "status" not in tail and "warnings" not in tail


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


def test_completeness_piece_with_no_catalog_product_is_a_gap_and_selection_passes(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS)
    # Without the minimal brief this spacious dining room also needs a sideboard (the pool has one) and a
    # pendant or chandelier (it has none).
    packet = {**intent_packet(), "functional_hints": [], "fit_flexibility": "balanced"}
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection()})

    events, record = run_pipeline(tmp_path, genai)

    [slot] = [slot for slot in record["slots"] if slot["slot"] == "overhead_lighting"]
    assert slot["gap"] and slot["candidates"] == 0 and "reported as a gap" in slot["note"]
    assert all("(overhead_lighting)" in prompt.split("CATALOG GAPS")[1] for prompt in genai.of("Selection"))
    assert [stage_runs(record, "select", index) for index in range(3)] == [1, 1, 1]
    assert len(of_type(events, "variant_ready")) == 3


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

    # The slot holds no other sideboard, so the mismatched one stays as a warning; the requested chairs' attribute
    # failure fails every turn, and each retry names the chairs as rejected.
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all("sideboard_1" in variant["render_manifest"]["layout"] for variant in ready)
    for index in range(3):
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert notes.count("style kept: sideboard_1 (no candidate in the slot matches the anchor)") == 1
        failures = [note for note in notes if note.startswith("selection turn") and " failed: " in note]
        assert failures == [f"selection turn {turn} failed: SELECTION CONSTRAINT AUDIT: attribute constraint \"soft neutral\" "
                            "is not satisfied by ['dining_chair_39']" for turn in range(1, MAX_SELECTION_TURNS + 1)]
        # The kept sideboard is no longer a rejection after the first check.
        rejections = [note for note in notes if note.startswith("jev check rejected")]
        assert rejections == ["jev check rejected: style ['sideboard_1']; attributes \"soft neutral\" by ['dining_chair_39']",
                              *["jev check rejected: style []; attributes \"soft neutral\" by ['dining_chair_39']"] * (MAX_SELECTION_TURNS - 1)]
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3 * (MAX_SELECTION_TURNS - 1)
    assert all("REJECTED BY THE STYLE AND ATTRIBUTE CHECK (do not pick again): dining_chair_39\n" in prompt for prompt in retries)
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


def test_final_review_fixes_blocking_findings_the_solver_left(tmp_path, searches, monkeypatch, cached_solver):
    solve = variant_stages.solve_layout
    solved: dict = {}

    def chair_on_the_plant(instances, room, intent):
        layout, report = solve(instances, room, intent)
        solved.setdefault("layout", layout)
        moved = {**layout, "dining_chair_1": {**layout["dining_chair_1"], "position": list(layout["planter_1"]["position"])}}
        return moved, {**report, "alternatives": []}

    def restore(prompt: str) -> dict:
        pose = solved["layout"]["dining_chair_1"]
        return {"review": "a chair stands on the plant",
                "adjustments": [{"uid": "dining_chair_1", "x": pose["position"][0], "y": pose["position"][1],
                                 "rotation_z": pose["rotation"][2]}]}

    monkeypatch.setattr(variant_stages, "solve_layout", chair_on_the_plant)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": restore})

    events, record = run_pipeline(tmp_path, genai)

    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3 and all(len(variant["selected_assets"]) == 7 for variant in ready)
    assert delivered(events, "dining_chair_1") == [solved["layout"]["dining_chair_1"]["position"][:2]] * 3
    # One review call per design, with one image: the design's own layout.
    assert {call["stage"] for call in record["model_calls"]} == {"interpret", "select", "finish"}
    assert [count for title, count in genai.images if title == "Arrangement"] == [1, 1, 1]
    for index in range(3):
        assert [stage_runs(record, stage, index) for stage in ("place", "finish")] == [1, 1]
        notes = [note["text"] for note in record["notes"] if note["variant_index"] == index]
        assert any(note.startswith("solver: score [") for note in notes)
        assert any(note.startswith("final review: a chair stands on the plant; adjusted dining_chair_1: kept,")
                   for note in notes)
    # The record keeps each design's solver plan; the dining table's quarter comes first.
    assert all(variant["plan"][0][0] == "dining" for variant in record["variants"])


# The plant in the other front corner, or by the left wall: the checks score both the same as PLACEMENTS.
CORNER = {**PLACEMENTS, "planter_1": {**PLACEMENTS["planter_1"], "position": [3.6, 0.6, 0.0]}}
LEFT_WALL = {**PLACEMENTS, "planter_1": {**PLACEMENTS["planter_1"], "position": [0.5, 2.3, 0.0]}}


def delivered(events: list[dict], key: str) -> list[list[float]]:
    """Each design's delivered position of `key`, by variant index."""
    variants = sorted((event["data"]["variant"] for event in of_type(events, "variant_ready")), key=lambda variant: variant["variant_index"])
    return [variant["render_manifest"]["layout"][key]["position"][:2] for variant in variants]


def stage_data(events: list[dict], node: str) -> list[dict]:
    """Each design's node_complete data for a stage node, by variant index."""
    return [event["data"] for event in sorted((event for event in of_type(events, "node_complete") if event["node"] == node),
                                              key=lambda event: event["variant_index"])]


def solver_notes(record: dict) -> list[str]:
    """The end of each design's solver note (the layout it took), by variant index."""
    return [next(note["text"] for note in record["notes"] if note["variant_index"] == index and note["text"].startswith("solver:"))
            .split("; ")[-1] for index in range(3)]


def planter_plans(record: dict) -> list[tuple]:
    """Each ready design's planter position and plan, sorted: the designs take layouts first come, so in any order."""
    return sorted((variant["layout"]["planter_1"]["position"][:2], variant["plan"]) for variant in record["variants"])


def test_each_design_takes_a_different_tied_layout(tmp_path, searches, monkeypatch):
    # OVERLAPPING scores worse than PLACEMENTS, so only the other three tie; each has its own zone plan.
    back_left = [["dining", [False, True]], ["sideboard", "right"]]
    solve_as(monkeypatch, PLACEMENTS, OVERLAPPING, CORNER, LEFT_WALL, plans=(LEFT_TABLE, LEFT_TABLE, RIGHT_TABLE, back_left))
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    assert planter_plans(record) == [([0.5, 2.3], back_left), ([0.6, 0.6], LEFT_TABLE), ([3.6, 0.6], RIGHT_TABLE)]
    assert sorted(variant["render_manifest"]["layout"]["planter_1"]["position"][:2] for variant in
                  (event["data"]["variant"] for event in of_type(events, "variant_ready"))) == [[0.5, 2.3], [0.6, 0.6], [3.6, 0.6]]
    assert sorted((data["plans"], data["plan"]) for data in stage_data(events, "layout_initial")) == [(3, 1), (3, 2), (3, 3)]
    assert sorted(solver_notes(record)) == [f"tied layout {n} of 3 (3 main layouts)" for n in (1, 2, 3)]
    # Each review sees only its own layout.
    assert [count for title, count in genai.images if title == "Arrangement"] == [1, 1, 1]
    for prompt in genai.of("Arrangement"):
        assert "Review it." in prompt and "variant A" not in prompt and '"choice"' not in prompt
        assert REQUEST["user_intent"] in prompt and "planter_1 (planter)" in prompt and "door_0" in prompt
        assert "REQUESTED ITEMS: light wood dining table for four x1, soft neutral dining chairs x4" in prompt
        assert "STYLE HINTS: scandinavian, cozy, minimalist" in prompt
        assert "P0 — ARCHITECTURE" in prompt and "DINING ROOM RULES" in prompt and "COORDINATES:" in prompt
    poses = [json.loads(prompt.split("POSES (instance key: [x, y, rotation_z]):\n")[1].split("\n")[0]) for prompt in genai.of("Arrangement")]
    assert sorted(pose["planter_1"][:2] for pose in poses) == [[0.5, 2.3], [0.6, 0.6], [3.6, 0.6]]
    assert [(data["review"], data["valid"]) for data in stage_data(events, "render_scene")] == [(REVIEW, True)] * 3
    assert "layout_pick" not in stage_data(events, "render_scene")[0]


def test_designs_differ_in_a_wall_piece_after_the_zone_plans_run_out(tmp_path, searches, monkeypatch):
    # PLACEMENTS and CORNER share the table's quarter and differ in the sideboard's wall; LEFT_WALL has another quarter.
    solve_as(monkeypatch, PLACEMENTS, CORNER, LEFT_WALL, plans=(LEFT_TABLE, LEFT_TABLE_LEFT_SIDEBOARD, RIGHT_TABLE))
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    # The first two take the two table quarters; the last takes the sideboard on the other wall.
    assert planter_plans(record) == [([0.5, 2.3], RIGHT_TABLE), ([0.6, 0.6], LEFT_TABLE), ([3.6, 0.6], LEFT_TABLE_LEFT_SIDEBOARD)]
    first = solver_notes(record).index("tied layout 1 of 3 (2 main layouts)")
    assert sorted(solver_notes(record)) == ["tied layout 1 of 3 (2 main layouts)",
                                            f"tied layout 2 of 3 (2 main layouts), repeats the main layout of variant {first}",
                                            "tied layout 3 of 3 (2 main layouts)"]


def test_a_design_repeats_a_main_layout_only_when_the_ties_run_out(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS, CORNER, plans=(LEFT_TABLE, RIGHT_TABLE))
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement()})

    events, record = run_pipeline(tmp_path, genai)

    # With both taken, the last design takes the best layout again and the record says so.
    assert planter_plans(record) == [([0.6, 0.6], LEFT_TABLE), ([0.6, 0.6], LEFT_TABLE), ([3.6, 0.6], RIGHT_TABLE)]
    assert sorted((data["plans"], data["plan"]) for data in stage_data(events, "layout_initial")) == [(2, 1), (2, 1), (2, 2)]
    first = solver_notes(record).index("tied layout 1 of 2 (2 main layouts)")
    assert sorted(solver_notes(record)) == ["tied layout 1 of 2 (2 main layouts)",
                                            f"tied layout 1 of 2 (2 main layouts), repeats the main layout of variant {first}",
                                            "tied layout 2 of 2 (2 main layouts)"]


# The solver leaves floor lamps off-axis; a room-aligned turn is not a quarter turn from that, but is allowed.
TILTED = {**PLACEMENTS, "planter_1": {**PLACEMENTS["planter_1"], "rotation": [0.0, 0.0, 3.37]}}


@pytest.mark.parametrize(("solved", "moves", "outcome", "planter"), [
    (PLACEMENTS, [("planter_1", 0.7, 0.6)], "adjusted planter_1: kept, score [0, 0, 0] -> [0, 0, 0]", [0.7, 0.6]),  # a tie keeps it
    (PLACEMENTS, [("planter_1", 3.6, 0.6)], "adjusted planter_1: kept, score [0, 0, 0] -> [0, 0, 0]", [3.6, 0.6]),  # a 3 m move
    (PLACEMENTS, [("dining_chair_1", 2.535, 2.8)], "adjusted dining_chair_1: reverted, score [0, 0, 0] -> [1, ",  # into the table
     [0.6, 0.6]),
    (PLACEMENTS, [("dining_chair_1", 2.535, 2.8), ("planter_1", 0.7, 0.6)],  # the bad move reverts the set, so each is checked alone
     "reverted, score [0, 0, 0] -> [1, 1, 1]; one at a time: kept planter_1, reverted dining_chair_1, score [0, 0, 0] -> [0, 0, 0]",
     [0.7, 0.6]),
    (PLACEMENTS, [("sofa_9", 1.0, 1.0, 0.0), ("planter_1", 0.7, 0.6, PLACEMENTS["planter_1"]["rotation"][2] + 0.5)],
     "rejected unknown, non-finite, or off-axis: sofa_9, planter_1", [0.6, 0.6]),
    (TILTED, [("planter_1", 0.7, 0.6, math.pi)], "adjusted planter_1: kept", [0.7, 0.6]),  # squared to the room
])
def test_final_review_adjustments_are_kept_only_when_not_worse(tmp_path, searches, monkeypatch, solved, moves, outcome, planter):
    solve_as(monkeypatch, solved)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": arrangement(*moves)})

    events, record = run_pipeline(tmp_path, genai)

    assert delivered(events, "planter_1") == [planter] * 3
    assert delivered(events, "dining_chair_1") == [PLACEMENTS["dining_chair_1"]["position"][:2]] * 3
    reviews = [note["text"] for note in record["notes"] if note["text"].startswith(f"final review: {REVIEW}")]
    assert len(reviews) == 3 and all(outcome in note for note in reviews)


def test_failed_final_review_delivers_the_design_layout(tmp_path, searches, monkeypatch):
    solve_as(monkeypatch, PLACEMENTS, CORNER, plans=(LEFT_TABLE, RIGHT_TABLE))
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": selection(), "Arrangement": DEADLINE})

    events, record = run_pipeline(tmp_path, genai)

    # Each design keeps the layout it took, not the solver's best.
    assert planter_plans(record) == [([0.6, 0.6], LEFT_TABLE), ([0.6, 0.6], LEFT_TABLE), ([3.6, 0.6], RIGHT_TABLE)]
    assert [call["error"] is not None for call in record["model_calls"] if call["stage"] == "finish"] == [True] * 3
    assert sum(note["text"].startswith("final review failed, kept the solver's layout: ModelCallError") for note in record["notes"]) == 3
    assert [(data["review"], data["valid"]) for data in stage_data(events, "render_scene")] == [(None, True)] * 3


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
    assert ids(pools, "planter") == [["plant_4", "plant_3", "plant_2", "plant_1"]] * 3
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


def cloned_selection():
    """Select TURN's items from the clones in the prompt: on the first turn the first one marked shared, after that
    the first one not marked shared."""
    def answer(prompt: str) -> dict:
        rows = [line.split(",") for line in prompt.splitlines() if "_c" in line.split(",")[0]]
        shared = "FAILED VALIDATION" not in prompt
        return selection([(next(cells[0] for cells in rows if cells[0].startswith(f"{uid}_c") and (cells[-1] == "yes") == shared),
                           count) for uid, count in TURN["items"]])
    return answer


def test_selection_over_the_reuse_rate_is_rejected_and_retried(tmp_path, monkeypatch):
    cloned_searches(monkeypatch, 12)
    solve_as(monkeypatch, PLACEMENTS)
    genai = FakeGenai({"IntentPacket": intent_packet(), "Selection": cloned_selection()})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    for index in range(3):
        assert stage_runs(record, "select", index) == 2
    retries = [prompt for prompt in genai.of("Selection") if "FAILED VALIDATION" in prompt]
    assert len(retries) == 3
    assert all("REUSE LIMIT: 4 of 4 products are shared with other variants; at most 2 may be." in p for p in retries)
    assert all("At most 50% of your distinct products may be shared ones." in prompt for prompt in genai.of("Selection"))


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
    # No requested slot is a gap; the living room's required lamp has no product in these rows, so it is a catalog gap.
    assert [slot_id for slot_id, slot in slots.items() if slot["gap"]] == ["lighting"]
    assert {entry["slot"]: entry["candidates"] for entry in run.slots}["sofa"] == 2


def test_retrieve_merges_categories_round_robin_and_keeps_up_to_fetch(monkeypatch):
    packet = {"normalized_prompt": "a sofa", "requested_items": [item("sofa", "sofa")]}
    # Distance order: every floor lamp is nearer than the two table lamps.
    lamps = [product(f"floor_{n}", "floor_lamp", 0.4, 0.4) for n in range(40)]
    lamps += [product(f"table_{n}", "table_lamp", 0.3, 0.3) for n in range(2)]

    slots, pool, run, _, _ = retrieve(
        monkeypatch, "living_room", packet, lambda filters: [row for row in lamps if row["category"] in filters["categories"]]
    )

    assert slots["lighting"]["kind"] == "required"  # a living room requires a lamp (issue #54)
    assert pool["lighting"][:5] == ["floor_0", "table_0", "floor_1", "table_1", "floor_2"]
    assert len(pool["lighting"]) == shared_stages.FETCH > shared_stages.KEEP["required"]
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

    # A media support is not a TV substitute, so the TV slot holds only TVs.
    assert "tv" in slots["tv"]["categories"] and "tv_stand" not in slots["tv"]["categories"] and not slots["tv"]["gap"]
    assert pool["tv"] == ["tv_unpriced"]
    [tv_filters] = [f for f in calls if "tv" in f["categories"]]
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


def test_requested_coffee_table_still_searches_side_tables(monkeypatch):
    packet = {"normalized_prompt": "a sofa and a wooden coffee table",
              "requested_items": [item("sofa", "sofa"), item("wooden coffee table", "coffee_table")]}
    rows = [product("coffee_1", "coffee_table", 1.1, 0.6, height_m=0.45), product("side_1", "side_table", 0.5, 0.5, height_m=0.55)]

    slots, pool, _, _, _ = retrieve(monkeypatch, "living_room", packet,
                                    lambda filters: [row for row in rows if row["category"] in filters["categories"]],
                                    room_area=(5.0, 5.5))

    assert pool["coffee_table"] == ["coffee_1"]
    assert slots["surface"]["kind"] == "optional" and pool["surface"] == ["side_1"]



def test_living_room_drops_coffee_tables_outside_the_height_limits(monkeypatch):
    # Catalog heights are bounding boxes: 1 cm under the limit passes, past the tolerance does not.
    low = COFFEE_TABLE_HEIGHT_M[0]
    packet = {"normalized_prompt": "a sofa and a coffee table",
              "requested_items": [item("sofa", "sofa"), item("coffee table", "coffee_table")]}
    rows = [product("coffee_low", "coffee_table", 1.1, 0.6, height_m=low - SIZE_TOLERANCE_M - 0.01),
            product("coffee_near", "coffee_table", 1.1, 0.6, height_m=low - 0.01),
            product("coffee_ok", "coffee_table", 1.1, 0.6, height_m=0.42)]

    _, pool, _, _, _ = retrieve(monkeypatch, "living_room", packet,
                                lambda filters: [row for row in rows if row["category"] in filters["categories"]])

    assert sorted(pool["coffee_table"]) == ["coffee_near", "coffee_ok"]

# --- select: TVs ------------------------------------------------------------

STAND = product("stand", "tv_stand", 1.5, 0.4, height_m=0.55)
WIDE_TV = product("tv_wide", "tv", 1.6, 0.25, placement_type="surface", is_purchasable=False, price=None)  # wider than the stand
FITTING_TV = product("tv_fits", "tv", 1.2, 0.25, placement_type="surface", is_purchasable=False, price=None)


def select_with_requested_tv(model_uids: list[str], tvs: list[dict]) -> tuple[dict, list[str]]:
    """One select turn in the recorded dining room whose brief also asks for a TV; the model answers `model_uids`.
    The TVs fill the requested TV slot and STAND an optional media slot."""
    tv = {"label": "TV", "canonical_category": "tv", "count": 1, "exact": False, "optional": False,
          "acceptable_substitutes": [], "descriptors": []}
    intent = coerce_intent_packet({**RUN["intent"], "requested_items": [*RUN["intent"]["requested_items"], tv]}, room_type="dining_room")
    _, room = _room(RUN)
    request = PipelineRequest(**REQUEST)
    pool = {"tv": tvs}
    for uid, _ in TURN["items"]:
        pool.setdefault(RUN["assets"][uid]["category"], []).append(RUN["assets"][uid])
    slots = [{"id": slot_id, "kind": "requested", "category": slot_id, "label": slot_id, "count": 1, "required": True, "gap": False,
              "relaxed": False} for slot_id in pool]
    pool["media"] = [STAND]
    slots.append({"id": "media", "kind": "optional", "category": None, "label": "media", "gap": False, "relaxed": False})
    state = {"variant_index": 0, "direction": "", "pool": pool, "fit_step": None,
             "shared": {"request": request, "intent": intent, "room": room, "slots": slots}}
    answer = {"selected_assets": [{"uid": uid} for uid in model_uids], "gaps": ""}
    run = RunContext("run", request, GeminiModel(FakeGenai({"Selection": answer})), 3, product_reuse_rate=1.0)
    update = asyncio.run(variant_stages.select(state, StageContext(run, "select", 0)))
    return update, [note["text"] for note in run.notes]


@pytest.mark.parametrize(("extra", "tvs", "note", "added", "placement"), [
    (["stand"], [WIDE_TV, FITTING_TV], "added TV tv_fits", "tv_fits", "tabletop"),  # the first TV that fits the selected stand
    (["stand"], [WIDE_TV], "added TV tv_wide", "tv_wide", "wall_mounted"),  # no TV fits the stand: it hangs on the wall instead
    ([], [WIDE_TV, FITTING_TV], "added TV tv_wide", "tv_wide", "wall_mounted"),  # no stand selected: the first ranked TV
    (["stand", "tv_wide"], [WIDE_TV, FITTING_TV], "replaced TV tv_wide with tv_fits", "tv_fits", "tabletop"),  # too wide: swapped
])
def test_select_gives_a_requested_tv_a_fitting_stand_or_hangs_it_on_the_wall(extra, tvs, note, added, placement):
    model_uids = [uid for uid, count in TURN["items"] for _ in range(count)] + extra

    update, notes = select_with_requested_tv(model_uids, tvs)

    assert update["selection_validation"]["valid"], update["selection_validation"]["errors"]
    assert f"selection turn 1: {note}" in notes
    assert update["selection"]["selected_assets"][-1]["uid"] == added
    [tv] = [instance for instance in update["instances"] if instance["category"] == "tv"]
    assert tv["placement_mode"] == placement
    assert tv.get("paired_support_uid") == ("tv_stand_1" if placement == "tabletop" else None)
