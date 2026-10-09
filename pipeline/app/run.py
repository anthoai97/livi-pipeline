"""Run context: stage timing, model usage, outcomes, and the JSON run record.

One RunContext exists per request. It is the LangGraph runtime context, so every
graph node reads it from `runtime.context`. Stage functions get a StageContext,
which ties model calls to the stage and variant that made them.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
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

RUNS_DIR = Path(__file__).resolve().parents[1] / ".data" / "runs"

Emit = Callable[[dict], None]
RunStatus = Literal["running", "complete", "error", "cancelled", "timeout"]
M = TypeVar("M", bound=BaseModel)


class ModelClient(Protocol):
    """The structured-output model that stages call through StageContext.generate.

    `client` is the underlying genai client; the retrieve stage uses it for query
    embeddings (`search_assets.embed_query`).
    """

    client: genai.Client

    async def generate(self, ctx: StageContext, schema: type[M], contents: str, *, system: str | None = None) -> M: ...


@dataclass(frozen=True)
class StageContext:
    """What a stage function gets besides its state.

    - `generate(schema, contents, system=...)`: one bounded structured model call,
      recorded under this stage and variant.
    - `run.connection`: the request's sync psycopg connection (dict rows) for
      `search_assets`. Run blocking calls with `asyncio.to_thread`.
    - `run.model.client`: the genai client, for query embeddings.
    - `run.record_slot(...)` and `run.note(...)`: run-record entries.
    """

    run: RunContext
    stage: str
    variant_index: int | None

    async def generate(self, schema: type[M], contents: str, *, system: str | None = None) -> M:
        return await self.run.model.generate(self, schema, contents, system=system)


@dataclass
class RunContext:
    """Per-request dependencies and run record. The LangGraph runtime context."""

    run_id: str
    request: PipelineRequest
    model: ModelClient
    variant_count: int
    connection: psycopg.Connection[dict[str, Any]] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _started: float = field(default_factory=time.monotonic)
    status: RunStatus = "running"
    error: str | None = None
    stages: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)
    variants: dict[int, dict] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)

    def elapsed(self) -> float:
        """Seconds since the run started."""
        return round(time.monotonic() - self._started, 3)

    @asynccontextmanager
    async def stage(self, name: str, variant_index: int | None, emit: Emit) -> AsyncIterator[StageContext]:
        """Time one stage, emit node_start and node_complete, and record the outcome."""
        entry: dict = {"stage": name, "variant_index": variant_index, "start": self.elapsed(), "elapsed": None, "outcome": "running"}
        self.stages.append(entry)
        emit(contracts.node_start(name, variant_index))
        try:
            yield StageContext(self, name, variant_index)
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
        emit(contracts.node_complete(name, variant_index, entry["elapsed"]))

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

    def record_slot(self, slot: str, candidates: int, gap: bool, note: str | None = None) -> None:
        self.slots.append({"slot": slot, "candidates": candidates, "gap": gap, "note": note})

    def record_variant(self, variant_index: int, outcome: Literal["ready", "failed"], reason: str | None = None) -> None:
        now = self.elapsed()
        self.variants[variant_index] = {
            "variant_index": variant_index,
            "outcome": outcome,
            "reason": reason,
            "ready_at": now if outcome == "ready" else None,
            "finished_at": now,
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
                "room_vertices": len(request.room_vertices),
                "room_doors": len(request.room_doors),
                "room_windows": len(request.room_windows),
            },
            "stages": self.stages,
            "model_calls": self.model_calls,
            "slots": self.slots,
            "variants": [self.variants[index] for index in sorted(self.variants)],
            "notes": self.notes,
            "totals": {
                "first_ready_s": ready[0] if ready else None,
                "all_ready_s": ready[-1] if len(ready) == self.variant_count else None,
                "full_run_s": self.elapsed(),
                "model_calls": len(self.model_calls),
                "input_tokens": sum(call["input_tokens"] for call in self.model_calls),
                "output_tokens": sum(call["output_tokens"] for call in self.model_calls),
                "cost_usd": round(sum(call["cost_usd"] or 0.0 for call in self.model_calls), 8),
                "unpriced_calls": sum(1 for call in self.model_calls if call["cost_usd"] is None),
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
