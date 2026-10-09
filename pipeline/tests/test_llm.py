"""Model-call bounds, exercised through a real genai client on a mock transport."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from app.contracts import PipelineRequest
from app.llm import GeminiModel, gemini_client
from app.run import ModelCallError, RunContext, StageContext

REQUEST = PipelineRequest(
    user_intent="cozy living room",
    budget=3000,
    room_type="living_room",
    room_area=(4.0, 5.0),
    room_vertices=[(0, 0), (4, 0), (4, 5), (0, 5)],
    wall_height=2.7,
)


class Plan(BaseModel):
    note: str


def error(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": "fail", "status": "FAIL"}})


def call_with(responses: list) -> tuple[RunContext, Exception | None, int]:
    """Run one model call whose HTTP attempts answer from `responses` in order."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        outcome = responses[min(len(requests), len(responses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run = RunContext("test", REQUEST, GeminiModel(gemini_client("test-key", httpx.MockTransport(handler))), 3)

    async def main() -> Exception | None:
        try:
            await StageContext(run, "select", 1).generate(Plan, "pick")
        except ModelCallError as exc:
            return exc
        return None

    return run, asyncio.run(main()), len(requests)


def test_retries_timeouts_rate_limits_and_server_errors_up_to_three_attempts():
    run, exc, sent = call_with([httpx.ReadTimeout("slow"), error(429), error(503), error(503)])

    assert isinstance(exc, ModelCallError)
    assert sent == 3
    [call] = run.model_calls
    assert call["stage"] == "select" and call["variant_index"] == 1
    assert call["attempts"] == 3
    assert "503" in call["error"]


def test_client_errors_are_not_retried():
    run, exc, sent = call_with([error(400)])

    assert isinstance(exc, ModelCallError)
    assert sent == 1
    assert run.model_calls[0]["attempts"] == 1


@pytest.mark.parametrize("body", ['{"note": 3}', "not json"])
def test_output_that_does_not_match_the_schema_fails_the_call(body):
    response = httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": body}]}}]})
    run, exc, sent = call_with([response])

    assert isinstance(exc, ModelCallError)
    assert sent == 1
    assert "does not match Plan" in run.model_calls[0]["error"]
