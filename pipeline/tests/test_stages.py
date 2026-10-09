"""Real stages end to end with a fake genai client and fixed search results.

The room, candidates, selection, and layout come from the recorded legacy dining
room for four (fixtures/legacy_runs.json), which passed selection and the final
layout check.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from test_runtime import of_type, parse

from app import shared_stages
from app.contracts import PipelineRequest
from app.graph import MAX_CORRECTION_PROPOSALS, MAX_SELECTION_TURNS
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
    audit = TURN["constraint_audit"]
    return {
        "selected_assets": [{"uid": uid, "reason": "matches the brief"} for uid, count in items for _ in range(count)],
        "selection_strategy": {key: "" for key in ("summary", "higher_budget_additions", "lower_budget_savings", "gaps", "conflict")},
        "fit_satisfaction": TURN["fit_satisfaction"],
        "constraint_audit": {**audit, "attribute_constraint_checks": [{**check, "reason": "ok"} for check in audit["attribute_constraint_checks"]]},
    }


def layout(moved: dict[str, list[float]] | None = None) -> dict:
    moved = moved or {}
    return {
        "layout_summary": "",
        "layout": [
            {"uid": key, "position": moved.get(key, pose["position"]), "rotation": pose["rotation"]}
            for key, pose in PLACEMENTS.items()
        ],
    }


def chair_pose(position: list[float]) -> dict:
    return {"poses": [{"uid": "dining_chair_1", "x": position[0], "y": position[1],
                       "rotation_z": PLACEMENTS["dining_chair_1"]["rotation"][2]}]}


# A chair pushed into the table overlaps it (P0); the correction moves it back.
OVERLAPPING = layout({"dining_chair_1": TABLE_CENTER})
FIXED = chair_pose(PLACEMENTS["dining_chair_1"]["position"])


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


def run_pipeline(tmp_path: Path, genai: FakeGenai) -> tuple[list[dict], dict]:
    app = create_app(model=GeminiModel(genai), connect=lambda: None, runs_dir=tmp_path)

    async def main() -> str:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return (await client.post("/pipeline", json=REQUEST)).text

    events = parse(asyncio.run(main()))
    [record] = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    return events, record


def stage_runs(record: dict, stage: str, index: int | None) -> int:
    return sum(1 for s in record["stages"] if s["stage"] == stage and s["variant_index"] == index)


def test_shared_stages_run_once_and_each_variant_runs_its_stages(tmp_path, searches, monkeypatch):
    sideboard = next(record for record in POOL if record["asset_id"] == "sideboard_1")
    monkeypatch.setitem(sideboard, "mount_type", "wall_secured")
    monkeypatch.setitem(sideboard, "features", ["soft close drawers"])
    # The ottoman has no catalog product, so it is a gap and the run continues without it.
    ottoman = {"label": "velvet ottoman", "canonical_category": "ottoman", "count": 1, "exact": False,
               "optional": False, "acceptable_substitutes": [], "descriptors": ["velvet"]}
    genai = FakeGenai({
        "IntentPacket": intent_packet(ottoman),
        "Selection": selection(),
        "InitialLayout": OVERLAPPING,
        "Correction": FIXED,
    })

    events, record = run_pipeline(tmp_path, genai)

    for stage in ("interpret", "room", "retrieve"):
        assert stage_runs(record, stage, None) == 1
    for index in range(3):
        for stage in ("select", "place", "correct", "validate"):
            assert stage_runs(record, stage, index) == 1
    calls = {}
    for call in record["model_calls"]:
        calls[call["stage"]] = calls.get(call["stage"], 0) + 1
    assert calls == {"interpret": 1, "select": 3, "place": 3, "correct": 3}
    assert [len(genai.of(title)) for title in ("IntentPacket", "Selection", "InitialLayout", "Correction")] == [1, 3, 3, 3]

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
    for title in ("Selection", "InitialLayout", "Correction"):
        assert all("wall_secured" in prompt and "soft close drawers" in prompt for prompt in genai.of(title))
    # Variant directions: none for variant 0, the legacy dining texts for 1 and 2.
    directions = ["warm, rounded dining table", "compact, structured dining table"]
    assert [sum(text in prompt for prompt in genai.of("Selection")) for text in directions] == [1, 1]


def test_oversized_selection_steps_to_compact_then_capped_counts(tmp_path, searches):
    # Ten sideboards overcrowd the room on every turn.
    oversized = [(uid, 10 if uid == "sideboard_1" else count) for uid, count in TURN["items"]]
    genai = FakeGenai({
        "IntentPacket": intent_packet(),
        "Selection": selection(oversized),
        "InitialLayout": layout(),
        "Correction": FIXED,
    })

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
    genai = FakeGenai({"IntentPacket": packet, "Selection": selection(), "InitialLayout": layout(), "Correction": FIXED})

    events, record = run_pipeline(tmp_path, genai)

    assert len(of_type(events, "variant_ready")) == 3
    [slot] = [slot for slot in record["slots"] if slot["slot"] == "dining_table"]
    assert slot["candidates"] == 2 and "without them" in slot["note"]
    assert all("candidates ignore them" in prompt for prompt in genai.of("Selection"))


def test_blocking_layout_fails_the_variant(tmp_path, searches):
    genai = FakeGenai({
        "IntentPacket": intent_packet(),
        "Selection": selection(),
        "InitialLayout": OVERLAPPING,
        "Correction": chair_pose(TABLE_CENTER),
    })

    events, record = run_pipeline(tmp_path, genai)

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"layout_validation_failed"}
    assert all("overlaps" in e["errors"][0] for e in failed)
    assert all(stage_runs(record, "correct", index) == MAX_CORRECTION_PROPOSALS for index in range(3))
    assert events[-1]["type"] == "complete" and events[-1]["data"]["variants"] == []


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
        monkeypatch, "living_room", packet, lambda filters: [row for row in rows if row["category"] in filters["categories"]]
    )

    assert pool["sofa"] == ["sofa_acme", "sofa_other"]
    assert pool["rug"] == ["rug_turned"]
    assert pool["decor_plant"] == ["plant_design"]
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
