"""FastAPI entry point: POST /pipeline streams the run as server-sent events."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from functools import cache
from pathlib import Path
from typing import Any

import anyio
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from psycopg.rows import dict_row

from app import contracts
from app.contracts import PipelineRequest
from app.graph import RUN_DEADLINE_S, VARIANT_COUNT, PipelineGraph, Stages, build_graph
from app.jev import Jev, jev_client, switches
from app.llm import GeminiModel, gemini_client
from app.run import RUNS_DIR, ModelClient, RunContext, describe, json_default

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

HEARTBEAT_S = 10

Connect = Callable[[], psycopg.Connection[dict[str, Any]] | None]


@cache
def _default_model() -> GeminiModel:
    return GeminiModel(gemini_client(os.environ["GEMINI_API_KEY"]))


@cache
def _default_jev() -> Jev:
    return Jev(jev_client(os.environ["JEV_API_KEY"]))


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.Connection.connect(os.environ["LOCAL_CONNECTION_STRING"], row_factory=dict_row)


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, default=json_default)}\n\n"


async def _stream_run(
    graph: PipelineGraph,
    run: RunContext,
    connect: Connect,
    run_deadline_s: float,
    heartbeat_s: float,
    runs_dir: Path,
    active_runs: dict[str, RunContext],
    record_lock: asyncio.Lock,
) -> AsyncIterator[str]:
    """Run the graph and turn its custom stream into SSE frames.

    Adds heartbeats, enforces the run deadline, cancels the graph when the client
    disconnects, and writes the run record in every outcome.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + run_deadline_s
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    task: asyncio.Task[None] | None = None
    ready: list[dict] = []
    finished: set[int] = set()
    variant_started = False
    current: dict = {}

    async def pump() -> None:
        try:
            async for part in graph.astream(
                {"request": run.request},
                context=run,
                stream_mode="custom",
                subgraphs=True,
                version="v2",
            ):
                if part["type"] == "custom":
                    queue.put_nowait(part["data"])
        finally:
            queue.put_nowait(None)

    async def stop_graph() -> None:
        if task is not None and not task.done():
            task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(task, return_exceptions=True)

    def forward(event: dict) -> str:
        nonlocal variant_started, current
        if event["type"] == "node_start":
            current = event
            variant_started = variant_started or event["variant_index"] is not None
        elif event["type"] == "variant_ready":
            ready.append(event["data"]["variant"])
            finished.add(event["data"]["variant"]["variant_index"])
        elif event["type"] == "variant_failed":
            finished.add(event["variant_index"])
        return _frame(event)

    try:
        yield _frame(contracts.start_event(run.run_id))
        run.connection = await asyncio.to_thread(connect)
        task = asyncio.create_task(pump())
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(queue.get(), min(heartbeat_s, remaining))
            except TimeoutError:
                if loop.time() < deadline:
                    yield _frame(contracts.heartbeat_event(current.get("node"), current.get("variant_index"), run.elapsed()))
                continue
            if event is None:
                break
            yield forward(event)

        if not task.done():
            # Run deadline: stop the graph, send what already finished, fail the rest.
            await stop_graph()
            while not queue.empty():
                if (event := queue.get_nowait()) is not None:
                    yield forward(event)
            if not variant_started:
                run.finish("timeout", "run deadline passed before any variant started")
                yield _frame(contracts.error_event("The run deadline passed before any variant started.", "RUN_DEADLINE"))
                return
            for index in range(VARIANT_COUNT):
                if index not in finished:
                    run.record_variant(index, "failed", "timeout")
                    yield _frame(contracts.variant_failed(index, "timeout", "The run deadline passed.", []))
            run.finish("timeout")
            yield _frame(contracts.complete_event(run.run_id, ready))
            return

        if (exc := task.exception()) is not None:
            run.finish("error", describe(exc))
            stage = next((s["stage"] for s in run.stages if s["outcome"] == "error"), "pipeline")
            yield _frame(contracts.error_event(f"The {stage} stage failed.", "STAGE_FAILED", type(exc).__name__))
            return
        run.finish("complete")
        yield _frame(contracts.complete_event(run.run_id, ready))
    except (asyncio.CancelledError, GeneratorExit):
        run.finish("cancelled", "client disconnected")
        raise
    except Exception as exc:
        run.finish("error", describe(exc))
        yield _frame(contracts.error_event("The run failed to start.", "RUN_FAILED", type(exc).__name__))
    finally:
        with anyio.CancelScope(shield=True):
            await stop_graph()
            try:
                if run.connection is not None:
                    run.connection.close()
            finally:
                async with record_lock:
                    try:
                        run.write(runs_dir)
                    finally:
                        active_runs.pop(run.run_id, None)


def create_app(
    *,
    stages: Stages | None = None,
    model: ModelClient | None = None,
    jev: Jev | None = None,
    connect: Connect = _connect,
    run_deadline_s: float = RUN_DEADLINE_S,
    heartbeat_s: float = HEARTBEAT_S,
    runs_dir: Path = RUNS_DIR,
) -> FastAPI:
    """Build the app. Tests inject fake stages, a fake model, a fake Jev client, and no database."""
    graph = build_graph(stages or Stages())
    app = FastAPI(title="Livinit pipeline")
    active_runs: dict[str, RunContext] = {}
    record_lock = asyncio.Lock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Python's JSON reader accepts NaN/Infinity. Keep rejected inputs JSON-safe
        # so the validation response is 422 instead of failing during serialization.
        errors = jsonable_encoder(exc.errors(), custom_encoder={float: lambda value: value if math.isfinite(value) else str(value)})
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.post("/pipeline")
    async def pipeline(request: PipelineRequest) -> StreamingResponse:
        run = RunContext(uuid.uuid4().hex, request, model or _default_model(), VARIANT_COUNT,
                         jev or _default_jev(), runs_dir=runs_dir, **switches())
        active_runs[run.run_id] = run
        return StreamingResponse(
            _stream_run(graph, run, connect, run_deadline_s, heartbeat_s, runs_dir, active_runs, record_lock),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{run_id}/previews/{index}.png")
    async def preview(run_id: str, index: str) -> FileResponse:
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None or index not in {"0", "1", "2"}:
            raise HTTPException(status_code=404, detail="Preview not found")
        path = runs_dir / run_id / f"variant_{index}.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Preview not found")
        return FileResponse(path, media_type="image/png")

    @app.post("/runs/{run_id}/client-timing", status_code=204)
    async def client_timing(run_id: str, timing: contracts.ClientTiming) -> Response:
        if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        async with record_lock:
            if run := active_runs.get(run_id):
                run.client_timing.append(timing.model_dump())
            else:
                path = runs_dir / f"{run_id}.json"
                if not path.is_file():
                    raise HTTPException(status_code=404, detail="Run not found")
                record = json.loads(path.read_text(encoding="utf-8"))
                record.setdefault("client_timing", []).append(timing.model_dump())
                path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return Response(status_code=204)

    return app


app = create_app()
