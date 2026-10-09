"""LangGraph topology, state types, and bounds.

    START -- interpret -- room -- retrieve -- rank -- Send x3 -- variant -- END

    rank deals each slot's candidates to the variants; each Send carries one pool.

    variant subgraph, one per Send:
      select (repeats until the selection passes, at most 4 turns in all)
      -- place -- repair -- correct (repeats while blocking findings remain and each
         proposal improves, at most 3 proposals per layout)
      -- refine (when no blocking finding remains) -- validate
      validate fails -- reselect (once, while turns remain) -- select

`variant` runs the compiled subgraph and always returns a result: it catches
every exception except cancellation, because LangGraph drops all parallel branch
updates when one branch raises. Each variant emits variant_ready or
variant_failed through the stream writer as soon as it finishes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app import contracts, shared_stages, variant_stages
from app.contracts import PipelineRequest
from app.run import ModelCallError, RunContext, StageContext, describe

MODEL_CALL_TIMEOUT_S = 60
MODEL_CALL_ATTEMPTS = 3
MODEL_HEDGE_AFTER_S = 8
JEV_CALL_TIMEOUT_S = 5
JEV_CALL_ATTEMPTS = 2
MAX_SELECTION_TURNS = 4
MAX_CORRECTION_PROPOSALS = 3
MAX_RESELECTIONS = 1
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
    selection_validation: Record  # select: legacy validator report for `selection`; "valid" decides the next step
    selection_turns: int  # graph: select calls so far, across reselection
    fit_step: str | None  # select: None, "compact" (smaller products), or "capped" (capped counts)
    instances: list[Record]  # select: instance records of `selection`, keyed by uid = instance_key
    layout: Record  # place, repair, correct, refine: best layout so far
    issues: Record  # place, repair, correct, refine: analyze_layout issues for `layout`
    findings: list[Record]  # place, repair, correct, refine: findings for `layout`
    blocking_findings: list[Record]  # place, repair, correct, refine: P0, P1, and critical P2 findings; empty ends correction
    non_blocking_findings: list[Record]  # place, repair, correct, refine: the other P2 findings
    correction_proposals: int  # graph: correct calls so far for this layout
    correction_stalled: bool  # correct: the last proposal did not improve the score; ends correction
    correction_escalated: bool  # correct: later proposals for this layout use the correct_escalate model
    reselections: int  # graph: reselections so far
    placement_feedback: str  # select: the failed layout's items to replace, set on reselection
    validation_errors: list[str]  # validate: why the layout fails; empty when it passes
    render_manifest: Record  # validate: set when the layout passes
    selected_assets: list[Record]  # validate: set when the layout passes
    total_cost: float  # validate: set when the layout passes


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
    repair: VariantStage = variant_stages.repair
    correct: VariantStage = variant_stages.correct
    refine: VariantStage = variant_stages.refine
    validate: VariantStage = variant_stages.validate
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
    if state.get("selection_validation", {}).get("valid"):
        return "place"
    return "select" if state.get("selection_turns", 0) < MAX_SELECTION_TURNS else END


def _after_layout(state: VariantState) -> str:
    if not state.get("blocking_findings"):
        return "refine"
    if state.get("correction_proposals", 0) < MAX_CORRECTION_PROPOSALS and not state.get("correction_stalled"):
        return "correct"
    return "validate"


def _after_validate(state: VariantState) -> str:
    if (
        "render_manifest" in state
        or state.get("reselections", 0) >= MAX_RESELECTIONS
        or state.get("selection_turns", 0) >= MAX_SELECTION_TURNS
    ):
        return END
    return "reselect"


def _reselect(state: VariantState, runtime: Runtime[RunContext]) -> VariantState:
    """Start a reselection after a failed layout: the next layout gets its own correction budget."""
    errors = "; ".join(state.get("validation_errors", []))
    runtime.context.note(f"reselection after the layout failed: {errors[:300]}", state["variant_index"])
    return {"reselections": state.get("reselections", 0) + 1, "correction_proposals": 0, "correction_stalled": False,
            "correction_escalated": False}


def _variant_result(state: VariantState, run_id: str) -> VariantResult:
    index = state["variant_index"]
    validation = state.get("selection_validation", {})
    if not validation.get("valid"):
        errors = [str(error) for error in validation.get("errors", [])]
        message = f"No selection passed validation within {MAX_SELECTION_TURNS} turns."
        return _failed(index, "asset_selection_failed", message, errors)
    if "render_manifest" not in state:
        errors = state.get("validation_errors", [])
        message = "The final layout did not pass validation."
        return _failed(index, "layout_validation_failed", message, errors)
    variant = contracts.ready_variant(
        run_id,
        index,
        render_manifest=state["render_manifest"],
        selected_assets=state.get("selected_assets", []),
        total_cost=state.get("total_cost", 0.0),
        selection_validation=validation,
    )
    return {"outcome": "ready", "reason": None, "event": contracts.variant_ready(variant)}


def _failed(index: int, reason: str, message: str, errors: list[str]) -> VariantResult:
    return {"outcome": "failed", "reason": reason, "event": contracts.variant_failed(index, reason, message, errors)}


def build_graph(stages: Stages = Stages()) -> PipelineGraph:
    variant_builder = StateGraph(VariantState, context_schema=RunContext)
    variant_builder.add_node("select", _stage_node("select", stages.select, "selection_turns"))
    variant_builder.add_node("place", _stage_node("place", stages.place))
    variant_builder.add_node("repair", _stage_node("repair", stages.repair))
    variant_builder.add_node("correct", _stage_node("correct", stages.correct, "correction_proposals"))
    variant_builder.add_node("refine", _stage_node("refine", stages.refine))
    variant_builder.add_node("validate", _stage_node("validate", stages.validate))
    variant_builder.add_node("reselect", _reselect)
    variant_builder.add_edge(START, "select")
    variant_builder.add_conditional_edges("select", _after_select, ["select", "place", END])
    variant_builder.add_edge("place", "repair")
    variant_builder.add_conditional_edges("repair", _after_layout, ["correct", "refine", "validate"])
    variant_builder.add_conditional_edges("correct", _after_layout, ["correct", "refine", "validate"])
    variant_builder.add_edge("refine", "validate")
    variant_builder.add_conditional_edges("validate", _after_validate, ["reselect", END])
    variant_builder.add_edge("reselect", "select")
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
        try:
            start: VariantState = {"variant_index": index, "direction": state["direction"], "pool": state["pool"],
                                   "shared": state["shared"]}
            final = cast(VariantState, await variant_graph.ainvoke(start))
            result = _variant_result(final, run.run_id)
            findings = [finding["issue"] for finding in final.get("non_blocking_findings", [])]
        except Exception as exc:  # CancelledError is not an Exception, so cancellation still propagates.
            reason = "model_call_failed" if isinstance(exc, ModelCallError) else "variant_error"
            result = _failed(index, reason, describe(exc), [describe(exc)])
        run.record_variant(index, result["outcome"], result["reason"], findings)
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
