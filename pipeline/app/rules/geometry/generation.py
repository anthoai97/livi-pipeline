"""Deterministic seed layout, ported from core/geometry/generation.py."""

from __future__ import annotations
from typing import Any
from shapely.geometry import Polygon
from app.rules.planner.seed_guidance import build_seed_layout_plan
from app.rules.pipeline_shared import (
    WALL_MOUNT_Z,
    ceiling_mount_z,
    is_ceiling_mounted_asset,
    is_wall_mounted_asset,
    requires_window_clearance,
)
from app.rules.protected_paths import protected_path_polygons
from app.rules.geometry.candidates import (
    _dedupe_candidates,
    _dynamic_preferred_walls,
    _guided_candidates,
    _placement_candidates,
    _requires_support_surface,
)
from app.rules.geometry.placement import (
    _place_on_support_surface,
    _placement_outcome,
    _placement_target,
    _rug_fits_room_bounds,
    _search_placement,
    _skip_reason,
)
from app.rules.geometry.primitives import _blocker_polygons, asset_polygon, is_rug, room_polygon

def generate_deterministic_layout_with_report(
    assets: list[dict[str, Any]],
    room_area: tuple[float, float],
    room_vertices: list[list[float]] | None = None,
    room_doors: list[dict[str, Any]] | None = None,
    room_windows: list[dict[str, Any]] | None = None,
    planner_guidance: dict[str, Any] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
    placed: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic seed layout for the LLM.

    assets are the selected instances (keyed by uid); planner_guidance is
    build_seed_guidance output. placed holds poses fixed beforehand (the code
    solver's groups): they are kept, their floor footprints block the other
    items, and they get no outcome. Returns (layout, report): layout maps uid to
    {category, position, rotation, optional on_top_of} for placed items only;
    report holds placed_comfortably and placed_tightly uids, skipped outcomes
    ({uid, category, status: "skipped_<reason>", reason, ...}), and all outcomes.
    """
    room_width, room_depth = room_area
    room_poly = room_polygon(room_area, room_vertices)
    room_cover = room_poly.buffer(1e-6)
    full_blockers = _blocker_polygons(
        room_doors,
        room_windows,
        boundary=room_vertices,
        room_area=room_area,
    )
    door_only_blockers = _blocker_polygons(
        room_doors,
        room_windows,
        boundary=room_vertices,
        room_area=room_area,
        allow_window_overlap=True,
    )
    protected_path_blockers = protected_path_polygons(protected_paths or [])
    asset_map = {asset.get("uid", ""): asset for asset in assets if asset.get("uid")}
    seed_plan = build_seed_layout_plan(assets, planner_guidance)
    uid_rank = {
        uid: index for index, uid in enumerate(seed_plan.get("ordered_uids") or [])
    }
    placement_modes = seed_plan.get("placement_mode_by_uid") or {}
    support_dependent_uids = {
        uid
        for asset in assets
        if (uid := asset.get("uid", ""))
        and (
            _requires_support_surface(uid, asset)
            or placement_modes.get(uid) == "on_support_surface"
            or any(
                str(asset.get(field) or "") in asset_map
                and str(asset.get(field) or "") != uid
                for field in ("paired_support_uid", "support_uid")
            )
        )
    }
    layout: dict[str, Any] = dict(placed or {})
    occupied: list[Polygon] = [
        asset_polygon(placement["position"], placement["rotation"][2],
                      float(asset_map[uid].get("width", 0.5) or 0.5), float(asset_map[uid].get("depth", 0.5) or 0.5))
        for uid, placement in layout.items()
        if not is_rug(uid, asset_map[uid]) and float(placement["position"][2]) <= 0.1
    ]
    outcomes: list[dict[str, Any]] = []

    ordered_assets = sorted(
        assets,
        key=lambda asset: (
            asset.get("uid", "") in support_dependent_uids,
            uid_rank.get(asset.get("uid", ""), 10**6),
            -(float(asset.get("width", 0.5) or 0.5) * float(asset.get("depth", 0.5) or 0.5)),
            asset.get("uid", ""),
        ),
    )
    wall_offset = 0

    for asset in ordered_assets:
        uid = asset.get("uid", "")
        if not uid or uid in layout:
            continue

        width = float(asset.get("width", 0.5) or 0.5)
        depth = float(asset.get("depth", 0.5) or 0.5)
        category = asset.get("category", "")
        rug_like = is_rug(uid, asset)
        if is_wall_mounted_asset(category, uid):
            z = WALL_MOUNT_Z
        elif is_ceiling_mounted_asset(category, uid):
            z = ceiling_mount_z(asset.get("height", 0.3))
        else:
            z = 0.0

        placement_mode = placement_modes.get(uid)
        if rug_like and not _rug_fits_room_bounds(asset, room_cover):
            outcomes.append(
                _placement_outcome(
                    uid=uid,
                    asset=asset,
                    status="skipped_oversized_rug",
                    reason="oversized_rug",
                )
            )
            continue

        if uid in support_dependent_uids and _place_on_support_surface(
            uid,
            asset,
            layout,
            asset_map,
            outcomes,
        ):
            continue

        asset_blockers = (
            full_blockers
            if requires_window_clearance(category, uid, z=z)
            else door_only_blockers
        )
        if not rug_like:
            asset_blockers = [*asset_blockers, *protected_path_blockers]
        guided_candidates = _guided_candidates(
            uid,
            asset,
            room_area,
            layout,
            asset_map,
            seed_plan,
        )
        preferred_walls = _dynamic_preferred_walls(
            uid,
            placement_mode,
            layout,
            asset_map,
            seed_plan,
        )
        search_candidates = _dedupe_candidates(
            guided_candidates
            + _placement_candidates(
                asset,
                room_area,
                wall_offset,
                preferred_walls=preferred_walls,
                placement_mode=placement_mode,
            )
        )
        target = _placement_target(
            placement_mode=placement_mode,
            width=width,
            depth=depth,
            room_area=room_area,
            layout=layout,
            asset_map=asset_map,
            seed_plan=seed_plan,
        )
        placed, fallback = _search_placement(
            search_candidates=search_candidates,
            width=width,
            depth=depth,
            z=z,
            rug_like=rug_like,
            room_cover=room_cover,
            room_area=room_area,
            occupied=occupied,
            blockers=asset_blockers,
            target=target,
            placement_mode=placement_mode,
        )
        if placed is not None:
            center, rotation_z, poly, status, clearance = placed
            layout[uid] = {
                "category": category,
                "position": [float(center[0]), float(center[1]), float(z)],
                "rotation": [0.0, 0.0, float(rotation_z)],
            }
            if not rug_like:
                occupied.append(poly)
            wall_offset += 1
            outcomes.append(
                _placement_outcome(
                    uid=uid,
                    asset=asset,
                    status=status,
                    clearance=clearance,
                )
            )
        else:
            blocker_overlap, asset_overlap = fallback
            reason = _skip_reason(blocker_overlap, asset_overlap)
            outcomes.append(
                _placement_outcome(
                    uid=uid,
                    asset=asset,
                    status=f"skipped_{reason}",
                    reason=reason,
                    blocker_overlap=blocker_overlap,
                    asset_overlap=asset_overlap,
                )
            )

    report = {
        "placed_comfortably": [
            item["uid"] for item in outcomes if item.get("status") == "placed_comfortably"
        ],
        "placed_tightly": [
            item["uid"] for item in outcomes if item.get("status") == "placed_tightly"
        ],
        "skipped": [
            item for item in outcomes if str(item.get("status", "")).startswith("skipped_")
        ],
        "outcomes": outcomes,
    }
    return layout, report
