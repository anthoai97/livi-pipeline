"""Aggregate deterministic layout analysis."""

from typing import Any
from app.rules.layout.bedroom import bedroom_layout_measurements
from app.rules.layout.dining import dining_layout_measurements
from app.rules.layout.studio import studio_layout_measurements
from app.rules.layout.comfort import comfort_layout_measurements
from app.rules.layout.constants import (
    CRITICAL_LIVING_GROUP_KINDS,
    CRITICAL_P2_ISSUE_KEYS,
    ISSUE_KEYS_BY_TIER,
)
from app.rules.layout.metrics import has_blocking_issues, issue_counts, serialize_layout_issues

from app.rules.layout.validation_functional import (
    compute_coffee_table_axis_violations,
    compute_dining_group_violations,
    compute_floor_lamp_reach_violations,
    compute_living_dining_clearance_violations,
    compute_living_group_cohesion_violations,
    compute_media_focal_alignment_violations,
    compute_media_group_violations,
    compute_service_corridor_violations,
    compute_side_table_reach_violations,
    compute_sofa_coffee_table_violations,
    compute_sofa_wall_gap_violations,
    compute_table_lamp_support_violations,
    compute_task_chair_orientation_violations,
)
from app.rules.layout.validation_geometry import (
    compute_boundary_violations,
    compute_ceiling_mount_violations,
    compute_door_violations,
    compute_obstacle_violations,
    compute_overlaps,
    compute_protected_path_violations,
    compute_wall_aligned_violations,
    compute_wall_mount_violations,
)

def analyze_layout(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    boundary: list[list[float]],
    room_doors: list[dict[str, Any]],
    room_area: tuple[float, float],
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
    tiers: tuple[str, ...] = ("P0", "P1", "P2"),
    room_type: str = "living_room",
) -> dict[str, Any]:
    """Measure a layout. Returns every issue key, each holding its findings (empty when clean)."""
    issues = {
        "overlaps": [],
        "boundary_violations": [],
        "door_violations": [],
        "obstacle_violations": [],
        "wall_mount_violations": [],
        "ceiling_mount_violations": [],
        "wall_aligned_violations": [],
        "protected_path_violations": [],
        "sofa_coffee_table_violations": [],
        "sofa_wall_gap_violations": [],
        "media_focal_alignment_violations": [],
        "media_group_violations": [],
        "rug_composition_violations": [],
        "coffee_table_axis_violations": [],
        "living_group_cohesion_violations": [],
        "dining_group_violations": [],
        "living_dining_clearance_violations": [],
        "task_chair_orientation_violations": [],
        "side_table_reach_violations": [],
        "floor_lamp_reach_violations": [],
        "table_lamp_support_violations": [],
        "service_corridor_violations": [],
    }
    if "P0" in tiers:
        issues["overlaps"] = compute_overlaps(layout, assets)
        issues["boundary_violations"] = compute_boundary_violations(
            layout,
            assets,
            boundary,
        )
        issues["door_violations"] = compute_door_violations(
            layout,
            assets,
            room_doors,
            boundary,
            room_area,
        )
        issues["obstacle_violations"] = compute_obstacle_violations(
            layout,
            assets,
            room_windows,
            boundary,
            room_area,
        )
        issues["wall_mount_violations"] = compute_wall_mount_violations(
            layout,
            assets,
            boundary,
        )
        issues["ceiling_mount_violations"] = compute_ceiling_mount_violations(
            layout,
            assets,
        )
    if "P1" in tiers:
        issues["wall_aligned_violations"] = compute_wall_aligned_violations(
            layout,
            assets,
            boundary,
        )
        issues["protected_path_violations"] = compute_protected_path_violations(
            layout,
            assets,
            protected_paths or [],
        )
    if "P2" in tiers:
        if room_type in {"bedroom", "studio"}:
            issues.update(bedroom_layout_measurements(layout, assets, boundary, room_area, mixed_groups=room_type == "studio")["violations"])
        if room_type in {"dining_room", "studio"}:
            issues.update(dining_layout_measurements(layout, assets, boundary, room_area, mixed_groups=room_type == "studio")["violations"])
        if room_type == "studio":
            issues.update(studio_layout_measurements(layout, assets)["violations"])
        issues["sofa_coffee_table_violations"] = compute_sofa_coffee_table_violations(
            layout,
            assets,
        )
        issues["sofa_wall_gap_violations"] = compute_sofa_wall_gap_violations(
            layout,
            assets,
            boundary,
            room_windows,
        )
        issues["media_focal_alignment_violations"] = (
            compute_media_focal_alignment_violations(layout, assets, boundary) if room_type != "studio" else []
        )
        issues["media_group_violations"] = compute_media_group_violations(
            layout,
            assets,
        )
        issues["coffee_table_axis_violations"] = compute_coffee_table_axis_violations(
            layout,
            assets,
        )
        issues["living_group_cohesion_violations"] = (
            compute_living_group_cohesion_violations(layout, assets, mixed_groups=room_type == "studio")
        )
        issues["dining_group_violations"] = compute_dining_group_violations(
            layout,
            assets,
        )
        issues["living_dining_clearance_violations"] = (
            compute_living_dining_clearance_violations(layout, assets)
        )
        issues["task_chair_orientation_violations"] = (
            compute_task_chair_orientation_violations(layout, assets)
        )
        issues["side_table_reach_violations"] = compute_side_table_reach_violations(
            layout,
            assets,
        )
        issues["floor_lamp_reach_violations"] = compute_floor_lamp_reach_violations(
            layout,
            assets,
            boundary,
        )
        issues["table_lamp_support_violations"] = (
            compute_table_lamp_support_violations(layout, assets)
        )
        issues["service_corridor_violations"] = (
            compute_service_corridor_violations(layout, assets)
        )
        for key, findings in comfort_layout_measurements(layout, assets, room_type, boundary=boundary)["violations"].items():
            issues[key].extend(findings)
    return issues


def findings_by_level(issues: dict[str, Any]) -> dict[str, dict[str, list[Any]]]:
    """Group non-empty analyze_layout findings into P0, P1, P2, and critical_p2.

    critical_p2 holds the P2 findings that block a layout. Values are JSON-ready.
    """
    serialized = serialize_layout_issues(issues)
    levels = {
        level: {key: serialized[key] for key in keys if serialized.get(key)}
        for level, keys in ISSUE_KEYS_BY_TIER.items()
    }
    critical = {key: list(serialized.get(key) or []) for key in CRITICAL_P2_ISSUE_KEYS}
    critical["living_group_cohesion_violations"] = [
        finding for finding in critical["living_group_cohesion_violations"]
        if str(finding.get("kind") or "") in CRITICAL_LIVING_GROUP_KINDS
    ]
    levels["critical_p2"] = {key: findings for key, findings in critical.items() if findings}
    return levels


def final_layout_check(
    layout: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    room_type: str,
    room_area: tuple[float, float],
    room_vertices: list[list[float]],
    room_doors: list[dict[str, Any]],
    room_windows: list[dict[str, Any]] | None = None,
    protected_paths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Final validation from render_scene: every selected instance placed exactly once and no blocking finding.

    assets are the selected instance records; layout is keyed by instance key.
    """
    expected = {asset.get("instance_key") or asset["uid"] for asset in assets}
    if set(layout) != expected:
        return {
            "valid": False,
            "errors": [
                "Layout must contain every selected instance exactly once: "
                f"missing={sorted(expected - set(layout))}, unknown={sorted(set(layout) - expected)}"
            ],
            "counts": {},
            "issues": {},
        }
    issues = analyze_layout(
        layout, assets, room_vertices, room_doors, tuple(room_area),
        room_windows=room_windows or [], protected_paths=protected_paths or [], room_type=room_type,
    )
    valid = not has_blocking_issues(issues)
    counts = issue_counts(issues)
    found = {key: count for key, count in counts.items() if count}
    return {
        "valid": valid,
        "errors": [] if valid else [f"Final layout has unresolved blocking issues: {found}"],
        "counts": counts,
        "issues": serialize_layout_issues(issues),
    }
