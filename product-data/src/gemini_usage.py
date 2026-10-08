"""Record Gemini token usage and estimated paid-tier cost."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

# Paid-tier text, image, and video rates. Output includes thinking tokens.
# Source: https://ai.google.dev/gemini-api/docs/pricing
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_VERIFIED_ON = "2026-10-08"
PRICING = {
    "gemini-3.1-flash-lite": {
        "input_usd_per_million": 0.25,
        "cached_input_usd_per_million": 0.025,
        "output_usd_per_million": 1.50,
    },
    "gemini-3-flash": {
        "input_usd_per_million": 0.50,
        "cached_input_usd_per_million": 0.05,
        "output_usd_per_million": 3.00,
    },
    "gemini-3.5-flash-lite": {
        "input_usd_per_million": 0.30,
        "cached_input_usd_per_million": 0.03,
        "output_usd_per_million": 2.50,
    },
    "gemini-3.5-flash": {
        "input_usd_per_million": 1.50,
        "cached_input_usd_per_million": 0.15,
        "output_usd_per_million": 9.00,
    },
    "gemini-3.8-flash": {
        "input_usd_per_million": 0.75,
        "cached_input_usd_per_million": 0.075,
        "output_usd_per_million": 3.75,
    },
}


@dataclass(frozen=True)
class TokenPrice:
    label: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float


def match_price(model: str) -> TokenPrice | None:
    name = (model or "").split("/")[-1].lower()
    for label, rates in PRICING.items():
        if name.startswith(label):
            return TokenPrice(
                label,
                rates["input_usd_per_million"],
                rates["cached_input_usd_per_million"],
                rates["output_usd_per_million"],
            )
    return None


def usage_counts(interaction) -> dict[str, int]:
    """Read interaction.usage from the Interactions API."""
    usage = getattr(interaction, "usage", None)

    def count(field: str) -> int:
        if usage is None:
            return 0
        value = getattr(usage, field, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(field)
        return int(value or 0)

    prompt = count("total_input_tokens")
    cached = min(count("total_cached_tokens"), prompt)
    output = count("total_output_tokens")
    thoughts = count("total_thought_tokens")
    reported_total = count("total_tokens")
    billed_output = output + thoughts
    if reported_total and thoughts and reported_total < prompt + billed_output:
        billed_output = output
    return {
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "output_tokens": billed_output,
        "candidates_tokens": output,
        "thoughts_tokens": thoughts,
        "total_tokens": reported_total or prompt + billed_output,
    }


def estimate_cost(model: str, counts: dict[str, int]) -> tuple[float | None, dict]:
    price = match_price(model)
    if price is None or not counts:
        return None, {"matched_model": None, "pricing_source": PRICING_SOURCE, "pricing_verified_on": PRICING_VERIFIED_ON}
    fresh_input = counts["input_tokens"] - counts["cached_input_tokens"]
    cost = (
        fresh_input / 1_000_000 * price.input_usd_per_million
        + counts["cached_input_tokens"] / 1_000_000 * price.cached_input_usd_per_million
        + counts["output_tokens"] / 1_000_000 * price.output_usd_per_million
    )
    return round(cost, 8), {
        "matched_model": price.label,
        "input_usd_per_million": price.input_usd_per_million,
        "cached_input_usd_per_million": price.cached_input_usd_per_million,
        "output_usd_per_million": price.output_usd_per_million,
        "pricing_source": PRICING_SOURCE,
        "pricing_verified_on": PRICING_VERIFIED_ON,
    }


class GeminiUsage:
    """Append one JSON line per Gemini call, then write a run summary."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.summary_path = log_path.with_name("model-usage-summary.json")
        self.calls: list[dict] = []
        self._lock = Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_chat(self, payload: dict, *, step: str, requested_model: str, source_table: str, source_id: str) -> None:
        usage = payload.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        completion = usage.get("completion_tokens_details") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        output = int(usage.get("completion_tokens") or 0)
        thoughts = int(completion.get("reasoning_tokens") or 0) if isinstance(completion, dict) else 0
        counts = {
            "input_tokens": prompt,
            "cached_input_tokens": min(cached, prompt),
            "output_tokens": output,
            "candidates_tokens": max(output - thoughts, 0),
            "thoughts_tokens": thoughts,
            "total_tokens": int(usage.get("total_tokens") or prompt + output),
        }
        resolved = payload.get("model") or requested_model
        self._write(
            counts,
            step=step,
            requested_model=requested_model,
            resolved_model=str(resolved),
            source_table=source_table,
            source_id=source_id,
            cost_usd=0.0,
            pricing={
                "billing": "codex-pro-subscription",
                "matched_model": str(resolved),
                "note": "No per-token API charge. Tokens count against the ChatGPT Codex Pro subscription.",
            },
        )

    def record(self, response, *, step: str, requested_model: str, source_table: str, source_id: str) -> None:
        resolved = getattr(response, "model", None) or requested_model
        counts = usage_counts(response)
        cost, pricing = estimate_cost(str(resolved), counts)
        if cost is None:
            cost, pricing = estimate_cost(requested_model, counts)
        self._write(
            counts,
            step=step,
            requested_model=requested_model,
            resolved_model=str(resolved),
            source_table=source_table,
            source_id=source_id,
            cost_usd=cost,
            pricing=pricing,
        )

    def _write(self, counts: dict, *, step: str, requested_model: str, resolved_model: str, source_table: str, source_id: str, cost_usd, pricing: dict) -> None:
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "source_table": source_table,
            "source_id": source_id,
            "requested_model": requested_model,
            "resolved_model": resolved_model,
            **counts,
            "cost_usd": cost_usd,
            "pricing": pricing,
        }
        line = json.dumps(entry, ensure_ascii=True)
        with self._lock:
            self.calls.append(entry)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def summary(self) -> dict:
        with self._lock:
            calls = list(self.calls)
        totals = {
            "calls": len(calls),
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "unpriced_calls": 0,
        }
        by_step: dict[str, dict] = {}
        for call in calls:
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "thoughts_tokens", "total_tokens"):
                totals[key] += int(call.get(key) or 0)
            if call.get("cost_usd") is None:
                totals["unpriced_calls"] += 1
            else:
                totals["cost_usd"] = round(totals["cost_usd"] + float(call["cost_usd"]), 8)
            step = by_step.setdefault(call["step"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            step["calls"] += 1
            step["input_tokens"] += int(call.get("input_tokens") or 0)
            step["output_tokens"] += int(call.get("output_tokens") or 0)
            step["cost_usd"] = round(step["cost_usd"] + float(call.get("cost_usd") or 0), 8)
        totals["cost_usd"] = round(totals["cost_usd"], 8)
        report = {
            "pricing_source": PRICING_SOURCE,
            "pricing_verified_on": PRICING_VERIFIED_ON,
            "totals": totals,
            "by_step": by_step,
            "calls": calls,
        }
        self.summary_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report
