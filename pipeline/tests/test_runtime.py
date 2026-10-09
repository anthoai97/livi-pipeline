"""Runtime behavior through POST /pipeline with fake stages and a fake genai client."""

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from pydantic import BaseModel

from app import contracts
from app.graph import MAX_CORRECTION_PROPOSALS, MAX_SELECTION_TURNS, Stages
from app.llm import GeminiModel, gemini_client
from app.main import create_app

REQUEST = {
    "user_intent": "cozy living room with a cream sofa",
    "budget": 3000,
    "room_type": "living_room",
    "room_area": [4.0, 5.0],
    "room_vertices": [[0, 0], [4, 0], [4, 5], [0, 5]],
    "wall_height": 2.7,
    "room_doors": [],
    "room_windows": [],
    "wall_finishes": {},
}


class Plan(BaseModel):
    note: str


class FakeGenai:
    """Stands in for genai.Client. Every call returns a Plan with fixed usage.

    `delays` makes calls whose contents match a key wait that many seconds.
    """

    def __init__(self, delays: dict[str, float] | None = None):
        self.delays = delays or {}
        self.calls: list[str] = []
        self.cancelled = 0
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model: str, contents: str, config: types.GenerateContentConfig):
        self.calls.append(contents)
        try:
            await asyncio.sleep(self.delays.get(contents, 0))
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text='{"note": "ok"}')]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1000,
                cached_content_token_count=200,
                candidates_token_count=100,
                thoughts_token_count=50,
                total_token_count=1150,
            ),
            model_version=model,
        )


SOFA = {
    "asset_id": "a1",
    "source_table": "catalog.assets",
    "title": "Cream boucle sofa",
    "category": "sofa",
    "image_url": "https://example.com/sofa.png",
    "model_url": "https://example.com/sofa.glb",
    "product_url": "https://example.com/sofa",
    "width_m": 2.0,
    "depth_m": 0.9,
    "height_m": 0.8,
    "placement_type": "floor",
    "center": [0.02, 0.4, -0.01],
    "front_view": 0,
    "topdown_url": "https://example.com/sofa-top.png",
    "mount_type": "freestanding",
    "features": ["removable cushions"],
    "price": 1200.0,
    "currency": "USD",
}
INSTANCE: contracts.Instance = {
    "instance_key": "sofa_1",
    "category": "sofa",
    "position": [2.0, 1.0, 0.0],
    "rotation": [0.0, 0.0, 1.57],
    "placement_mode": "floor",
    "asset": SOFA,
}


async def interpret(state, ctx):
    await ctx.generate(Plan, "interpret")
    return {"intent": {"items": ["sofa"]}}


async def room(state, ctx):
    return {"room": {"walls": 4}}


async def retrieve(state, ctx):
    ctx.run.record_slot("sofa", 10, False)
    ctx.run.record_slot("bookcase", 0, True)
    return {"slots": [{"id": "sofa"}, {"id": "bookcase", "gap": True}], "pool": {"sofa": [SOFA]}}


async def select(state, ctx):
    await ctx.generate(Plan, "select")
    return {"selection": {"sofa_1": "a1"}, "selection_validation": {"valid": True, "errors": []}}


async def place(state, ctx):
    await ctx.generate(Plan, "place")
    return {"layout": {"sofa_1": {}}, "findings": [{"level": "P0"}], "blocking_findings": [{"level": "P0"}]}


async def correct(state, ctx):
    await ctx.generate(Plan, "correct")
    return {"findings": [], "blocking_findings": []}


async def validate(state, ctx):
    if state["blocking_findings"]:
        return {"validation_errors": ["blocking findings remain"]}
    return {
        "render_manifest": contracts.render_manifest(state["shared"]["request"], [INSTANCE]),
        "selected_assets": [contracts.selected_asset(INSTANCE)],
        "total_cost": 1200.0,
    }


STAGES = Stages(
    interpret=interpret,
    room=room,
    retrieve=retrieve,
    select=select,
    place=place,
    correct=correct,
    validate=validate,
    direction=lambda index, room_type: f"direction {index}",
)


# Fields the web app reads (step 1 contract check), plus today's legacy extras.
VARIANT_KEYS = {
    "variant_index",
    "variant_id",
    "committed",
    "render_manifest",
    "selected_assets",
    "total_cost",
    "selection_validation",
    "asset_selection_failed",
}
SELECTED_ASSET_KEYS = {
    "center",
    "frontView",
    "topdown_url",
    "mount_type",
    "features",
    "uid",
    "instance_key",
    "asset_id",
    "name",
    "category",
    "image_url",
    "model_url",
    "width",
    "depth",
    "height",
    "price",
    "is_decor_item",
    "is_placeholder",
}
MANIFEST_KEYS = {"room_area", "room_vertices", "room_doors", "room_windows", "wall_height", "layout", "assets"}
LAYOUT_KEYS = {"uid", "instance_key", "category", "position", "rotation"}
ASSET_KEYS = {
    "center",
    "frontView",
    "topdown_url",
    "mount_type",
    "features",
    "instance_key",
    "asset_id",
    "uid",
    "name",
    "category",
    "image_url",
    "glb_url",
    "width",
    "depth",
    "height",
    "placement_mode",
    "is_decor_item",
    "is_placeholder",
}


def parse(body: str) -> list[dict]:
    return [json.loads(frame.removeprefix("data: ")) for frame in body.split("\n\n") if frame.strip()]


def post(tmp_path: Path, body: dict = REQUEST, *, stages: Stages = STAGES, **options):
    """Send one request and return (status, events, run record)."""
    app = create_app(stages=stages, model=GeminiModel(FakeGenai()), connect=lambda: None, runs_dir=tmp_path, **options)

    async def main() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/pipeline", json=body)

    response = asyncio.run(main())
    if response.status_code != 200:
        return response.status_code, [], None
    [record] = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    return response.status_code, parse(response.text), record


def of_type(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def test_streams_the_full_event_sequence(tmp_path):
    status, events, record = post(tmp_path)

    assert status == 200
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "complete"
    assert "fit_confirmation_required" not in {event["type"] for event in events}

    shared = [(e["type"], e["node"]) for e in events if e["type"].startswith("node_") and e["variant_index"] is None]
    assert shared == [
        ("node_start", "interpret"),
        ("node_complete", "interpret"),
        ("node_start", "extract_room"),
        ("node_complete", "extract_room"),
        ("node_start", "rag_scope_assets"),
        ("node_complete", "rag_scope_assets"),
    ]
    for index in range(3):
        nodes = [e["node"] for e in events if e["type"] == "node_complete" and e["variant_index"] == index]
        assert nodes == ["select_asset_intent", "layout_initial", "layout_fix", "render_scene"]
    assert all(isinstance(e["elapsed"], float) for e in of_type(events, "node_complete"))

    ready = of_type(events, "variant_ready")
    assert sorted(e["data"]["variant"]["variant_index"] for e in ready) == [0, 1, 2]
    for event in ready:
        assert set(event["data"]) == {"variant"}
        variant = event["data"]["variant"]
        assert variant["committed"] is False
        assert variant["asset_selection_failed"] is False
        assert variant["total_cost"] == 1200.0
        assert set(variant) == VARIANT_KEYS
        assert set(variant["selected_assets"][0]) == SELECTED_ASSET_KEYS
        manifest = variant["render_manifest"]
        assert set(manifest) == MANIFEST_KEYS
        assert set(manifest["layout"]["sofa_1"]) == LAYOUT_KEYS
        assert set(manifest["assets"]["sofa_1"]) == ASSET_KEYS
        assert manifest["assets"]["sofa_1"]["glb_url"] == "https://example.com/sofa.glb"
        for asset in (manifest["assets"]["sofa_1"], variant["selected_assets"][0]):
            assert asset["center"] == SOFA["center"]
            assert asset["frontView"] == 0
            assert asset["topdown_url"] == SOFA["topdown_url"]
            assert asset["mount_type"] == "freestanding"
            assert asset["features"] == ["removable cushions"]
    assert events[-1]["data"]["variants"] == [e["data"]["variant"] for e in ready]
    assert {v["variant_index"]: v["variant_id"] for v in events[-1]["data"]["variants"]} == {
        0: record["run_id"],
        1: f"{record['run_id']}_v1",
        2: f"{record['run_id']}_v2",
    }
    assert record["status"] == "complete"


def test_unknown_model_metadata_remains_unknown():
    asset = {
        **SOFA,
        "source_table": "pipeline.decor_items",
        "center": None,
        "front_view": None,
        "topdown_url": None,
        "mount_type": None,
        "features": [],
    }
    instance = {**INSTANCE, "asset": asset}
    manifest = contracts.render_manifest(contracts.PipelineRequest(**REQUEST), [instance])
    for entry in (manifest["assets"]["sofa_1"], contracts.selected_asset(instance)):
        assert entry["center"] is None
        assert entry["frontView"] is None
        assert entry["topdown_url"] is None
        assert entry["mount_type"] is None
        assert entry["features"] == []


def test_run_record_lists_stages_model_calls_and_totals(tmp_path):
    _, _, record = post(tmp_path)

    stages = [(s["stage"], s["variant_index"]) for s in record["stages"]]
    assert stages.count(("interpret", None)) == 1
    for index in range(3):
        for stage in ("select", "place", "correct", "validate"):
            assert (stage, index) in stages
    assert all(s["outcome"] == "ok" and s["elapsed"] >= 0 for s in record["stages"])

    calls = record["model_calls"]
    assert len(calls) == 1 + 3 * 3
    assert {c["variant_index"] for c in calls} == {None, 0, 1, 2}
    for call in calls:
        assert call["input_tokens"] == 1000 and call["cached_input_tokens"] == 200
        assert call["thoughts_tokens"] == 50 and call["cost_usd"] > 0
    assert record["totals"]["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in calls))
    assert record["totals"]["first_ready_s"] <= record["totals"]["all_ready_s"] <= record["totals"]["full_run_s"]
    assert record["slots"] == [
        {"slot": "sofa", "candidates": 10, "gap": False, "note": None},
        {"slot": "bookcase", "candidates": 0, "gap": True, "note": None},
    ]
    assert [v["outcome"] for v in record["variants"]] == ["ready"] * 3


@pytest.mark.parametrize(
    "extra",
    [
        {"upload_to_supabase": True},
        {"billing_operation_id": "op_1"},
        {"usdz_path": "room.usdz"},
        {"split_splat_import_id": "split_1"},
        {"surprise": 1},
    ],
)
def test_request_rejects_fields_outside_the_geometry_contract(tmp_path, extra):
    status, _, _ = post(tmp_path, {**REQUEST, **extra})
    assert status == 422


def test_request_requires_room_geometry(tmp_path):
    body = {key: value for key, value in REQUEST.items() if key != "room_vertices"}
    status, _, _ = post(tmp_path, body)
    assert status == 422


def test_failed_variants_do_not_block_the_others(tmp_path):
    turns: dict[int, int] = {}

    async def flaky_select(state, ctx):
        index = state["variant_index"]
        turns[index] = turns.get(index, 0) + 1
        if index == 0:
            raise RuntimeError("selection crashed")
        if index == 1:
            return {"selection": {}, "selection_validation": {"valid": False, "errors": ["over budget"]}}
        return await select(state, ctx)

    _, events, record = post(tmp_path, stages=replace(STAGES, select=flaky_select))

    failed = {e["variant_index"]: e for e in of_type(events, "variant_failed")}
    assert failed[0]["reason"] == "variant_error"
    assert failed[1]["reason"] == "asset_selection_failed"
    assert failed[1]["errors"] == ["over budget"]
    assert turns[1] == MAX_SELECTION_TURNS
    [ready] = of_type(events, "variant_ready")
    assert ready["data"]["variant"]["variant_index"] == 2
    assert events[-1]["data"]["variants"] == [ready["data"]["variant"]]
    assert [(v["outcome"], v["reason"]) for v in record["variants"]] == [
        ("failed", "variant_error"),
        ("failed", "asset_selection_failed"),
        ("ready", None),
    ]


def test_correction_stops_after_the_proposal_limit(tmp_path):
    async def stuck_correct(state, ctx):
        await ctx.generate(Plan, "correct")
        return {"blocking_findings": [{"level": "P1"}]}

    _, events, record = post(tmp_path, stages=replace(STAGES, correct=stuck_correct))

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"layout_validation_failed"}
    assert len(failed) == 3
    corrections = [s for s in record["stages"] if s["stage"] == "correct" and s["variant_index"] == 0]
    assert len(corrections) == MAX_CORRECTION_PROPOSALS
    assert events[-1]["data"]["variants"] == []


def test_shared_stage_failure_sends_error(tmp_path):
    async def broken_retrieve(state, ctx):
        raise RuntimeError("database down")

    _, events, record = post(tmp_path, stages=replace(STAGES, retrieve=broken_retrieve))

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "STAGE_FAILED"
    assert not of_type(events, "variant_ready")
    assert record["status"] == "error"
    assert "database down" in record["error"]


def test_run_deadline_fails_unfinished_variants(tmp_path):
    async def slow_select(state, ctx):
        if state["variant_index"] > 0:
            await asyncio.sleep(30)
        return await select(state, ctx)

    started = time.monotonic()
    _, events, record = post(tmp_path, stages=replace(STAGES, select=slow_select), run_deadline_s=1.0)

    assert time.monotonic() - started < 5
    [ready] = of_type(events, "variant_ready")
    assert ready["data"]["variant"]["variant_index"] == 0
    timeouts = {e["variant_index"]: e["reason"] for e in of_type(events, "variant_failed")}
    assert timeouts == {1: "timeout", 2: "timeout"}
    assert events[-1]["data"]["variants"] == [ready["data"]["variant"]]
    assert record["status"] == "timeout"
    assert [v["outcome"] for v in record["variants"]] == ["ready", "failed", "failed"]


def test_run_deadline_before_any_variant_sends_error(tmp_path):
    async def slow_interpret(state, ctx):
        await asyncio.sleep(30)

    _, events, record = post(tmp_path, stages=replace(STAGES, interpret=slow_interpret), run_deadline_s=0.5)

    assert [e["type"] for e in events] == ["start", "node_start", "error"]
    assert record["status"] == "timeout"
    assert record["stages"][0]["outcome"] == "cancelled"


def test_heartbeat_after_quiet_period(tmp_path):
    async def slow_room(state, ctx):
        await asyncio.sleep(0.35)
        return await room(state, ctx)

    _, events, _ = post(tmp_path, stages=replace(STAGES, room=slow_room), heartbeat_s=0.1)

    kinds = [e["type"] for e in events]
    room_start = kinds.index("node_start", kinds.index("node_complete"))
    beats = [e for e in events[room_start : kinds.index("node_complete", room_start)] if e["type"] == "heartbeat"]
    assert beats
    assert all(beat["node"] == "extract_room" and beat["elapsed"] > 0 for beat in beats)


def test_client_disconnect_cancels_the_run(tmp_path):
    genai = FakeGenai(delays={"select": 30})
    app = create_app(stages=STAGES, model=GeminiModel(genai), connect=lambda: None, runs_dir=tmp_path)

    async def main() -> None:
        body = json.dumps(REQUEST).encode()
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict:
            if messages:
                return messages.pop(0)
            while genai.calls.count("select") < 3:
                await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            pass

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/pipeline",
            "raw_path": b"/pipeline",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        calls = len(genai.calls)
        await asyncio.sleep(0.2)
        assert len(genai.calls) == calls

    asyncio.run(main())

    [record] = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert record["status"] == "cancelled"
    assert genai.cancelled == 3
    selects = [s for s in record["stages"] if s["stage"] == "select"]
    assert [s["outcome"] for s in selects] == ["cancelled"] * 3
    assert not [s for s in record["stages"] if s["stage"] == "place"]
    assert [c["error"] for c in record["model_calls"] if c["stage"] == "select"] == ["cancelled"] * 3


def test_model_call_attempt_limit_fails_the_variant_with_a_reason(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})

    async def no_model_interpret(state, ctx):
        return {"intent": {}}

    model = GeminiModel(gemini_client("test-key", httpx.MockTransport(handler)))
    app = create_app(stages=replace(STAGES, interpret=no_model_interpret), model=model, connect=lambda: None, runs_dir=tmp_path)

    async def main() -> str:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return (await client.post("/pipeline", json=REQUEST)).text

    events = parse(asyncio.run(main()))

    failed = of_type(events, "variant_failed")
    assert {e["reason"] for e in failed} == {"model_call_failed"}
    [record] = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert [c["attempts"] for c in record["model_calls"]] == [3, 3, 3]
    assert all("503" in c["error"] for c in record["model_calls"])
