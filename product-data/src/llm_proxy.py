"""Structured JSON calls through the local cli-proxy-api container.

Reads AI_PROXY_BASE_URL, AI_PROXY_API_KEY, and LLM_PROXY_MODEL (default
gpt-6-luna). Calls use the ChatGPT Codex Pro subscription, so there is no
per-token charge.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable

DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
DEFAULT_MODEL = "gpt-6-luna"


class ProxyClient:
    """OpenAI-compatible client for the local cli-proxy-api container."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ.get("AI_PROXY_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("AI_PROXY_API_KEY", "").strip()
        self.model = model or os.environ.get("LLM_PROXY_MODEL", "").strip() or DEFAULT_MODEL
        if not self.api_key:
            raise RuntimeError("AI_PROXY_API_KEY is required")

    def chat(self, messages: list, schema: dict, name: str = "response") -> dict:
        """One chat completion constrained to a strict JSON schema. Returns the raw payload."""
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
        }).encode()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"proxy {exc.code}: {detail}") from exc

    def complete(
        self,
        content: str | list,
        schema: dict,
        *,
        name: str = "response",
        attempts: int = 3,
        on_response: Callable[[dict], None] | None = None,
    ) -> dict:
        """Send one user message and return its parsed JSON reply, with bounded retries.

        content is text, or a list of OpenAI content parts for text with images.
        on_response receives each raw payload, for example to record token usage.
        """
        last_error = None
        for attempt in range(attempts):
            try:
                payload = self.chat([{"role": "user", "content": content}], schema, name)
                if on_response:
                    on_response(payload)
                return json.loads(payload["choices"][0]["message"].get("content") or "")
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"proxy call failed: {last_error}")
