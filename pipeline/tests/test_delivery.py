"""Browser delivery through real stages and HTTP routes, with external calls faked."""

import asyncio
import io
import json
import threading
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from app import graph
from app.jev import Jev
from app.llm import GeminiModel
from app.main import create_app
from test_runtime import REQUEST, STAGES, FakeGenai, FakeJevClient, of_type, parse, select
from test_stages import (
    FIXED, OVERLAPPING, PLACEMENTS, POOL, FakeGenai as StageGenai,
    intent_packet, run_pipeline, search_pool, selection, solve_as,
)

TIMING = {"variant_index": 0, "received_ms": 100, "loaded_ms": 200.5, "displayed_ms": 250,
          "models": 7, "failed_models": 1, "screen": "generate"}


def test_real_stage_data_and_preview_delivery(tmp_path, monkeypatch):
    search_pool(monkeypatch, POOL)
    solve_as(monkeypatch, OVERLAPPING)
    main_thread = threading.get_ident()
    render = graph.render_plan

    def in_worker(record, variant):
        assert threading.get_ident() != main_thread
        return render(record, variant)

    monkeypatch.setattr(graph, "render_plan", in_worker)
    events, record = run_pipeline(tmp_path, StageGenai({
        "IntentPacket": intent_packet(), "Selection": selection(), "Correction": FIXED,
    }))
    completed = of_type(events, "node_complete")
    expected = {
        "interpret": {"style_hints", "requested_categories"},
        "extract_room": {"room_area", "wall_height", "doors", "windows", "floor_area_sqm", "usable_area_sqm",
                         "protected_paths", "fit", "fit_message"},
        "rag_scope_assets": {"slots", "candidates", "gaps", "preview"},
        "layout_initial": {"placed", "findings", "blocking", "swaps", "drops"},
        "layout_fix": {"placed", "findings", "blocking", "improved"},
        "render_scene": {"valid", "errors"},
    }
    for event in completed:
        keys = expected.get(event["node"])
        if event["node"] == "select_asset_intent":
            keys = ({"ranked_slots", "shared_products"} if event["variant_index"] is None else
                    {"turn", "valid", "errors", "fit_step", "total_cost", "items"})
        assert keys <= event["data"].keys()
    assert len([event for event in completed if event["node"] == "layout_fix"]) == 6
    retrieval = next(event["data"] for event in completed if event["node"] == "rag_scope_assets")
    assert retrieval["candidates"] == sum(slot["candidates"] for slot in record["slots"])
    assert len(retrieval["preview"]) <= retrieval["slots"]
    ready = [event["data"]["variant"] for event in of_type(events, "variant_ready")]
    assert len(ready) == 3
    assert all(preview["error"] is None and preview["elapsed"] >= 0 for preview in record["previews"])
    for event in completed:
        data = event["data"]
        if "items" in data:
            assert data["total_cost"] == ready[0]["total_cost"]
            assert len(data["items"]) <= retrieval["slots"]
        if event["node"] == "render_scene":
            assert data == {"valid": True, "errors": 0}
    app = create_app(runs_dir=tmp_path)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for variant in ready:
                url = f"/runs/{record['run_id']}/previews/{variant['variant_index']}.png"
                assert variant["preview_url"] == url
                response = await client.get(url)
                assert response.status_code == 200 and response.headers["content-type"] == "image/png"
                image = Image.open(io.BytesIO(response.content))
                image.verify()
                assert image.width == 900

    asyncio.run(fetch())


@pytest.mark.parametrize("failure", ["render", "write"])
def test_preview_failure_keeps_valid_variants_ready(tmp_path, monkeypatch, failure):
    search_pool(monkeypatch, POOL)
    solve_as(monkeypatch, PLACEMENTS)
    if failure == "render":
        def broken(record, variant):
            raise RuntimeError("renderer unavailable")
        monkeypatch.setattr(graph, "render_plan", broken)
    else:
        from pathlib import Path
        write_bytes = Path.write_bytes

        def no_space(path, data):
            if path.suffix == ".png":
                raise OSError("disk full")
            return write_bytes(path, data)
        monkeypatch.setattr(Path, "write_bytes", no_space)
    events, record = run_pipeline(tmp_path, StageGenai({"IntentPacket": intent_packet(), "Selection": selection()}))
    ready = of_type(events, "variant_ready")
    assert len(ready) == 3 and all(event["data"]["variant"]["preview_url"] is None for event in ready)
    assert [variant["outcome"] for variant in record["variants"]] == ["ready"] * 3
    assert len(record["previews"]) == 3
    assert all(preview["error"] and preview["elapsed"] >= 0 for preview in record["previews"])


@pytest.mark.parametrize("run_id,index", [
    ("bad", "0"), ("g" * 32, "0"), ("a" * 31, "0"), ("a" * 32, "-1"),
    ("a" * 32, "3"), ("a" * 32, "00"), ("a" * 32, "1.0"), ("a" * 32, "two"),
    ("a" * 32, "0"),
])
def test_preview_route_rejects_invalid_or_missing_paths(tmp_path, run_id, index):
    async def request():
        app = create_app(runs_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(f"/runs/{run_id}/previews/{index}.png")).status_code == 404
    asyncio.run(request())


@pytest.mark.parametrize("changes", [
    {"surprise": 1}, {"variant_index": -1}, {"variant_index": 3}, {"variant_index": True},
    {"received_ms": -1}, {"received_ms": "100"}, {"loaded_ms": 86_400_001},
    {"displayed_ms": 100}, {"models": 10_001}, {"models": 1.5}, {"failed_models": 8},
    {"failed_models": -1}, {"screen": "unknown"}, {"received_ms": "NaN"}, {"displayed_ms": "Infinity"},
])
def test_timing_rejects_invalid_bodies(tmp_path, changes):
    async def request():
        app = create_app(runs_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/runs/{'a' * 32}/client-timing", json={**TIMING, **changes})
            assert response.status_code == 422
    asyncio.run(request())


def test_timing_unknown_run_is_not_created(tmp_path):
    async def request():
        app = create_app(runs_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for run_id in ("a" * 32, "bad"):
                assert (await client.post(f"/runs/{run_id}/client-timing", json=TIMING)).status_code == 404
        assert not list(tmp_path.iterdir())
    asyncio.run(request())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_timing_rejects_nonfinite_json_numbers(tmp_path, value):
    async def request():
        app = create_app(runs_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/runs/{'a' * 32}/client-timing", content=json.dumps({**TIMING, "received_ms": value}),
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 422
    asyncio.run(request())


@pytest.mark.parametrize("cancel", [False, True])
def test_timing_survives_completion_or_disconnect_with_ready_variant(tmp_path, cancel):
    async def request():
        disconnect = asyncio.Event()
        run_id = None
        pending_posts = []

        async def slow_select(state, ctx):
            if cancel and state["variant_index"] > 0:
                await asyncio.Event().wait()
            return await select(state, ctx)

        app = create_app(stages=replace(STAGES, select=slow_select), model=GeminiModel(FakeGenai()),
                         jev=Jev(FakeJevClient()), connect=lambda: None, runs_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            messages = [{"type": "http.request", "body": json.dumps(REQUEST).encode(), "more_body": False}]

            async def receive():
                if messages:
                    return messages.pop()
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def post_timing(screen):
                response = await client.post(f"/runs/{run_id}/client-timing", json={**TIMING, "screen": screen})
                assert response.status_code == 204

            async def send(message):
                nonlocal run_id
                if message["type"] != "http.response.body" or not message.get("body"):
                    return
                for event in parse(message["body"].decode()):
                    if event["type"] == "start":
                        run_id = event["run_id"]
                    elif event["type"] == "variant_ready" and event["data"]["variant"]["variant_index"] == 0:
                        await post_timing("generate")
                        assert not (tmp_path / f"{run_id}.json").exists()
                        if cancel:
                            pending_posts.append(asyncio.create_task(post_timing("chooser")))
                            disconnect.set()
                    elif event["type"] == "complete":
                        pending_posts.append(asyncio.create_task(post_timing("chooser")))

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                     "scheme": "http", "path": "/pipeline", "raw_path": b"/pipeline", "query_string": b"",
                     "root_path": "", "headers": [(b"content-type", b"application/json")],
                     "client": ("test", 1), "server": ("test", 80)}
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
            await asyncio.gather(*pending_posts)
            await asyncio.gather(*(post_timing("studio") for _ in range(5)))
        record = json.loads((tmp_path / f"{run_id}.json").read_text())
        assert record["status"] == ("cancelled" if cancel else "complete")
        assert record["variants"][0]["outcome"] == "ready"
        assert len(record["client_timing"]) == 7
        assert [entry["screen"] for entry in record["client_timing"]] == ["generate", "chooser", *["studio"] * 5]
        if cancel:
            assert any(stage["outcome"] == "cancelled" for stage in record["stages"])

    asyncio.run(request())
