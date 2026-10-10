"""Run context: stage timing, model and Jev usage, outcomes, and the JSON run record.

One RunContext exists per request. It is the LangGraph runtime context, so every
graph node reads it from `runtime.context`. Stage functions get a StageContext,
which ties model and Jev calls to the stage and variant that made them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

from app import contracts

if TYPE_CHECKING:
    import psycopg
    from google import genai

    from app.contracts import PipelineRequest
    from app.jev import Jev

RUNS_DIR = Path(__file__).resolve().parents[1] / ".data" / "runs"

Emit = Callable[[dict], None]
RunStatus = Literal["running", "complete", "error", "cancelled", "timeout"]
M = TypeVar("M", bound=BaseModel)


class ModelClient(Protocol):
    """The structured-output model that stages call through StageContext.generate.

    `client` is the underlying genai client; the retrieve stage uses it for query
    embeddings (`search_assets.embed_query`). `stages` maps a stage, or a model key
    such as `correct_escalate`, to its model (LLM_STAGE_MODELS).
    """

    client: genai.Client
    stages: Mapping[str, Any]

    async def generate(
        self, ctx: StageContext, schema: type[M], contents: str | list[str | bytes], *, system: str | None = None,
        model_key: str | None = None
    ) -> M: ...


@dataclass(frozen=True)
class StageContext:
    """What a stage function gets besides its state.

    - `generate(schema, contents, system=..., model_key=...)`: one bounded
      structured model call, recorded under this stage and variant. `contents`
      is a prompt, or a list of text parts and PNG image bytes. `model_key`
      picks the model by that key instead of the stage name.
    - `run.connection`: the request's sync psycopg connection (dict rows) for
      `search_assets`. Run blocking calls with `asyncio.to_thread`.
    - `run.model.client`: the genai client, for query embeddings.
    - `ask(use, state, questions)`: one Jev request of yes/no questions, recorded
      under this stage and variant. Returns each question's yes probability by
      name, or None when the call fails; the failure is noted, and the caller
      falls back to its Jev-off behavior. `run.jev_uses` decides which uses
      run; `run.product_reuse_rate` caps how much of a selection other
      variants may share.
    - `run.record_slot(...)` and `run.note(...)`: run-record entries.
    - `data`: display facts sent with this stage's `node_complete` event.
    """

    run: RunContext
    stage: str
    variant_index: int | None
    data: dict = field(default_factory=dict)

    async def generate(self, schema: type[M], contents: str | list[str | bytes], *, system: str | None = None,
                       model_key: str | None = None) -> M:
        return await self.run.model.generate(self, schema, contents, system=system, model_key=model_key)

    async def ask(self, use: str, state: Any, questions: dict[str, str]) -> dict[str, float] | None:
        if self.run.jev is None:
            self.run.note(f"jev {use} skipped: no Jev client", self.variant_index)
            return None
        return await self.run.jev.ask(self, use, state, questions)


@dataclass
class RunContext:
    """Per-request dependencies and run record. The LangGraph runtime context."""

    run_id: str
    request: PipelineRequest
    model: ModelClient
    variant_count: int
    jev: Jev | None = None
    jev_uses: frozenset[str] = frozenset()  # JEV_USES: "rank", "check"
    product_reuse_rate: float = 0.5  # PRODUCT_REUSE_RATE
    connection: psycopg.Connection[dict[str, Any]] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _started: float = field(default_factory=time.monotonic)
    status: RunStatus = "running"
    error: str | None = None
    stages: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    jev_calls: list[dict] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)
    variants: dict[int, dict] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    previews: list[dict] = field(default_factory=list)
    client_timing: list[dict] = field(default_factory=list)
    runs_dir: Path = RUNS_DIR

    def elapsed(self) -> float:
        """Seconds since the run started."""
        return round(time.monotonic() - self._started, 3)

    @asynccontextmanager
    async def stage(self, name: str, variant_index: int | None, emit: Emit) -> AsyncIterator[StageContext]:
        """Time one stage, emit node_start and node_complete, and record the outcome."""
        entry: dict = {"stage": name, "variant_index": variant_index, "start": self.elapsed(), "elapsed": None, "outcome": "running"}
        self.stages.append(entry)
        emit(contracts.node_start(name, variant_index))
        ctx = StageContext(self, name, variant_index)
        try:
            yield ctx
        except asyncio.CancelledError:
            entry["outcome"] = "cancelled"
            raise
        except Exception as exc:
            entry["outcome"] = "error"
            entry["error"] = describe(exc)
            raise
        else:
            entry["outcome"] = "ok"
        finally:
            entry["elapsed"] = round(self.elapsed() - entry["start"], 3)
        emit(contracts.node_complete(name, variant_index, entry["elapsed"], ctx.data))

    def record_model_call(
        self,
        ctx: StageContext,
        *,
        model: str,
        counts: dict[str, int],
        cost_usd: float | None,
        elapsed: float,
        attempts: int,
        error: str | None,
    ) -> None:
        self.model_calls.append(
            {
                "stage": ctx.stage,
                "variant_index": ctx.variant_index,
                "model": model,
                **counts,
                "cost_usd": cost_usd,
                "elapsed": elapsed,
                "attempts": attempts,
                "error": error,
            }
        )

    def record_jev_call(
        self,
        ctx: StageContext,
        *,
        use: str,
        model: str,
        questions: int,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        elapsed: float,
        error: str | None,
    ) -> None:
        self.jev_calls.append(
            {
                "stage": ctx.stage,
                "variant_index": ctx.variant_index,
                "use": use,
                "model": model,
                "questions": questions,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "elapsed": elapsed,
                "error": error,
            }
        )

    def record_slot(self, slot: str, candidates: int, gap: bool, note: str | None = None) -> None:
        self.slots.append({"slot": slot, "candidates": candidates, "gap": gap, "note": note})

    def record_variant(
        self,
        variant_index: int,
        outcome: Literal["ready", "failed"],
        reason: str | None = None,
        non_blocking_findings: list[str] | None = None,
        result: dict | None = None,
    ) -> None:
        """`non_blocking_findings` are the issue keys of the noncritical P2 findings at the final layout.

        `result` is a ready variant's compact result for benchmark review: `direction`,
        `total_cost`, `products`, and `layout` (app.graph `_variant_summary`).
        """
        now = self.elapsed()
        self.variants[variant_index] = {
            "variant_index": variant_index,
            "outcome": outcome,
            "reason": reason,
            "ready_at": now if outcome == "ready" else None,
            "finished_at": now,
            "non_blocking_findings": non_blocking_findings or [],
            **(result or {}),
        }

    def note(self, text: str, variant_index: int | None = None) -> None:
        """Record a decision worth comparing across runs, such as the fit step applied."""
        self.notes.append({"variant_index": variant_index, "at": self.elapsed(), "text": text})

    def finish(self, status: RunStatus, error: str | None = None) -> None:
        if self.status == "running":
            self.status = status
            self.error = error

    def record(self) -> dict:
        ready = sorted(v["ready_at"] for v in self.variants.values() if v["outcome"] == "ready")
        jev_cost = sum(call["cost_usd"] or 0.0 for call in self.jev_calls)
        request = self.request
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "status": self.status,
            "error": self.error,
            "request": {
                "user_intent": request.user_intent,
                "budget": request.budget,
                "room_type": request.room_type,
                "room_area": request.room_area,
                "wall_height": request.wall_height,
                "room_vertices": [list(vertex) for vertex in request.room_vertices],
                "room_doors": request.room_doors,
                "room_windows": request.room_windows,
            },
            "switches": {
                "JEV_USES": ",".join(sorted(self.jev_uses)),
                "PRODUCT_REUSE_RATE": self.product_reuse_rate,
            },
            "stages": self.stages,
            "model_calls": self.model_calls,
            "jev_calls": self.jev_calls,
            "slots": self.slots,
            "variants": [self.variants[index] for index in sorted(self.variants)],
            "notes": self.notes,
            "previews": self.previews,
            "client_timing": self.client_timing,
            "totals": {
                "first_ready_s": ready[0] if ready else None,
                "all_ready_s": ready[-1] if len(ready) == self.variant_count else None,
                "full_run_s": self.elapsed(),
                "model_calls": len(self.model_calls),
                "input_tokens": sum(call["input_tokens"] for call in self.model_calls),
                "output_tokens": sum(call["output_tokens"] for call in self.model_calls),
                # Model and Jev calls.
                "cost_usd": round(sum(call["cost_usd"] or 0.0 for call in self.model_calls) + jev_cost, 8),
                "unpriced_calls": sum(1 for call in self.model_calls if call["cost_usd"] is None),
                "jev_calls": len(self.jev_calls),
                "jev_cost_usd": round(jev_cost, 8),
            },
        }

    def write(self, runs_dir: Path = RUNS_DIR) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.record(), indent=2, default=json_default) + "\n", encoding="utf-8")
        return path


class ModelCallError(RuntimeError):
    """A model call failed after its attempts, or returned output that does not match the schema."""


def describe(exc: BaseException) -> str:
    name = type(exc).__name__
    return (f"{name}: {exc}" if str(exc) else name)[:500]


def json_default(value: object) -> object:
    """JSON fallback for database values: Decimal prices become numbers."""
    if isinstance(value, Decimal):
        return float(value)
    return str(value)
