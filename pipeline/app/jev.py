"""Bounded Jev (typesafe-sdk) yes/no questions with per-call usage and cost records.

Jev answers each yes/no question ("noul") with a probability from 0 to 1. One
request reads the state once and answers all of its questions. A failed call
never fails a variant: `ask` returns None and notes the failure, and the caller
falls back to its Jev-off behavior.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, Literal, get_args

from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy

from app.graph import JEV_CALL_ATTEMPTS, JEV_CALL_TIMEOUT_S
from app.run import describe

if TYPE_CHECKING:
    from typesafe_sdk import SystemOneResponse

    from app.run import StageContext

# Pinned, so tuned thresholds do not move with the jev-latest alias.
DEFAULT_MODEL = "jev-1.13.0"
# Input tokens only; output tokens are free.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000
JEV_USES = ("rank", "check")
Refinement = Literal["off", "always", "jev"]


def jev_client(api_key: str) -> AsyncTypeSafeClient:
    """Jev client with the call bounds: 5 s per attempt, at most 2 attempts."""
    return AsyncTypeSafeClient(
        api_key=api_key,
        timeout=JEV_CALL_TIMEOUT_S,
        retry=RetryPolicy(max_retries=JEV_CALL_ATTEMPTS - 1, timeout=None),
    )


def switches() -> dict[str, Any]:
    """The run's options: JEV_USES (comma list of rank and check, default both),
    REFINEMENT (off, always, or jev, default off), and PRODUCT_REUSE_RATE (the
    largest share, 0 to 1, of a variant's distinct products that other variants
    may also use, default 0.5), as RunContext fields."""
    uses = frozenset(use.strip() for use in os.environ.get("JEV_USES", ",".join(JEV_USES)).split(",") if use.strip())
    refinement = os.environ.get("REFINEMENT", "off").strip()
    reuse = os.environ.get("PRODUCT_REUSE_RATE", "0.5").strip()
    if not uses <= set(JEV_USES):
        raise ValueError(f"JEV_USES may list only {', '.join(JEV_USES)}; got {sorted(uses)}")
    if refinement not in get_args(Refinement):
        raise ValueError(f"REFINEMENT must be one of {', '.join(get_args(Refinement))}; got {refinement!r}")
    try:
        reuse_rate = float(reuse)
    except ValueError:
        reuse_rate = -1.0
    if not 0 <= reuse_rate <= 1:
        raise ValueError(f"PRODUCT_REUSE_RATE must be a number from 0 to 1; got {reuse!r}")
    return {"jev_uses": uses, "refinement": refinement, "product_reuse_rate": reuse_rate}


class Jev:
    """The Jev client stages reach through StageContext.ask. Tests pass a fake `client`."""

    def __init__(self, client: AsyncTypeSafeClient, model: str | None = None):
        self.client = client
        self.model = model or os.environ.get("JEV_MODEL") or DEFAULT_MODEL

    async def ask(self, ctx: StageContext, use: str, state: Any, questions: dict[str, str]) -> dict[str, float] | None:
        started = time.monotonic()
        response: SystemOneResponse | None = None
        error: str | None = "cancelled"
        try:
            response = await self.client.system_one(
                state=state,
                questions={name: Noul(instructions=text) for name, text in questions.items()},
                model=self.model,
            )
            answers = {name: response.nouls[name].noul for name in questions}
            error = None
            return answers
        except Exception as exc:
            error = describe(exc)
            ctx.run.note(f"jev {use} failed, using the Jev-off behavior: {error}", ctx.variant_index)
            return None
        finally:
            usage = response.usage if response is not None else None
            input_tokens = usage.input_tokens if usage is not None else None
            ctx.run.record_jev_call(
                ctx,
                use=use,
                model=response.model if response is not None else self.model,
                questions=len(questions),
                input_tokens=input_tokens,
                output_tokens=usage.output_tokens if usage is not None else None,
                cost_usd=round(input_tokens * USD_PER_INPUT_TOKEN, 8) if input_tokens is not None else None,
                elapsed=round(time.monotonic() - started, 3),
                error=error,
            )
