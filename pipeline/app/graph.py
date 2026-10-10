"""LangGraph topology, state types, and bounds.

    START -- interpret -- room -- retrieve -- rank -- Send x3 -- variant -- END

    rank deals each slot's candidates to the variants; each Send carries one pool.

    variant subgraph, one per Send:
      select (repeats until the selection passes, at most 4 turns in all; the last
         turn's selection is placed even when it fails)
      -- place (code solver; also returns the layouts that tie its best)
      -- finish (one model call picks a layout, reviews and adjusts it; the final
         check removes the items it fails on, then delivers the rest) -- END

Failed checks remove items instead of failing a variant. `variant` runs the
compiled subgraph and always returns a result: it catches every exception except
cancellation, because LangGraph drops all parallel branch updates when one branch
raises. Each variant emits variant_ready, or variant_failed for an exception,
through the stream writer as soon as it finishes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app import contracts, shared_stages, variant_stages
from app.contracts import PipelineRequest
from app.preview import png_bytes, render_plan
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.taxonomy import normalize_category
from app.run import ModelCallError, RunContext, StageContext, describe

MODEL_CALL_TIMEOUT_S = 60
MODEL_CALL_ATTEMPTS = 3
MODEL_HEDGE_AFTER_S = 8
JEV_CALL_TIMEOUT_S = 5
JEV_CALL_ATTEMPTS = 2
MAX_SELECTION_TURNS = 4
RUN_DEADLINE_S = 300
VARIANT_COUNT = 3

# JSON-like record whose shape the stage that writes it owns.
Record = dict[str, Any]


class VariantResult(TypedDict):
    """What a variant leaves in the parent state: its outcome and the event it emitted."""

    outcome: Literal["ready", "failed"]
    reason: str | None
    event: Record


def merge_variants(left: dict[int, VariantResult], right: dict[int, VariantResult]) -> dict[int, VariantResult]:
    return {**left, **right}


class PipelineState(TypedDict, total=False):
    """Parent state. Each shared stage returns a partial update of it."""

    request: PipelineRequest  # input
    intent: Record  # interpret
    room: Record  # room
    slots: list[Record]  # retrieve: planned slots, gaps marked
    pool: dict[str, list[Record]]  # retrieve: candidates per slot id
    pools: list[dict[str, list[Record]]]  # rank: each variant's candidates per slot id, by variant index
    variants: Annotated[dict[int, VariantResult], merge_variants]  # variant


class Shared(TypedDict):
    """Shared values every variant reads. Read-only: copy a record before changing it."""

    request: PipelineRequest
    intent: Record
    room: Record
    slots: list[Record]


class VariantInput(TypedDict):
    """The Send payload for one variant."""

    variant_index: int
    direction: str
    pool: dict[str, list[Record]]
    shared: Shared


class VariantState(TypedDict, total=False):
    """Variant subgraph state. Each variant stage returns a partial update of it.

    Stages may add keys for other variant-owned values; declare them here.
    """

    variant_index: int
    direction: str
    shared: Shared
    pool: dict[str, list[Record]]  # graph: this variant's candidates per slot id, dealt by rank
    selection: Record  # select: latest selection, gaps in selection["gaps"]
    selection_validation: Record  # select: legacy validator report for `selection`; "valid" or the last turn ends selection
    selection_turns: int  # graph: select calls so far
    fit_step: str | None  # select: None, "compact" (smaller products), or "capped" (capped counts)
    instances: list[Record]  # select, finish: instance records of `selection`, keyed by uid = instance_key
    layout: Record  # place, finish: best layout so far
    issues: Record  # place, finish: analyze_layout issues for `layout`
    findings: list[Record]  # place, finish: findings for `layout`
    blocking_findings: list[Record]  # place, finish: P0, P1, and critical P2 findings
    non_blocking_findings: list[Record]  # place, finish: the other P2 findings
    layout_options: list[Record]  # place: measured layouts that tie the best score, the solver's best first
    render_manifest: Record  # finish
    selected_assets: list[Record]  # finish
    total_cost: float  # finish


PipelineGraph = CompiledStateGraph[PipelineState, RunContext, Any, Any]
SharedStage = Callable[[PipelineState, StageContext], Awaitable[PipelineState]]
VariantStage = Callable[[VariantState, StageContext], Awaitable[VariantState]]


@dataclass(frozen=True)
class Stages:
    """Stage functions the graph runs. Tests replace any of them with fakes."""

    interpret: SharedStage = shared_stages.interpret
    room: SharedStage = shared_stages.room
    retrieve: SharedStage = shared_stages.retrieve
    rank: SharedStage = variant_stages.rank
    select: VariantStage = variant_stages.select
    place: VariantStage = variant_stages.place
    finish: VariantStage = variant_stages.finish
    direction: Callable[[int, str], str] = variant_stages.direction


def _stage_node(name: str, stage: Callable[[Any, StageContext], Awaitable[Any]], counter: str | None = None):
    """Wrap a stage: time it, emit node events, and count loop iterations in `counter`."""

    async def node(state: Mapping[str, Any], runtime: Runtime[RunContext]) -> Mapping[str, Any]:
        async with runtime.context.stage(name, state.get("variant_index"), runtime.stream_writer) as ctx:
            update = await stage(state, ctx)  # type: ignore[arg-type]
        if counter:
            update = {**update, counter: state.get(counter, 0) + 1}
        return update

    return node


def _after_select(state: VariantState) -> str:
    if state.get("selection_validation", {}).get("valid") or state.get("selection_turns", 0) >= MAX_SELECTION_TURNS:
        return "place"
    return "select"


def _variant_result(state: VariantState, run_id: str) -> VariantResult:
    variant = contracts.ready_variant(
        run_id,
        state["variant_index"],
        render_manifest=state["render_manifest"],
        selected_assets=state.get("selected_assets", []),
        total_cost=state.get("total_cost", 0.0),
        selection_validation=state["selection_validation"],
    )
    return {"outcome": "ready", "reason": None, "event": contracts.variant_ready(variant)}


def _variant_summary(state: VariantState) -> Record:
    """A ready variant's compact result for the run record: products and poses, no URLs."""
    layout = state["layout"]
    products = [
        {
            "instance_key": key,
            "uid": str(asset.get("asset_id") or ""),
            "title": asset.get("title"),
            "category": normalize_category(asset.get("category")),
            "price": asset.get("price"),
            "width_m": asset.get("width_m"),
            "depth_m": asset.get("depth_m"),
            "height_m": asset.get("height_m"),
            "colors": asset.get("colors") or [],
            "styles": asset.get("styles") or [],
            "materials": asset.get("materials") or [],
            "is_decor_item": asset.get("is_decor_item"),
            "mount_type": asset.get("mount_type"),
            "placement_mode": placement_mode_for_asset(asset),
        }
        for asset in state.get("instances", [])
        if (key := str(asset.get("instance_key") or asset.get("uid") or "")) in layout
    ]
    return {
        "direction": state.get("direction", "").strip(),
        "total_cost": state.get("total_cost", 0.0),
        "products": products,
        "layout": {
            key: {"position": pose.get("position"), "rotation": pose.get("rotation"), "on_top_of": pose.get("on_top_of")}
            for key, pose in layout.items()
        },
    }


def _failed(index: int, reason: str, message: str, errors: list[str]) -> VariantResult:
    return {"outcome": "failed", "reason": reason, "event": contracts.variant_failed(index, reason, message, errors)}


def build_graph(stages: Stages = Stages()) -> PipelineGraph:
    variant_builder = StateGraph(VariantState, context_schema=RunContext)
    variant_builder.add_node("select", _stage_node("select", stages.select, "selection_turns"))
    variant_builder.add_node("place", _stage_node("place", stages.place))
    variant_builder.add_node("finish", _stage_node("finish", stages.finish))
    variant_builder.add_edge(START, "select")
    variant_builder.add_conditional_edges("select", _after_select, ["select", "place"])
    variant_builder.add_edge("place", "finish")
    variant_builder.add_edge("finish", END)
    variant_graph: CompiledStateGraph[VariantState, RunContext, Any, Any] = variant_builder.compile(name="variant")

    def fan_out(state: PipelineState) -> list[Send]:
        shared: Shared = {
            "request": state["request"],
            "intent": state["intent"],
            "room": state["room"],
            "slots": state["slots"],
        }
        room_type = state["request"].room_type
        return [
            Send("variant", {"variant_index": index, "direction": stages.direction(index, room_type),
                             "pool": state["pools"][index], "shared": shared})
            for index in range(VARIANT_COUNT)
        ]

    async def variant(state: VariantInput, runtime: Runtime[RunContext]) -> PipelineState:
        index = state["variant_index"]
        run = runtime.context
        findings: list[str] = []
        summary = None
        try:
            start: VariantState = {"variant_index": index, "direction": state["direction"], "pool": state["pool"],
                                   "shared": state["shared"]}
            final = cast(VariantState, await variant_graph.ainvoke(start))
            result = _variant_result(final, run.run_id)
            findings = [finding["issue"] for finding in final.get("non_blocking_findings", [])]
            summary = _variant_summary(final)
            plan = {**summary, "variant_index": index}
            preview = {"variant_index": index, "elapsed": None, "error": None}
            run.previews.append(preview)
            started = time.monotonic()

            def render() -> None:
                directory = run.runs_dir / run.run_id
                directory.mkdir(parents=True, exist_ok=True)
                image = render_plan({"request": run.request.model_dump()}, plan)
                (directory / f"variant_{index}.png").write_bytes(png_bytes(image))

            try:
                await asyncio.to_thread(render)
            except asyncio.CancelledError:
                preview["error"] = "cancelled"
                raise
            except Exception as exc:
                preview["error"] = describe(exc)
            else:
                result["event"]["data"]["variant"]["preview_url"] = f"/runs/{run.run_id}/previews/{index}.png"
            finally:
                preview["elapsed"] = round(time.monotonic() - started, 3)
        except Exception as exc:  # CancelledError is not an Exception, so cancellation still propagates.
            reason = "model_call_failed" if isinstance(exc, ModelCallError) else "variant_error"
            result = _failed(index, reason, describe(exc), [describe(exc)])
        run.record_variant(index, result["outcome"], result["reason"], findings, summary)
        runtime.stream_writer(result["event"])
        return {"variants": {index: result}}

    builder = StateGraph(PipelineState, context_schema=RunContext)
    builder.add_node("interpret", _stage_node("interpret", stages.interpret))
    builder.add_node("room", _stage_node("room", stages.room))
    builder.add_node("retrieve", _stage_node("retrieve", stages.retrieve))
    builder.add_node("rank", _stage_node("rank", stages.rank))
    builder.add_node("variant", variant)
    builder.add_edge(START, "interpret")
    builder.add_edge("interpret", "room")
    builder.add_edge("room", "retrieve")
    builder.add_edge("retrieve", "rank")
    builder.add_conditional_edges("rank", fan_out, ["variant"])
    builder.add_edge("variant", END)
    return builder.compile(name="pipeline")
