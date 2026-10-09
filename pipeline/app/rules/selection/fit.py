"""Deterministic fit counts and the two automatic fit steps.

The pipeline never asks the user to confirm fit. Fit step "compact" applies the
legacy automatic continue_anyway decision (keep requested counts, pick compact
products). Fit step "capped" applies the legacy use_recommendation decision
(cap each requested count at the count the room fit estimate says will fit).
"""

import re
from typing import Any
from app.rules.planner.taxonomy import normalize_category

from app.rules.selection.catalog import (
    _base_uid,
    _catalog_asset_for_selected,
    _coerce_count_map,
)
from app.rules.selection.constants import (
    FIT_ACCENT_SEATING,
    FIT_CATEGORY_LABELS,
    FIT_LIGHTING,
)

# Legacy fit decision applied by each automatic fit step.
FIT_STEP_DECISIONS = {"compact": "continue_anyway", "capped": "use_recommendation"}

def _fit_warning_counts(room: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """Requested counts and the counts that fit, from the room fit estimate."""
    counts = (room.get("fit") or {}).get("counts") or {}
    return _coerce_count_map(counts.get("requested")), _coerce_count_map(counts.get("recommended"))

def _target_count_for_bucket(
    bucket: str,
    requested_counts: dict[str, int],
    recommended_counts: dict[str, int],
    *,
    decision: str,
) -> int:
    requested = int(requested_counts.get(bucket, 0))
    if decision == "use_recommendation":
        recommended = int(recommended_counts.get(bucket, 0))
        return max(0, min(requested, recommended))
    return requested

def _fit_decision_target_counts(
    *,
    decision: str,
    requested_counts: dict[str, int],
    recommended_counts: dict[str, int],
) -> dict[str, int]:
    return {
        category: _target_count_for_bucket(
            category,
            requested_counts,
            recommended_counts,
            decision=decision,
        )
        for category in requested_counts
    }

def capped_counts(room: dict[str, Any]) -> dict[str, int]:
    """Selection targets for fit step "capped": each requested count capped at the count that fits.

    The room fit estimate drops lower-priority optional furniture first, keeps
    functional groups whole, and keeps required items such as the bed.
    """
    requested_counts, recommended_counts = _fit_warning_counts(room)
    return _fit_decision_target_counts(
        decision="use_recommendation",
        requested_counts=requested_counts,
        recommended_counts=recommended_counts,
    )

def fit_step_guidance(fit_step: str | None, room: dict[str, Any]) -> str:
    """Selection prompt block for the applied fit step ("" when none or nothing was requested)."""
    decision = FIT_STEP_DECISIONS.get(str(fit_step or ""), "")
    if decision not in {"use_recommendation", "continue_anyway"}:
        return ""
    requested_counts, recommended_counts = _fit_warning_counts(room)
    if not requested_counts:
        return ""
    if decision == "continue_anyway":
        return f"""
=== ADVISORY FIT ESTIMATE ===
The pipeline automatically continued after a coarse category-size estimate; the user did not accept a reduced plan.
Original requested counts: {_format_fit_counts(requested_counts)}
Keep these requests as the goal and try compact catalog items with their actual dimensions.
The estimate does not establish that any requested item cannot fit and its recommended counts are not caps.
Only omit optional furniture when concrete selection or layout constraints require it, and explain the constraint in selection_strategy.gaps.
For bedrooms, preserve the bed and access, then prioritize requested wardrobe, requested TV/support, and requested desk/chair, in that order. These groups are opt-in; do not add a wardrobe that was not requested. Omit a lower-priority group before a higher-priority one and keep each functional group complete.
All actual footprint, support, budget, and layout checks still apply.
"""
    target_counts = _fit_decision_target_counts(
        decision=decision,
        requested_counts=requested_counts,
        recommended_counts=recommended_counts,
    )
    is_recommendation = decision == "use_recommendation"
    decision_label = (
        "User accepted the recommended furniture counts."
        if is_recommendation
        else "User chose to continue, so satisfy the original requested counts with the best fit-safe subset you can assemble."
    )
    fit_check = room.get("fit") or {}
    fit_counts = fit_check.get("counts") if isinstance(fit_check.get("counts"), dict) else {}
    warning_detail = str(fit_check.get("message") or "").strip()
    previous_counts = _coerce_count_map(fit_counts.get("selected"))
    excess_counts = _coerce_count_map(fit_counts.get("excess"))
    overcrowding_lines = [
        "Why: "
        + (
            warning_detail
            or "the requested furniture counts did not fit the usable floor area."
        )
    ]
    if previous_counts:
        overcrowding_lines.append(
            f"Counts that triggered the warning: {_format_fit_counts(previous_counts)}"
        )
    if excess_counts:
        overcrowding_lines.append(
            f"Counts flagged as excess: {_format_fit_counts(excess_counts)}"
        )
    overcrowding_block = "\n".join(overcrowding_lines)
    # The legacy model-built fit recommendation plan is not ported.
    plan_block = ""
    recommendation_line = (
        f"Recommended usable counts: {_format_fit_counts(recommended_counts)}"
        if is_recommendation
        else (
            "Prior recommendation counts, not a cap for continue-anyway: "
            f"{_format_fit_counts(recommended_counts)}"
        )
    )
    plant_exception = (
        " and the required non-shoppable decor plant in a fresh design"
        if room.get("room_type") != "studio"
        else ""
    )
    reduction_rule = (
        (
            "You must produce the reduced plan yourself. Select at most the target count for "
            "each requested category; never exceed it, and never add categories outside the "
            f"requested set except fitting rugs when rugs were not explicitly excluded{plant_exception}. "
            "The server does not trim, re-add, or reorder your selection - "
            "whatever you return is what the room gets."
        )
        if is_recommendation
        else (
            "You must produce the fit-safe plan yourself. Keep the requested counts as the "
            "goal, but choose the most compact catalog item for every slot so the total "
            "footprint stays inside the stated furniture area. If the requested counts cannot "
            "all fit even at the smallest available sizes, drop the lowest-priority requested "
            "items yourself and say so in selection_strategy.gaps. The server does not trim or "
            "re-add anything - whatever you return is what the room gets."
        )
    )
    return f"""
=== ACCEPTED FIT DECISION ===
The user answered the fit warning with: {decision}.
{decision_label}
{overcrowding_block}
Original requested counts: {_format_fit_counts(requested_counts)}
{recommendation_line}
Selection target for this run: {_format_fit_counts(target_counts)}
{plan_block}

{reduction_rule}
A selection that still exceeds the footprint limit fails validation outright; there is no second confirmation step, so bring it inside the limit in this run.
Use your judgment to choose compact catalog items that satisfy the requested categories and target counts.
Prefer exact category matches. If a selected item is a clear substitute for a requested category because of its name, description, dimensions, or style, keep it and explain that substitution in the item reason.
Do not add unrequested furniture categories just to spend budget.
Include at least one fitting rug unless the user explicitly excluded rugs; rugs do not count toward the density floor.
For this accepted fit decision, satisfying the target categories and preserving circulation is more important than using the remaining budget.
Return fit_satisfaction.selected_counts for every requested category you satisfied. Each entry must use the requested category name, the count satisfied, and the selected UIDs that satisfy it. Include substitutions when a selected UID satisfies a different requested category.
"""

def _fit_bucket_for_category(
    category: str,
    requested_counts: dict[str, int],
) -> str:
    category = normalize_category(category)
    if category in requested_counts:
        return category
    if "accent_chair" in requested_counts and category in FIT_ACCENT_SEATING:
        return "accent_chair"
    if "lamp" in requested_counts and category in FIT_LIGHTING:
        return "lamp"
    return category

def _fit_bucket_for_asset(
    asset: dict[str, Any],
    requested_counts: dict[str, int],
    assets_by_uid: dict[str, dict],
    fit_satisfaction_buckets: dict[str, str] | None = None,
) -> str:
    uid = str(asset.get("uid") or "")
    base_uid = _base_uid(uid, assets_by_uid)
    if fit_satisfaction_buckets:
        if uid in fit_satisfaction_buckets:
            return fit_satisfaction_buckets[uid]
        if base_uid in fit_satisfaction_buckets:
            return fit_satisfaction_buckets[base_uid]
    hydrated = _catalog_asset_for_selected(asset, assets_by_uid)
    return _fit_bucket_for_category(str(hydrated.get("category") or ""), requested_counts)

def _fit_category_counts(
    assets: list[dict[str, Any]],
    requested_counts: dict[str, int],
    assets_by_uid: dict[str, dict],
    fit_satisfaction_buckets: dict[str, str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for asset in assets:
        bucket = _fit_bucket_for_asset(
            asset,
            requested_counts,
            assets_by_uid,
            fit_satisfaction_buckets,
        )
        if requested_counts and bucket not in requested_counts:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))

def _fit_satisfaction_uid_buckets(
    parsed: dict[str, Any],
    selected_assets: list[dict[str, Any]],
    requested_counts: dict[str, int],
    assets_by_uid: dict[str, dict],
) -> dict[str, str]:
    if not requested_counts:
        return {}
    fit_satisfaction = parsed.get("fit_satisfaction")
    if not isinstance(fit_satisfaction, dict):
        return {}
    selected_counts = fit_satisfaction.get("selected_counts")
    if not isinstance(selected_counts, list):
        return {}

    selected_uid_tokens: dict[str, set[str]] = {}
    for asset in selected_assets:
        uid = str(asset.get("uid") or "")
        if not uid:
            continue
        tokens = {uid}
        base_uid = _base_uid(uid, assets_by_uid)
        if base_uid:
            tokens.add(base_uid)
        selected_uid_tokens[uid] = tokens

    buckets: dict[str, str] = {}
    for entry in selected_counts:
        if not isinstance(entry, dict):
            continue
        category_text = re.sub(
            r"[\s-]+",
            " ",
            str(entry.get("category") or "").strip().lower(),
        )
        category = normalize_category(category_text)
        if category not in requested_counts:
            continue
        selected_uids = entry.get("selected_uids")
        if not isinstance(selected_uids, list):
            continue
        for selected_uid in selected_uids:
            selected_uid = str(selected_uid or "").strip()
            if not selected_uid:
                continue
            for actual_uid, tokens in selected_uid_tokens.items():
                if selected_uid not in tokens:
                    continue
                buckets[actual_uid] = category
                for token in tokens:
                    buckets[token] = category
    return buckets

def _coerce_fit_satisfaction_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"selected_counts": value}
    return {}

def _fit_target_validation_feedback(
    *,
    decision: str | None,
    requested_counts: dict[str, int],
    recommended_counts: dict[str, int],
    selected_assets: list[dict[str, Any]],
    assets_by_uid: dict[str, dict],
    fit_satisfaction: Any = None,
) -> dict[str, Any]:
    if decision not in {"use_recommendation", "continue_anyway"} or not requested_counts:
        return {"errors": [], "target_counts": {}, "selected_counts": {}}

    target_counts = _fit_decision_target_counts(
        decision=decision,
        requested_counts=requested_counts,
        recommended_counts=recommended_counts,
    )
    satisfaction_payload = _coerce_fit_satisfaction_payload(fit_satisfaction)
    satisfaction_buckets = _fit_satisfaction_uid_buckets(
        {"fit_satisfaction": satisfaction_payload},
        selected_assets,
        requested_counts,
        assets_by_uid,
    )
    selected_counts = _fit_category_counts(
        selected_assets,
        requested_counts,
        assets_by_uid,
        satisfaction_buckets,
    )
    if decision == "continue_anyway":
        return {
            "errors": [],
            "target_counts": target_counts,
            "selected_counts": selected_counts,
        }
    missing_counts = {
        category: max(0, int(target) - int(selected_counts.get(category, 0)))
        for category, target in target_counts.items()
        if int(target) > int(selected_counts.get(category, 0))
    }
    if not missing_counts:
        return {
            "errors": [],
            "target_counts": target_counts,
            "selected_counts": selected_counts,
        }
    missing_text = _format_fit_counts(missing_counts)
    target_text = _format_fit_counts(target_counts)
    return {
        "errors": [
            "MISSING FIT TARGET: Accepted fit decision requires "
            f"{target_text}; this validation call is missing {missing_text}. "
            "Select compact catalog items for the missing requested categories. "
            "If a selected asset is a legitimate compact substitute, include it in "
            "fit_satisfaction.selected_counts with the requested category and selected_uids."
        ],
        "target_counts": target_counts,
        "selected_counts": selected_counts,
    }

def _fit_count_label(category: str, count: int) -> str:
    singular, plural = FIT_CATEGORY_LABELS.get(
        category,
        (category.replace("_", " "), f"{category.replace('_', ' ')}s"),
    )
    return singular if count == 1 else plural

def _format_fit_counts(counts: dict[str, int]) -> str:
    parts = [
        f"{count} {_fit_count_label(category, count)}"
        for category, count in sorted(counts.items())
        if count > 0
    ]
    if not parts:
        return "0 items"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"
