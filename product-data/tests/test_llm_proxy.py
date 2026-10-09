"""ProxyClient request body: reasoning effort is sent only when set."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import llm_proxy


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()


@pytest.mark.parametrize(("effort", "env", "sent"), [(None, None, None), ("medium", None, "medium"), (None, "high", "high")])
def test_reasoning_effort_is_sent_only_when_set(monkeypatch, effort, env, sent):
    bodies = []
    monkeypatch.setattr(llm_proxy.urllib.request, "urlopen",
                        lambda request, timeout: bodies.append(json.loads(request.data)) or Response())
    monkeypatch.delenv("LLM_PROXY_EFFORT", raising=False)
    if env:
        monkeypatch.setenv("LLM_PROXY_EFFORT", env)

    client = llm_proxy.ProxyClient(base_url="http://proxy.test/v1", api_key="key", model="m", effort=effort)

    assert client.complete("hi", {"type": "object"}) == {"ok": True}
    assert bodies[0].get("reasoning_effort") == sent
    assert ("reasoning_effort" in bodies[0]) == (sent is not None)
