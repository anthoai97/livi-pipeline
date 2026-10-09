"""Pre-placement furniture fit heuristics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any
from app.rules.room_policy import BEDROOM_FIT_GROUPS, normalize_room_type

from .fit_policy import (
    CATEGORY_CAPS,
    PROTECTED_MIN_ROLES,
    REMOVAL_PRIORITY,
    ROLE_CAPS,
    ROLE_PROFILES,
    ROOM_THRESHOLDS,
)
from ._util import room_scale as _room_scale, round4 as _round
from .taxonomy import ROLE_CATEGORIES, normalize_category


def _normalize_category(category: Any) -> str:
    return normalize_category(category)


def _role_for_category(category: str) -> str:
    for role, categories in ROLE_CATEGORIES.items():
        if category in categories:
            return role
    return "other"


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _room_area(room_facts: Mapping[str, Any]) -> float:
    summary = room_facts.get("summary") or {}
    area = summary.get("area") or room_facts.get("area")
    if area:
        return _positive_float(area, 0.0)
    return _positive_float(room_facts.get("width"), 0.0) * _positive_float(room_facts.get("depth"), 0.0)


def _available_area(
    *,
    area_sqm: float,
    room_facts: Mapping[str, Any],
    feasibility_digest: Mapping[str, Any] | None,
) -> float:
    if isinstance(feasibility_digest, Mapping) and feasibility_digest.get("available_furnishing_area_sqm") is not None:
        return min(area_sqm, _positive_float(feasibility_digest.get("available_furnishing_area_sqm"), area_sqm))

    blocked_zones = room_facts.get("blocked_zones") or []
    door_clearance_area = sum(
        _positive_float(zone.get("width"), 0.0) * _positive_float(zone.get("depth"), 0.0)
        for zone in blocked_zones
        if isinstance(zone, Mapping) and zone.get("reason") == "door_clearance"
    )
    return max(0.0, area_sqm - door_clearance_area)


def _asset_load(
    asset: Mapping[str, Any], fallback_index: int, *, exact_assets: bool = False,
) -> dict[str, Any]:
    category = _normalize_category(asset.get("category") or asset.get("type")) or "unknown"
    role = _role_for_category(category)
    default_width, default_depth, clearance, weight = ROLE_PROFILES[role]
    width = float(asset["width"]) if exact_assets else _positive_float(asset.get("width"), default_width)
    depth = float(asset["depth"]) if exact_assets else _positive_float(asset.get("depth"), default_depth)
    footprint = width * depth
    adjusted = (width + clearance * 2.0) * (depth + clearance * 2.0) * weight
    mode = str(asset.get("placement_mode") or "").strip().lower()
    if asset.get("support_uid") or mode in {"tabletop", "wall", "ceiling"}:
        footprint = adjusted = 0.0
    if exact_assets:
        # Concrete selections share the selector's 2x floor-footprint density
        # basis. Rugs and supported/mounted pieces do not consume another floor
        # footprint; actual floor placement overrides broad category roles.
        occupies_floor = (
            category != "rug"
            and not asset.get("support_uid")
            and (
                mode == "floor"
                or (not mode and (category == "floor_mirror" or role not in {"tabletop", "wall_or_ceiling"}))
            )
        )
        footprint = width * depth if occupies_floor else 0.0
        adjusted = footprint * 2.0
    uid = str(asset.get("uid") or asset.get("id") or asset.get("asset_uid") or "").strip()

    return {
        "uid": uid or f"asset_{fallback_index}",
        "category": category,
        "role": role,
        "width": _round(width),
        "depth": _round(depth),
        "footprint_sqm": _round(footprint),
        "clearance_adjusted_sqm": _round(adjusted),
    }


def _normalize_counts(counts: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(counts, Mapping):
        return {}
    normalized: Counter[str] = Counter()
    for category, count in counts.items():
        try:
            amount = int(count)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            normalized[_normalize_category(category)] += amount
    return dict(sorted(normalized.items()))


def _role_counts(counts: Mapping[str, int]) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for category, count in counts.items():
        totals[_role_for_category(category)] += int(count)
    return dict(sorted(totals.items()))


def _average_loads(loads: list[Mapping[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for load in loads:
        grouped.setdefault(str(load["category"]), []).append(float(load["clearance_adjusted_sqm"]))
    return {category: sum(values) / len(values) for category, values in grouped.items()}


def _default_load(category: str) -> float:
    width, depth, clearance, weight = ROLE_PROFILES[_role_for_category(category)]
    return (width + clearance * 2.0) * (depth + clearance * 2.0) * weight


def _estimated_load(counts: Mapping[str, int], average_loads: Mapping[str, float]) -> float:
    # A supported TV shares its stand's floor space. This is only a load
    # estimate; catalog dimensions and the support inset still require validation.
    supported_tvs = min(
        int(counts.get("tv", 0)),
        sum(int(counts.get(category, 0)) for category in
            ("tv_stand", "media_unit", "media_console", "console_table")),
    )
    return sum(
        int(count) * float(average_loads.get(category, _default_load(category)))
        for category, count in counts.items()
    ) - supported_tvs * float(average_loads.get("tv", _default_load("tv")))


def _category_cap(category: str, room_scale: str) -> int:
    if category in CATEGORY_CAPS.get(room_scale, {}):
        return CATEGORY_CAPS[room_scale][category]
    return ROLE_CAPS[room_scale].get(_role_for_category(category), 2)


def _recommend_counts(
    *,
    source_counts: Mapping[str, int],
    room_scale: str,
    available_area_sqm: float,
    average_loads: Mapping[str, float],
    room_type: str = "living_room",
) -> dict[str, int]:
    recommended = {
        category: count if room_type in {"dining_room", "studio"} and category in {"dining_table", "dining_chair"}
        else min(count, _category_cap(category, room_scale))
        for category, count in source_counts.items()
        if min(count, _category_cap(category, room_scale)) > 0
    }
    target_load = available_area_sqm * ROOM_THRESHOLDS[room_scale]["comfortable"]

    if room_type == "bedroom":
        for group in reversed(BEDROOM_FIT_GROUPS):
            if _estimated_load(recommended, average_loads) <= target_load:
                break
            # Recommend dropping whole optional functions, never an orphan chair
            # or a TV without its support. This does not change selected assets.
            for category in group:
                recommended.pop(category, None)

    while target_load > 0 and _estimated_load(recommended, average_loads) > target_load:
        removable = [
            category
            for category, count in recommended.items()
            if count > (1 if room_type != "studio" and _role_for_category(category) in PROTECTED_MIN_ROLES else 0)
            and not (room_type in {"dining_room", "studio"} and category in {"dining_table", "dining_chair"})
            and not (room_type == "studio" and (
                category in {"bed", "sofa", "loveseat", "sectional", "sleeper_sofa"}
                or any(category in group for group in BEDROOM_FIT_GROUPS)
            ))
        ]
        if not removable:
            if room_type == "studio":
                # Keep optional functions until smaller accessories are exhausted,
                # then reduce whole groups in the requested retention order.
                group = next((group for group in reversed(BEDROOM_FIT_GROUPS)
                              if any(category in recommended for category in group)), None)
                if group:
                    for category in group:
                        recommended.pop(category, None)
                    continue
            break
        category = max(
            removable,
            key=lambda item: (
                REMOVAL_PRIORITY.get(_role_for_category(item), 0),
                average_loads.get(item, _default_load(item)),
            ),
        )
        recommended[category] -= 1
        if recommended[category] <= 0:
            recommended.pop(category)

    return dict(sorted(recommended.items()))


def _completion_report(
    *,
    room_scale: str,
    density_score: float,
    selected_counts: Mapping[str, int],
) -> dict[str, Any]:
    thresholds = ROOM_THRESHOLDS[room_scale]
    if density_score < thresholds["sparse"]:
        level = "sparse"
    elif density_score > thresholds["comfortable"]:
        level = "dense"
    else:
        level = "balanced"

    return {
        "level": level,
        "role_counts": _role_counts(selected_counts),
    }


def build_fit_check(
    *,
    room_facts: Mapping[str, Any],
    selected_assets: list[Mapping[str, Any]] | None = None,
    feasibility_digest: Mapping[str, Any] | None = None,
    requested_counts: Mapping[str, Any] | None = None,
    exact_assets: bool = False,
    room_type: str | None = None,
) -> dict[str, Any]:
    """Estimate whether selected/requested furniture can fit before placement."""
    loads = [
        _asset_load(asset, index, exact_assets=exact_assets)
        for index, asset in enumerate(selected_assets or [])
    ]
    selected_counts = _normalize_counts(Counter(load["category"] for load in loads))
    requested = _normalize_counts(requested_counts)
    source_counts = requested or selected_counts

    area_sqm = _room_area(room_facts)
    available_area_sqm = _available_area(
        area_sqm=area_sqm,
        room_facts=room_facts,
        feasibility_digest=feasibility_digest,
    )
    room_scale = str((feasibility_digest or {}).get("room_scale") or "").strip() or _room_scale(area_sqm)
    if room_scale not in ROOM_THRESHOLDS:
        room_scale = _room_scale(area_sqm)

    average_loads = _average_loads(loads)
    selected_adjusted_sqm = sum(float(load["clearance_adjusted_sqm"]) for load in loads)
    source_adjusted_sqm = _estimated_load(source_counts, average_loads) if requested else selected_adjusted_sqm
    density_score = source_adjusted_sqm / available_area_sqm if available_area_sqm > 0 else 0.0
    thresholds = ROOM_THRESHOLDS[room_scale]

    if density_score > thresholds["overcrowded"]:
        severity = "overcrowded"
        message = "This room may feel overcrowded."
    elif density_score > thresholds["comfortable"]:
        severity = "tight"
        message = "This room may feel tight."
    else:
        severity = "comfortable"
        message = "This room should fit comfortably."

    recommended = _recommend_counts(
        source_counts=source_counts,
        room_scale=room_scale,
        available_area_sqm=available_area_sqm,
        average_loads=average_loads,
        room_type=normalize_room_type(room_type or (feasibility_digest or {}).get("room_type")),
    )
    excess = {
        category: count - int(recommended.get(category, 0))
        for category, count in source_counts.items()
        if count > int(recommended.get(category, 0))
    }
    completion = _completion_report(
        room_scale=room_scale,
        density_score=density_score,
        selected_counts=selected_counts,
    )

    warnings = []
    if severity == "overcrowded":
        warnings.append(message)
    elif severity == "tight":
        warnings.append("Consider reducing optional pieces or choosing smaller furniture.")
    if completion["level"] == "sparse":
        warnings.append("This room may feel underfurnished for its size.")

    return {
        "load_basis": "measured_floor_footprint_x2" if exact_assets else "category_clearance_estimate",
        "severity": severity,
        "message": message,
        "room_scale": room_scale,
        "room_area_sqm": _round(area_sqm),
        "available_furnishing_area_sqm": _round(available_area_sqm),
        "furniture_footprint_sqm": _round(sum(float(load["footprint_sqm"]) for load in loads)),
        "clearance_adjusted_footprint_sqm": _round(source_adjusted_sqm),
        "selected_clearance_adjusted_footprint_sqm": _round(selected_adjusted_sqm),
        "density_score": _round(density_score),
        "thresholds": {
            "comfortable_max": thresholds["comfortable"],
            "overcrowded_min": thresholds["overcrowded"],
            "sparse_max": thresholds["sparse"],
        },
        "counts": {
            "requested": source_counts,
            "selected": selected_counts,
            "recommended": recommended,
            "excess": dict(sorted(excess.items())),
            "selected_roles": _role_counts(selected_counts),
        },
        "category_loads": loads,
        "completion": completion,
        "warnings": warnings,
    }
