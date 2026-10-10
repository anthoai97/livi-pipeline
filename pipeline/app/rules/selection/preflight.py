"""Deterministic room-fit and media-support preflight checks."""

from typing import Any
from app.rules.geometry.primitives import room_polygon
from app.rules.layout.dining import dining_fit_envelopes
from app.rules.layout.constants import DINING_LIGHT_TABLE_MIN_GAP_M
from app.rules.categories import is_ceiling_mounted_asset
from app.rules.pipeline_shared import ceiling_mount_z

from app.rules.geometry.living_group import (
    canonical_living_group_poses,
    oriented_living_group_bounds,
)
from app.rules.geometry.candidates import (
    _requires_support_surface,
    _support_surface_eligible,
    _support_surface_fits,
)
from app.rules.geometry.primitives import media_display_fits_support_dimensions
from app.rules.planner.taxonomy import normalize_category
from app.rules.layout_rules import SOFA_COFFEE_TABLE_DISTANCE_RANGE_M

from app.rules.selection.catalog import (
    _asset_matches_any,
    _asset_uid,
    _asset_width_depth,
    _normalize_asset_text,
)
from app.rules.selection.constants import (
    BUDGET_FLEX_PCT,
    DESK_CATEGORIES,
    DINING_CHAIR_CATEGORIES,
    DINING_TABLE_CATEGORIES,
    FIT_ACCENT_SEATING,
    FIT_ANCHOR_SEATING,
    FIT_LIGHTING,
    FIT_RUGS,
    LAYOUT_PREFLIGHT_PREFIX,
    LAYOUT_RUG_CATEGORIES,
    MEDIA_DISPLAY_CATEGORIES,
    REQUESTED_ROLE_HARD_CATEGORIES,
    REQUESTED_ROLE_PREFIX,
    STORAGE_CATEGORIES,
    TASK_CHAIR_CATEGORIES,
    TV_COMPATIBLE_SUPPORT_CATEGORIES,
    TV_COMPATIBLE_SUPPORT_KEYWORDS,
    TV_HOST_CATEGORIES,
)


def _room_preflight_dimensions(room: dict[str, Any]) -> tuple[float, float, float, float]:
    room_area = room.get("room_area") or ()
    try:
        room_width = float(room_area[0])
        room_depth = float(room_area[1])
    except (TypeError, ValueError, IndexError):
        room_width = room_depth = 0.0
    margin = 0.08
    return (
        room_width,
        room_depth,
        max(0.0, room_width - 2 * margin),
        max(0.0, room_depth - 2 * margin),
    )

def _protected_path_zones(room: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the usable rectangles on either side of each full-room path."""
    room_width, room_depth, _, _ = _room_preflight_dimensions(room)
    if room_width <= 0 or room_depth <= 0:
        return []
    paths = room.get("protected_paths")

    result = []
    for path in paths or []:
        center = path.get("center") or [room_width / 2.0, room_depth / 2.0]
        axis = str(path.get("axis") or "")
        if axis == "x":
            half_depth = float(path.get("depth") or 0.0) / 2.0
            lower_depth = max(0.0, float(center[1]) - half_depth)
            upper_start = float(center[1]) + half_depth
            upper_depth = max(
                0.0,
                room_depth - upper_start,
            )
            zones = [
                {
                    "name": "below_path",
                    "width": room_width,
                    "depth": lower_depth,
                    "bounds": [0.0, 0.0, room_width, lower_depth],
                },
                {
                    "name": "above_path",
                    "width": room_width,
                    "depth": upper_depth,
                    "bounds": [0.0, upper_start, room_width, room_depth],
                },
            ]
        elif axis == "y":
            half_width = float(path.get("width") or 0.0) / 2.0
            left_width = max(0.0, float(center[0]) - half_width)
            right_start = float(center[0]) + half_width
            right_width = max(
                0.0,
                room_width - right_start,
            )
            zones = [
                {
                    "name": "left_of_path",
                    "width": left_width,
                    "depth": room_depth,
                    "bounds": [0.0, 0.0, left_width, room_depth],
                },
                {
                    "name": "right_of_path",
                    "width": right_width,
                    "depth": room_depth,
                    "bounds": [right_start, 0.0, room_width, room_depth],
                },
            ]
        else:
            continue
        result.append(
            {
                "path_id": str(path.get("id") or ""),
                "axis": axis,
                "zones": zones,
            }
        )
    return result

def _fits_rect(width: float, depth: float, room_width: float, room_depth: float) -> bool:
    return (
        width <= room_width
        and depth <= room_depth
    ) or (
        depth <= room_width
        and width <= room_depth
    )


def _is_living_accent_seating(asset: dict[str, Any]) -> bool:
    """Use resolved seating semantics before falling back to catalog taxonomy."""
    is_accent_category = (
        _asset_matches_any(
            asset,
            FIT_ACCENT_SEATING,
            keywords=("accent chair", "lounge chair", "armchair"),
        )
        and not _asset_matches_any(asset, FIT_ANCHOR_SEATING)
        and not _asset_matches_any(
            asset,
            TASK_CHAIR_CATEGORIES,
            keywords=("office chair", "task chair"),
        )
        and normalize_category(asset.get("category")) != "dining_chair"
    )
    if not is_accent_category:
        return False
    functional_role = str(asset.get("functional_role") or "").strip().lower()
    if functional_role:
        return functional_role == "living_seating"
    return True


def _requested_role_validation_errors(count_guidance: dict[str, Any]) -> list[str]:
    metrics = count_guidance.get("metrics") or {}
    constraints = metrics.get("explicit_count_constraints") or {}
    category_counts = metrics.get("category_counts") or {}

    def selected_role_count(category: str, selected: int) -> int:
        if category in FIT_ANCHOR_SEATING:
            return max(
                selected,
                sum(int(category_counts.get(candidate, 0)) for candidate in FIT_ANCHOR_SEATING),
            )
        if category in FIT_LIGHTING:
            return max(
                selected,
                sum(int(category_counts.get(candidate, 0)) for candidate in FIT_LIGHTING),
            )
        if category in FIT_RUGS:
            return max(
                selected,
                sum(int(category_counts.get(candidate, 0)) for candidate in FIT_RUGS),
            )
        if category in TV_HOST_CATEGORIES:
            return max(
                selected,
                sum(int(category_counts.get(candidate, 0)) for candidate in TV_HOST_CATEGORIES),
            )
        if category in STORAGE_CATEGORIES:
            return max(
                selected,
                sum(int(category_counts.get(candidate, 0)) for candidate in STORAGE_CATEGORIES),
            )
        return selected

    errors: list[str] = []
    for category, constraint in constraints.items():
        if not isinstance(constraint, dict):
            continue
        normalized = normalize_category(category)
        if normalized not in REQUESTED_ROLE_HARD_CATEGORIES:
            continue
        if bool(constraint.get("optional")):
            continue
        requested = int(constraint.get("count") or 0)
        selected = selected_role_count(normalized, int(constraint.get("selected") or 0))
        if requested <= 0 or selected >= requested:
            continue
        substitutes = constraint.get("acceptable_substitutes") or []
        substitute_text = (
            f" Acceptable substitutes: {', '.join(str(item) for item in substitutes)}."
            if substitutes
            else ""
        )
        errors.append(
            f"{REQUESTED_ROLE_PREFIX}: User requested {requested} {normalized}; "
            f"selected {selected}.{substitute_text} Add the requested role or "
            "select a valid substitute from the requested item's slot."
        )
    return errors


def _canonical_living_arrangement_footprints(
    anchor: dict[str, Any],
    table: dict[str, Any],
    accent_seating: list[dict[str, Any]],
) -> list[dict[str, float | str]]:
    """Measure the solver's canonical poses with exact projected min/max bounds."""
    sofa_key = "anchor"
    table_key = "coffee_table"
    chair_keys = [f"accent_{index}" for index in range(len(accent_seating))]
    dimensions = {
        sofa_key: _asset_width_depth(anchor),
        table_key: _asset_width_depth(table),
        **{
            key: _asset_width_depth(chair)
            for key, chair in zip(chair_keys, accent_seating, strict=True)
        },
    }
    minimum_gap = SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]
    canonical_gap = sum(SOFA_COFFEE_TABLE_DISTANCE_RANGE_M) / 2.0
    canonical_poses = canonical_living_group_poses(
        sofa_uid=sofa_key,
        coffee_table_uid=table_key,
        accent_seat_uids=chair_keys,
        dimensions=dimensions,
        sofa_table_gaps=[minimum_gap, canonical_gap],
    )
    arrangements: list[dict[str, float | str]] = []
    seen: set[tuple[str, float, float]] = set()
    for raw_name, relative_poses in canonical_poses:
        _, bounds = oriented_living_group_bounds(relative_poses, dimensions, 0)
        min_x, min_y, max_x, max_y = bounds
        name = (
            "sofa_table"
            if not accent_seating
            else (
                "one_side_rest_opposite"
                if raw_name.startswith("one_side_")
                else (
                    "chairs_both_sides"
                    if raw_name.startswith("both_sides_")
                    else raw_name
                )
            )
        )
        width = max_x - min_x
        depth = max_y - min_y
        signature = (name, round(width, 9), round(depth, 9))
        if signature in seen:
            continue
        seen.add(signature)
        arrangements.append({"name": name, "width": width, "depth": depth})
    return arrangements


def _layout_preflight_for_assets(
    *,
    selected_assets: list[dict[str, Any]],
    room: dict[str, Any],
    budget: float,
    total_cost: float,
    fit_checks: bool = True,
) -> dict[str, Any]:
    """Physical layout checks for a selection. fit_checks=False skips the size
    estimates the code solver settles by placing the selection: anchor seating,
    bed, and rug against the room clear area, tabletop items against their
    supports, and desk and dining clusters against the room clear area.
    support_fit_errors holds the errors (also in errors) for a tabletop item
    too large for every eligible selected support."""
    room_width, room_depth, clear_width, clear_depth = _room_preflight_dimensions(room)
    room_area_sqm = room_polygon((room_width, room_depth), room.get("room_vertices")).area
    protected_path_zones = _protected_path_zones(room)
    errors: list[str] = []
    support_fit_errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "room_width": round(room_width, 3),
        "room_depth": round(room_depth, 3),
        "clear_width": round(clear_width, 3),
        "clear_depth": round(clear_depth, 3),
        "budget_ratio": round(total_cost / budget, 3) if budget else None,
        "anchor_seating": [],
        "rugs": [],
        "support_requirements": [],
        "clusters": [],
        "protected_path_zones": protected_path_zones,
        "tiny_room": room_area_sqm < 10.0,
    }

    anchor_seating = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, FIT_ANCHOR_SEATING, keywords=("sofa", "loveseat", "sectional"))
    ]
    for anchor in anchor_seating:
        width, depth = _asset_width_depth(anchor)
        fits = _fits_rect(width, depth, clear_width, clear_depth)
        fill_ratio = (width * depth / room_area_sqm) if room_area_sqm else 0.0
        metrics["anchor_seating"].append(
            {
                "uid": _asset_uid(anchor),
                "width": round(width, 3),
                "depth": round(depth, 3),
                "fits_room_clear": fits,
                "fill_ratio": round(fill_ratio, 3),
            }
        )
        fits_protected_zones = all(
            any(
                _fits_rect(
                    width,
                    depth,
                    max(0.0, float(zone["width"]) - 0.08),
                    max(0.0, float(zone["depth"]) - 0.08),
                )
                for zone in path["zones"]
            )
            for path in protected_path_zones
        )
        if not fits and fit_checks:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: anchor seating {_asset_uid(anchor)} "
                f"is {width:.2f}m x {depth:.2f}m and cannot fit within the "
                f"{clear_width:.2f}m x {clear_depth:.2f}m room clear area. "
                "Select a smaller sofa/loveseat."
            )
        elif room_area_sqm < 10.0 and (
            width > clear_width * 0.82
            or depth > clear_depth * 0.55
            or fill_ratio > 0.26
        ):
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: anchor seating {_asset_uid(anchor)} "
                f"is too large for a {room_area_sqm:.1f}m2 tiny room. Select a "
                "compact loveseat or apartment sofa."
            )
        elif protected_path_zones and not fits_protected_zones:
            zone_summary = [
                (
                    path["path_id"],
                    [
                        [
                            round(float(zone["width"]), 2),
                            round(float(zone["depth"]), 2),
                        ]
                        for zone in path["zones"]
                    ],
                )
                for path in protected_path_zones
            ]
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: anchor seating {_asset_uid(anchor)} "
                f"is {width:.2f}m x {depth:.2f}m and cannot fit wholly on either "
                f"side of the required circulation path(s) {zone_summary}. Select "
                "a smaller sofa/loveseat whose footprint fits a usable path zone."
            )

    beds = [asset for asset in selected_assets if normalize_category(asset.get("category")) == "bed"]
    if beds:
        metrics["sleeping_anchor"] = []
    for bed in beds:
        width, depth = _asset_width_depth(bed)
        fits = _fits_rect(width, depth, clear_width, clear_depth)
        metrics["sleeping_anchor"].append({
            "uid": _asset_uid(bed), "width": width, "depth": depth,
            "fits_room_clear": fits,
        })
        if not fits and fit_checks:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: complete bed {_asset_uid(bed)} is "
                f"{width:.2f}m x {depth:.2f}m and cannot fit within the "
                f"{clear_width:.2f}m x {clear_depth:.2f}m room clear area. Select a smaller complete bed."
            )

    rugs = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, LAYOUT_RUG_CATEGORIES, keywords=("rug", "carpet"))
    ]
    for rug in rugs:
        width, depth = _asset_width_depth(rug)
        fits = _fits_rect(width, depth, clear_width, clear_depth)
        fill_ratio = (width * depth / room_area_sqm) if room_area_sqm else 0.0
        rug_metric = {
            "uid": _asset_uid(rug),
            "width": round(width, 3),
            "depth": round(depth, 3),
            "fits_room_clear": fits,
            "fill_ratio": round(fill_ratio, 3),
        }
        metrics["rugs"].append(rug_metric)
        if not fits and fit_checks:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: rug {_asset_uid(rug)} is "
                f"{width:.2f}m x {depth:.2f}m and cannot fit within the "
                f"{clear_width:.2f}m x {clear_depth:.2f}m room clear area. "
                "Select a smaller rug."
            )
        elif room_area_sqm < 10.0 and (
            fill_ratio > 0.72
            and width > clear_width * 0.92
            and depth > clear_depth * 0.92
        ):
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: rug {_asset_uid(rug)} is too large "
                f"for a {room_area_sqm:.1f}m2 tiny room. Select a smaller rug "
                "that leaves visible floor around the furniture group."
            )
        elif room_area_sqm < 10.0 and (
            fill_ratio > 0.60
            or width > clear_width * 0.92
            or depth > clear_depth * 0.92
        ):
            warnings.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: rug {_asset_uid(rug)} is large "
                f"for a {room_area_sqm:.1f}m2 tiny room. Prefer a smaller rug "
                "if a compact option is available."
            )

    for child in selected_assets:
        child_uid = _asset_uid(child)
        if not child_uid or not _requires_support_surface(child_uid, child):
            continue
        if _asset_matches_any(
            child,
            MEDIA_DISPLAY_CATEGORIES,
            keywords=("television", "monitor"),
        ):
            # Media pairing below uses its dedicated tolerance contract.
            continue
        eligible_supports = [
            support
            for support in selected_assets
            if _support_surface_eligible(
                child_uid,
                child,
                _asset_uid(support),
                support,
            )
        ]
        explicit_support = next(
            (
                support
                for field in ("paired_support_uid", "support_uid")
                if (explicit_uid := str(child.get(field) or ""))
                for support in eligible_supports
                if _asset_uid(support) == explicit_uid
            ),
            None,
        )
        support_candidates = (
            [explicit_support] if explicit_support is not None else eligible_supports
        )
        fitting_supports = [
            support
            for support in support_candidates
            if _support_surface_fits(
                child,
                *_asset_width_depth(support),
            )
        ]
        child_width, child_depth = _asset_width_depth(child)
        metrics["support_requirements"].append(
            {
                "uid": child_uid,
                "placement_mode": "tabletop",
                "width": round(child_width, 3),
                "depth": round(child_depth, 3),
                "eligible_supports": [
                    {
                        "uid": _asset_uid(support),
                        "width": round(_asset_width_depth(support)[0], 3),
                        "depth": round(_asset_width_depth(support)[1], 3),
                    }
                    for support in support_candidates
                ],
                "fitting_support_uids": [
                    _asset_uid(support) for support in fitting_supports
                ],
            }
        )
        if not support_candidates:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: tabletop asset {child_uid} "
                f"({child_width:.2f}m x {child_depth:.2f}m) has no selected "
                "eligible support surface. Add a valid support or replace/remove "
                "the tabletop asset."
            )
        elif not fitting_supports and fit_checks:
            support_summary = [
                {
                    "uid": _asset_uid(support),
                    "width": round(_asset_width_depth(support)[0], 2),
                    "depth": round(_asset_width_depth(support)[1], 2),
                }
                for support in support_candidates
            ]
            support_fit_errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: tabletop asset {child_uid} "
                f"({child_width:.2f}m x {child_depth:.2f}m) does not fit any "
                f"selected eligible support after layout inset: {support_summary}. "
                "Keep both items: select a smaller tabletop asset or a wider/deeper support from the same slots."
            )
            errors.append(support_fit_errors[-1])

    coffee_tables = [
        asset
        for asset in selected_assets
        if normalize_category(asset.get("category")) == "coffee_table"
    ]
    accent_seating = [
        asset
        for asset in selected_assets
        if _is_living_accent_seating(asset)
    ]
    if anchor_seating and coffee_tables and protected_path_zones:
        table = min(
            coffee_tables,
            key=lambda asset: (
                _asset_width_depth(asset)[0] * _asset_width_depth(asset)[1]
            ),
        )
        minimum_gap = SOFA_COFFEE_TABLE_DISTANCE_RANGE_M[0]
        canonical_gap = sum(SOFA_COFFEE_TABLE_DISTANCE_RANGE_M) / 2.0
        for anchor in anchor_seating:
            arrangements = _canonical_living_arrangement_footprints(
                anchor,
                table,
                accent_seating,
            )

            feasible_path_placements = []
            for path in protected_path_zones:
                for zone in path["zones"]:
                    usable_width = max(0.0, float(zone["width"]) - 0.08)
                    usable_depth = max(0.0, float(zone["depth"]) - 0.08)
                    for arrangement in arrangements:
                        orientations = []
                        if (
                            arrangement["width"] <= usable_width
                            and arrangement["depth"] <= usable_depth
                        ):
                            orientations.append("as_dimensioned")
                        if (
                            arrangement["depth"] <= usable_width
                            and arrangement["width"] <= usable_depth
                        ):
                            orientations.append("quarter_turn")
                        if orientations:
                            feasible_path_placements.append(
                                {
                                    "path_id": path["path_id"],
                                    "zone": zone["name"],
                                    "zone_bounds": zone["bounds"],
                                    "arrangement": arrangement["name"],
                                    "orientations": orientations,
                                }
                            )
            fits_protected_zones = all(
                any(
                    placement["path_id"] == path["path_id"]
                    for placement in feasible_path_placements
                )
                for path in protected_path_zones
            )
            smallest_arrangement = min(
                arrangements,
                key=lambda item: (
                    min(item["width"], item["depth"]),
                    item["width"] * item["depth"],
                ),
            )
            metrics["clusters"].append(
                {
                    "type": "living_seating",
                    "sofa_uid": _asset_uid(anchor),
                    "coffee_table_uid": _asset_uid(table),
                    "accent_seat_uids": [
                        _asset_uid(chair) for chair in accent_seating
                    ],
                    "width": round(smallest_arrangement["width"], 3),
                    "depth": round(smallest_arrangement["depth"], 3),
                    "minimum_edge_gap": minimum_gap,
                    "canonical_edge_gap": round(canonical_gap, 3),
                    "canonical_edge_gaps": [
                        round(minimum_gap, 3),
                        round(canonical_gap, 3),
                    ],
                    "arrangements": [
                        {
                            "name": arrangement["name"],
                            "width": round(arrangement["width"], 3),
                            "depth": round(arrangement["depth"], 3),
                        }
                        for arrangement in arrangements
                    ],
                    "fits_protected_path_zone": fits_protected_zones,
                    "feasible_path_placements": feasible_path_placements,
                }
            )
            if not fits_protected_zones:
                chair_summary = [
                    {
                        "uid": uid,
                        "width": round(width, 2),
                        "depth": round(depth, 2),
                    }
                    for width, depth, uid in (
                        (*_asset_width_depth(chair), _asset_uid(chair))
                        for chair in accent_seating
                    )
                ]
                arrangement_summary = [
                    {
                        "name": arrangement["name"],
                        "width": round(arrangement["width"], 2),
                        "depth": round(arrangement["depth"], 2),
                    }
                    for arrangement in arrangements
                ]
                zone_summary = [
                    {
                        "path_id": path["path_id"],
                        "usable_zones": [
                            [
                                round(max(0.0, float(zone["width"]) - 0.08), 2),
                                round(max(0.0, float(zone["depth"]) - 0.08), 2),
                            ]
                            for zone in path["zones"]
                        ],
                    }
                    for path in protected_path_zones
                ]
                errors.append(
                    f"{LAYOUT_PREFLIGHT_PREFIX}: complete living group "
                    f"{_asset_uid(anchor)} + {_asset_uid(table)} + chairs "
                    f"{chair_summary} has canonical footprints "
                    f"{arrangement_summary}, but usable circulation-path zones are "
                    f"{zone_summary}. It cannot fit wholly in one path zone "
                    "in any canonical conversation arrangement while keeping "
                    "the sofa and coffee-table axes aligned. Select narrower accent "
                    "chairs and/or a more compact sofa/coffee table without changing "
                    "the requested chair count."
                )

    desks = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, DESK_CATEGORIES, keywords=("desk",))
    ]
    task_chairs = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, TASK_CHAIR_CATEGORIES, keywords=("office chair", "task chair"))
    ]
    if desks and task_chairs:
        desk = max(desks, key=lambda asset: _asset_width_depth(asset)[0] * _asset_width_depth(asset)[1])
        chair = max(task_chairs, key=lambda asset: _asset_width_depth(asset)[0] * _asset_width_depth(asset)[1])
        desk_width, desk_depth = _asset_width_depth(desk)
        chair_width, chair_depth = _asset_width_depth(chair)
        cluster_width = max(desk_width, chair_width + 0.35)
        cluster_depth = desk_depth + chair_depth + 0.65
        fits = _fits_rect(cluster_width, cluster_depth, clear_width, clear_depth)
        metrics["clusters"].append(
            {
                "type": "desk_task_chair",
                "table_uid": _asset_uid(desk),
                "chair_uid": _asset_uid(chair),
                "width": round(cluster_width, 3),
                "depth": round(cluster_depth, 3),
                "fits_room_clear": fits,
            }
        )
        if not fits and fit_checks:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: desk/task-chair cluster needs about "
                f"{cluster_width:.2f}m x {cluster_depth:.2f}m and cannot fit "
                f"within the {clear_width:.2f}m x {clear_depth:.2f}m room clear area."
            )

    dining_tables = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, DINING_TABLE_CATEGORIES, keywords=("dining table", "bar table"))
    ]
    dining_chairs = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, DINING_CHAIR_CATEGORIES, keywords=("dining chair",))
        and not _asset_matches_any(asset, FIT_ANCHOR_SEATING)
        and not _asset_matches_any(asset, TASK_CHAIR_CATEGORIES, keywords=("office chair", "task chair"))
    ]
    if dining_tables and dining_chairs:
        table = max(dining_tables, key=lambda asset: _asset_width_depth(asset)[0] * _asset_width_depth(asset)[1])
        table_width, table_depth = _asset_width_depth(table)
        chair_count = len(dining_chairs)
        options = dining_fit_envelopes(table, dining_chairs)
        fitting_options = [option for option in options
                           if _fits_rect(option["width"], option["depth"], clear_width, clear_depth)]
        envelope = next(iter(fitting_options or options), {"width": table_width, "depth": table_depth})
        cluster_width, cluster_depth = envelope["width"], envelope["depth"]
        fits = bool(fitting_options)
        cluster_area = cluster_width * cluster_depth
        metrics["clusters"].append(
            {
                "type": "dining",
                "table_uid": _asset_uid(table),
                "chair_count": chair_count,
                "width": round(cluster_width, 3),
                "depth": round(cluster_depth, 3),
                "fits_room_clear": fits,
                "seating_envelopes": options[:8],
            }
        )
        if not options:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: dining table {_asset_uid(table)} has insufficient "
                f"edge length for {chair_count} selected chairs at usable seat spacing; choose a larger "
                "table or narrower chairs while preserving mandatory seating counts."
            )
        elif not fits and fit_checks:
            errors.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: dining table/chair cluster needs about "
                f"{cluster_width:.2f}m x {cluster_depth:.2f}m and cannot fit "
                f"within the {clear_width:.2f}m x {clear_depth:.2f}m room clear area."
            )
        elif room_area_sqm and cluster_area > room_area_sqm * 0.55:
            warnings.append(
                f"{LAYOUT_PREFLIGHT_PREFIX}: dining cluster uses "
                f"{cluster_area / room_area_sqm:.0%} of the room footprint; "
                "select smaller dining pieces if circulation is tight."
            )

    if dining_tables and room.get("room_type") == "dining_room":
        tabletop_z = max(float(table.get("height") or 0) for table in dining_tables)
        for asset in selected_assets:
            if not is_ceiling_mounted_asset(asset.get("category", ""), asset.get("uid", "")):
                continue
            gap = ceiling_mount_z(float(asset.get("height") or 0)) - tabletop_z
            if gap < DINING_LIGHT_TABLE_MIN_GAP_M - 1e-6:
                errors.append(
                    f"{LAYOUT_PREFLIGHT_PREFIX}: dining light {_asset_uid(asset)} leaves only {gap:.2f}m "
                    f"above the tabletop; at least {DINING_LIGHT_TABLE_MIN_GAP_M:.2f}m is required. "
                    "Choose a shorter model or omit this optional fixture. Its hanger cannot be shortened by layout."
                )

    floor_lamps = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, FIT_LIGHTING, keywords=("floor lamp",))
        and "floor" in _normalize_asset_text(_asset_uid(asset) + " " + str(asset.get("category") or ""))
    ]
    seats = [
        asset for asset in selected_assets
        if _asset_matches_any(asset, FIT_ANCHOR_SEATING | FIT_ACCENT_SEATING, keywords=("chair", "sofa", "loveseat"))
    ]
    if room_area_sqm < 10.0:
        if len(floor_lamps) > 1:
            errors.append(
                f"OVER CROWDED: Tiny room has {len(floor_lamps)} floor lamps; "
                "keep at most one slim floor lamp or use a table lamp."
            )
        if len(seats) > 2:
            errors.append(
                f"OVER CROWDED: Tiny room has {len(seats)} seating pieces; "
                "keep a compact sofa/loveseat and at most one secondary seat."
            )
    elif room_area_sqm < 14.0 and len(floor_lamps) > max(1, len(seats)):
        warnings.append(
            f"{LAYOUT_PREFLIGHT_PREFIX}: {len(floor_lamps)} floor lamps for "
            f"{len(seats)} seating pieces may be hard to service without blocking circulation."
        )

    if budget and total_cost / budget > 1 + BUDGET_FLEX_PCT:
        warnings.append(
            f"BUDGET RANGE: Selection uses {total_cost / budget:.0%} of budget; "
            f"the allowed cap is {1 + BUDGET_FLEX_PCT:.0%}."
        )

    metrics["status"] = "PASS" if not errors else "FAIL"
    metrics["errors"] = errors
    metrics["warnings"] = warnings
    return {
        "errors": errors,
        "support_fit_errors": support_fit_errors,
        "warnings": warnings,
        "metrics": metrics,
    }

def _tv_fits_on_support(tv: dict, support: dict) -> bool:
    tv_width, tv_depth = _asset_width_depth(tv)
    support_width, support_depth = _asset_width_depth(support)
    return media_display_fits_support_dimensions(
        tv_width,
        tv_depth,
        support_width,
        support_depth,
    )

def _tv_support_in_selection(tv: dict, selected_assets: list[dict]) -> dict | None:
    """Return a selected support on which the selected TV physically fits."""
    supports = [
        asset
        for asset in selected_assets
        if _asset_matches_any(
            asset,
            TV_COMPATIBLE_SUPPORT_CATEGORIES,
            keywords=TV_COMPATIBLE_SUPPORT_KEYWORDS,
        )
        and _tv_fits_on_support(tv, asset)
    ]
    if not supports:
        return None
    paired_support_uid = str(tv.get("paired_support_uid") or "")
    return min(
        supports,
        key=lambda asset: (
            _asset_uid(asset) != paired_support_uid,
            normalize_category(asset.get("category")) not in TV_HOST_CATEGORIES,
            _asset_uid(asset),
        ),
    )

def _annotate_tv_placement(selected_assets: list[dict]) -> list[dict]:
    """Hydrate placement metadata without changing the model-selected UID set.

    A TV that fits a selected support stands on it (tabletop with
    paired_support_uid); any other TV is wall-mounted."""
    annotated: list[dict] = []
    for asset in selected_assets:
        if normalize_category(asset.get("category")) != "tv":
            annotated.append(asset)
            continue
        placed = {**asset}
        placement_mode = str(placed.get("placement_mode") or "").strip().lower()
        if placement_mode in {"floor", "wall_mounted", "ceiling_mounted"}:
            placed.pop("paired_support_uid", None)
            placed.pop("support_uid", None)
            annotated.append(placed)
            continue
        support = _tv_support_in_selection(asset, selected_assets)
        if support:
            placed["placement_mode"] = "tabletop"
            placed["paired_support_uid"] = _asset_uid(support)
        else:
            placed["placement_mode"] = "wall_mounted"
            placed.pop("paired_support_uid", None)
        annotated.append(placed)
    return annotated
