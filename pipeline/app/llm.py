"""Bounded Gemini structured-output calls with per-stage token and cost records."""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from types import SimpleNamespace

import httpx
from gemini_usage import estimate_cost, usage_counts
from google import genai
from google.genai import types
from pydantic import ValidationError

from app.graph import MODEL_CALL_ATTEMPTS, MODEL_CALL_TIMEOUT_S
from app.run import M, ModelCallError, StageContext, describe

DEFAULT_MODEL = "gemini-3.8-flash"
# Rate limit, server errors, and request timeouts. Transport timeouts also retry.
RETRY_STATUS_CODES = [408, 429, 500, 502, 503, 504]

# Counts HTTP attempts of the model call running in the current task.
_attempts: ContextVar[list[int] | None] = ContextVar("model_call_attempts", default=None)


async def _count_attempt(_: httpx.Request) -> None:
    if (counter := _attempts.get()) is not None:
        counter[0] += 1


def gemini_client(api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> genai.Client:
    """Gemini client with the model-call bounds: 60 s per attempt, at most 3 attempts.

    The explicit httpx client keeps async calls on httpx (google-genai otherwise
    prefers aiohttp when installed, whose timeouts are not retried) and counts attempts.
    """
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=MODEL_CALL_TIMEOUT_S * 1000,
            retry_options=types.HttpRetryOptions(attempts=MODEL_CALL_ATTEMPTS, http_status_codes=RETRY_STATUS_CODES),
            httpx_async_client=httpx.AsyncClient(transport=transport, event_hooks={"request": [_count_attempt]}),
        ),
    )


def _counts(response: types.GenerateContentResponse | None) -> dict[str, int]:
    """Adapt generate_content usage_metadata to gemini_usage.usage_counts."""
    usage = response.usage_metadata if response is not None else None
    if usage is None:
        return usage_counts(None)
    return usage_counts(
        SimpleNamespace(
            usage={
                "total_input_tokens": usage.prompt_token_count,
                "total_cached_tokens": usage.cached_content_token_count,
                "total_output_tokens": usage.candidates_token_count,
                "total_thought_tokens": usage.thoughts_token_count,
                "total_tokens": usage.total_token_count,
            }
        )
    )


class GeminiModel:
    """ModelClient backed by google-genai. Tests pass a fake `client`."""

    def __init__(self, client: genai.Client, model: str | None = None):
        self.client = client
        self.model = model or os.environ.get("LLM_DESIGN_MODEL") or DEFAULT_MODEL

    async def generate(self, ctx: StageContext, schema: type[M], contents: str, *, system: str | None = None) -> M:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )
        counter = [0]
        token = _attempts.set(counter)
        started = time.monotonic()
        response: types.GenerateContentResponse | None = None
        error: str | None = "cancelled"
        try:
            response = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
            result = schema.model_validate_json(response.text or "")
            error = None
            return result
        except ValidationError as exc:
            error = f"output does not match {schema.__name__} ({exc.error_count()} errors)"
            raise ModelCallError(f"{ctx.stage} model call: {error}") from exc
        except Exception as exc:
            error = describe(exc)
            raise ModelCallError(f"{ctx.stage} model call failed after {max(counter[0], 1)} attempt(s): {error}") from exc
        finally:
            _attempts.reset(token)
            counts = _counts(response)
            resolved = (response.model_version if response is not None else None) or self.model
            cost, _ = estimate_cost(resolved, counts)
            if cost is None:
                cost, _ = estimate_cost(self.model, counts)
            ctx.run.record_model_call(
                ctx,
                model=resolved,
                counts=counts,
                cost_usd=cost,
                elapsed=round(time.monotonic() - started, 3),
                attempts=max(counter[0], 1),
                error=error,
            )
