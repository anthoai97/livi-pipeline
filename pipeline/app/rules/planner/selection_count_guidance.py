"""Room-scale count guidance for asset selection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from .taxonomy import (
    ANCHOR_COUNT_EQUIVALENTS,
    COUNT_CONSTRAINT_EQUIVALENTS,
    COUNT_GUIDANCE_ROLE_CATEGORIES,
    normalize_category,
    selection_category,
)


def selection_category_counts(
    uids: list[str],
    assets_by_uid: dict[str, dict],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for uid in uids:
        asset = assets_by_uid.get(uid)
        if not asset:
            continue
        category = selection_category(asset)
        if not category:
            continue
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def selection_role_counts(category_counts: dict[str, int]) -> dict[str, int]:
    role_counts: dict[str, int] = {}
    for category, count in category_counts.items():
        for role, categories in COUNT_GUIDANCE_ROLE_CATEGORIES.items():
            if category in categories:
                role_counts[role] = role_counts.get(role, 0) + count
                break
    return dict(sorted(role_counts.items()))


def _fit_satisfaction_counts(
    fit_satisfaction: Any,
    *,
    uids: list[str],
) -> dict[str, int]:
    if not isinstance(fit_satisfaction, Mapping):
        return {}
    selected_counts = fit_satisfaction.get("selected_counts")
    if not isinstance(selected_counts, list):
        return {}

    remaining_uids = Counter(str(uid) for uid in uids if str(uid))
    counts: Counter[str] = Counter()
    for entry in selected_counts:
        if not isinstance(entry, Mapping):
            continue
        category = normalize_category(entry.get("category"))
        if not category:
            continue
        selected_uids = entry.get("selected_uids")
        if not isinstance(selected_uids, list):
            continue
        matched = 0
        for raw_uid in selected_uids:
            uid = str(raw_uid or "")
            if remaining_uids.get(uid, 0) <= 0:
                continue
            remaining_uids[uid] -= 1
            matched += 1
        if matched <= 0:
            continue
        try:
            declared_count = int(entry.get("count"))
        except (TypeError, ValueError):
            declared_count = matched
        counts[category] += min(matched, max(0, declared_count))
    return dict(sorted(counts.items()))


def _constraint_categories(category: str) -> set[str]:
    categories = {category}
    if category in ANCHOR_COUNT_EQUIVALENTS:
        categories.update(ANCHOR_COUNT_EQUIVALENTS)
    equivalents = COUNT_CONSTRAINT_EQUIVALENTS.get(category)
    if equivalents:
        categories.update(equivalents)
    return categories


def _explicitly_counted_categories(
    explicit_count_constraints: Mapping[str, Any] | None,
) -> set[str]:
    """Categories covered by a hard user-stated count (exact/counted,
    non-optional). Digest caps yield to these: the user's explicit ask wins
    over the room heuristic, and the constraint's own over-count check still
    bounds the selection."""
    counted: set[str] = set()
    if not isinstance(explicit_count_constraints, Mapping):
        return counted
    for raw_category, raw_constraint in explicit_count_constraints.items():
        if isinstance(raw_constraint, Mapping):
            raw_count = raw_constraint.get("count")
            hard = (
                bool(raw_constraint.get("exact"))
                or str(raw_constraint.get("source") or "") == "counted"
            ) and not bool(raw_constraint.get("optional"))
        else:
            raw_count = raw_constraint
            hard = True
        if hard and isinstance(raw_count, (int, float)) and int(raw_count) > 0:
            counted |= _constraint_categories(normalize_category(raw_category))
    return counted


def _selected_count_for_constraint(
    *,
    category: str,
    requested_count: int,
    category_counts: Mapping[str, int],
    exact: bool = False,
    acceptable_substitutes: list[str] | None = None,
    fit_satisfaction_counts: Mapping[str, int] | None = None,
) -> int:
    direct_categories = _constraint_categories(category)
    direct_count = sum(
        int(category_counts.get(candidate, 0))
        for candidate in direct_categories
        if candidate
    )
    if exact:
        return direct_count
    substitute_categories = {
        candidate
        for candidate in (acceptable_substitutes or [])
        if candidate and candidate not in direct_categories
    }
    substitute_count = sum(
        int(category_counts.get(candidate, 0))
        for candidate in substitute_categories
    )
    selected_count = direct_count + min(
        substitute_count,
        max(0, requested_count - direct_count),
    )
    if fit_satisfaction_counts and category in fit_satisfaction_counts:
        selected_count = max(
            selected_count,
            int(fit_satisfaction_counts.get(category, 0)),
        )
    return selected_count


def count_guidance_feedback(
    uids: list[str],
    assets_by_uid: dict[str, dict],
    feasibility_digest: dict[str, Any],
    *,
    explicit_count_constraints: Mapping[str, Any] | None = None,
    fit_satisfaction: Any = None,
    enforce_inferred_max: bool = False,
) -> dict[str, Any]:
    category_counts = selection_category_counts(uids, assets_by_uid)
    role_counts = selection_role_counts(category_counts)
    max_counts = feasibility_digest.get("max_counts_by_category") or {}
    satisfaction_counts = _fit_satisfaction_counts(fit_satisfaction, uids=uids)

    errors = []
    warnings = []
    explicitly_counted = _explicitly_counted_categories(explicit_count_constraints)
    for category, count in category_counts.items():
        max_count = max_counts.get(category)
        if not isinstance(max_count, (int, float)):
            continue
        max_count = int(max_count)
        if count > max_count:
            if max_count <= 0:
                message = (
                    f"COUNT LIMIT: {category} is not recommended for this room; selected {count}."
                )
            else:
                message = (
                    f"COUNT LIMIT: Selected {count} {category}; recommended max is {max_count}."
                )
            # A hard user-stated count overrides the digest cap for its
            # category; the cap breach becomes advisory.
            (warnings if category in explicitly_counted else errors).append(message)
    explicit_metrics: dict[str, dict[str, Any]] = {}
    if isinstance(explicit_count_constraints, Mapping):
        for raw_category, raw_constraint in explicit_count_constraints.items():
            category = normalize_category(raw_category)
            if isinstance(raw_constraint, Mapping):
                raw_count = raw_constraint.get("count")
                exact = bool(raw_constraint.get("exact"))
                optional = bool(raw_constraint.get("optional"))
                source = str(raw_constraint.get("source") or "")
                acceptable_substitutes = [
                    normalize_category(candidate)
                    for candidate in raw_constraint.get("acceptable_substitutes") or []
                ]
            else:
                raw_count = raw_constraint
                exact = False
                optional = False
                source = "counted"
                acceptable_substitutes = []
            if not isinstance(raw_count, (int, float)) or int(raw_count) <= 0:
                continue
            requested_count = int(raw_count)
            selected_count = _selected_count_for_constraint(
                category=category,
                requested_count=requested_count,
                category_counts=category_counts,
                exact=exact,
                acceptable_substitutes=acceptable_substitutes,
                fit_satisfaction_counts=satisfaction_counts,
            )
            explicit_metrics[category] = {
                "count": requested_count,
                "selected": selected_count,
                "exact": exact,
                "optional": optional,
                "source": source,
            }
            if acceptable_substitutes:
                explicit_metrics[category]["acceptable_substitutes"] = [
                    candidate for candidate in acceptable_substitutes if candidate
                ]
            if selected_count > requested_count:
                message = (
                    f"COUNT LIMIT: Requested {requested_count} {category}; "
                    f"selected {selected_count}."
                )
                if exact or source == "counted" or (enforce_inferred_max and not optional):
                    errors.append(message)
                else:
                    warnings.append(
                        f"INFERRED COUNT: Initial suggestion was {requested_count} {category}; "
                        f"selected {selected_count}. This is not a user-stated limit."
                    )
            if (
                selected_count < requested_count
                and not optional
                and (exact or source == "counted")
            ):
                qualifier = "exactly " if exact else ""
                errors.append(
                    f"REQUESTED COUNT: Requested {qualifier}{requested_count} {category}; selected {selected_count}."
                )

    return {
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "category_counts": category_counts,
            "role_counts": role_counts,
            "max_counts_by_category": {
                str(category): int(count)
                for category, count in max_counts.items()
                if isinstance(count, (int, float))
            },
            "explicit_count_constraints": explicit_metrics,
        },
    }
