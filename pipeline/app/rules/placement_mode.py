"""Placement mode from prepared placement_type, plus the legacy category fallback.

Copied from core/planner/placement_semantics.py without its model classifier.
"""

from __future__ import annotations

from typing import Any

from app.rules.pipeline_shared import (
    is_ceiling_mounted_asset,
    is_floor_lamp_asset,
    is_floor_only_asset,
    is_table_lamp_asset,
    is_wall_mounted_asset,
)

PLACEMENT_MODES = {
    "floor",
    "tabletop",
    "wall_mounted",
    "ceiling_mounted",
    "unknown",
}

PLACEMENT_TYPE_MODES = {
    "floor": "floor",
    "surface": "tabletop",
    "wall": "wall_mounted",
    "ceiling": "ceiling_mounted",
}


def placement_mode_for_type(placement_type: Any) -> str:
    """Map prepared placement_type (floor, surface, wall, ceiling) to a layout placement mode."""
    return PLACEMENT_TYPE_MODES.get(str(placement_type or "").strip().lower(), "unknown")


def _asset_uid(asset: dict[str, Any]) -> str:
    return str(asset.get("uid") or asset.get("instance_key") or "").strip()


def deterministic_placement_mode(asset: dict[str, Any]) -> str:
    """Explicit placement_mode when valid, else the legacy category rule (ceiling, wall, table lamp, floor, unknown)."""
    uid = _asset_uid(asset)
    category = str(asset.get("category") or "")

    explicit = str(asset.get("placement_mode") or "").strip().lower()
    if explicit in PLACEMENT_MODES:
        return explicit

    if is_ceiling_mounted_asset(category, uid):
        return "ceiling_mounted"
    if is_wall_mounted_asset(category, uid):
        return "wall_mounted"
    if is_table_lamp_asset(category, uid):
        return "tabletop"
    if is_floor_lamp_asset(category, uid) or is_floor_only_asset(category, uid):
        return "floor"
    return "unknown"


def placement_mode_for_asset(asset: dict[str, Any]) -> str:
    mode = str(asset.get("placement_mode") or "").strip().lower()
    return mode if mode in PLACEMENT_MODES else deterministic_placement_mode(asset)
