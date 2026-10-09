"""Support-surface eligibility and fit, ported from core/geometry/candidates.py.

Only the helpers selection preflight uses are ported; the seed-placement
candidate search is not.
"""

from __future__ import annotations
from typing import Any
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.geometry.primitives import (
    _MEDIA_DISPLAY_ROLE_KEYWORDS,
    _MEDIA_SUPPORT_ROLE_KEYWORDS,
    _SUPPORT_INSET_M,
)
from app.rules.pipeline_shared import (
    is_table_lamp_asset,
    is_table_lamp_support_asset,
    is_tabletop_support_asset,
    matches_category_keywords,
)

def _is_media_display_asset(asset: dict[str, Any], uid: str) -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        _MEDIA_DISPLAY_ROLE_KEYWORDS,
    )

def _is_media_support_asset(asset: dict[str, Any], uid: str) -> bool:
    return matches_category_keywords(
        asset.get("category", ""),
        uid,
        _MEDIA_SUPPORT_ROLE_KEYWORDS,
    )

def _requires_support_surface(uid: str, asset: dict[str, Any]) -> bool:
    category = asset.get("category", "")
    if is_table_lamp_asset(category, uid):
        return True
    return placement_mode_for_asset({**asset, "uid": uid}) == "tabletop"

def _support_surface_eligible(
    uid: str,
    asset: dict[str, Any],
    parent_uid: str,
    parent_asset: dict[str, Any],
) -> bool:
    parent_category = parent_asset.get("category", "")
    if parent_uid == uid or _requires_support_surface(parent_uid, parent_asset):
        return False
    if _is_media_display_asset(asset, uid):
        return _is_media_support_asset(parent_asset, parent_uid)
    if is_table_lamp_asset(asset.get("category", ""), uid):
        if is_table_lamp_support_asset(parent_category, parent_uid):
            return True
        explicit_support_uid = str(
            asset.get("paired_support_uid") or asset.get("support_uid") or ""
        ).strip()
        return (
            explicit_support_uid == parent_uid
            and is_tabletop_support_asset(parent_category, parent_uid)
        )
    return is_tabletop_support_asset(parent_category, parent_uid)

def _support_usable_size(width: float, depth: float) -> tuple[float, float]:
    return (
        max(0.0, width - 2 * _SUPPORT_INSET_M),
        max(0.0, depth - 2 * _SUPPORT_INSET_M),
    )

def _support_fit_margins(
    asset: dict[str, Any],
    parent_width: float,
    parent_depth: float,
) -> tuple[float, float]:
    width = float(asset.get("width", 0.5) or 0.5)
    depth = float(asset.get("depth", 0.5) or 0.5)
    usable_width, usable_depth = _support_usable_size(parent_width, parent_depth)
    return (
        (usable_width - width) / 2,
        (usable_depth - depth) / 2,
    )

def _support_surface_fits(
    asset: dict[str, Any],
    parent_width: float,
    parent_depth: float,
) -> bool:
    margin_x, margin_y = _support_fit_margins(asset, parent_width, parent_depth)
    return margin_x >= -1e-6 and margin_y >= -1e-6
