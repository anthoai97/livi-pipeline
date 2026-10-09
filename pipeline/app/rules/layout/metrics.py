"""Layout issue metrics, diffs, and serializable summaries."""

from typing import Any, NamedTuple
from shapely.geometry import Polygon
from app.rules.pipeline_shared import base_asset_uid
from app.rules.layout.bedroom import BEDROOM_ISSUE_KEYS
from app.rules.layout.studio import STUDIO_ISSUE_KEYS

from app.rules.layout.constants import (
    DINING_ISSUE_KEYS,
    CRITICAL_LIVING_GROUP_KINDS,
    CRITICAL_P2_ISSUE_KEYS,
    ISSUE_KEYS_BY_TIER,
)

def _issue_count_for_keys(
    issues: dict[str, Any],
    keys: tuple[str, ...],
) -> int:
    return sum(len(issues.get(key) or []) for key in keys)

def critical_p2_issue_count(issues: dict[str, Any]) -> int:
    """Count functional P2 violations that make a layout unusable."""
    count = _issue_count_for_keys(
        issues,
        tuple(
            key
            for key in CRITICAL_P2_ISSUE_KEYS
            if key != "living_group_cohesion_violations"
        ),
    )
    for violation in issues.get("living_group_cohesion_violations") or []:
        kind = str(violation.get("kind") or "")
        if kind in CRITICAL_LIVING_GROUP_KINDS:
            count += 1
    return count

def has_blocking_issues(issues: dict[str, Any]) -> bool:
    """True when any P0 or P1 finding, or a critical P2 finding, remains."""
    hard_issue_keys = (
        *ISSUE_KEYS_BY_TIER["P0"],
        *ISSUE_KEYS_BY_TIER["P1"],
    )
    return (
        any(issues.get(key) for key in hard_issue_keys)
        or critical_p2_issue_count(issues) > 0
    )

def layout_issue_score(issues: dict[str, Any]) -> tuple[int, int, int]:
    """Prioritize hard geometry, then functional P2, then all remaining P2."""
    return (
        _issue_count_for_keys(
            issues,
            ISSUE_KEYS_BY_TIER["P0"] + ISSUE_KEYS_BY_TIER["P1"],
        ),
        critical_p2_issue_count(issues),
        _issue_count_for_keys(issues, ISSUE_KEYS_BY_TIER["P2"]),
    )

def layout_issue_magnitudes(issues: dict[str, Any]) -> tuple[float, float, float]:
    """Continuous evidence for equal-count candidates; never relax a violation."""
    access_keys = ("bed_access_violations", "bed_workstation_violations", "storage_access_violations",
                   "dining_access_violations", "dining_storage_access_violations")
    rows = [row for key in access_keys for row in issues.get(key) or []]
    rows += [row for row in issues.get("living_group_cohesion_violations") or []
             if row.get("kind") == "sofa_console_access"]
    shortfall = sum(max(0.0, float(row.get("min_gap", 0)) - float(row.get("gap", 0))) for row in rows)
    viewing = []
    for row in issues.get("media_group_violations") or []:
        if row.get("kind") != "tv_viewing_distance":
            continue
        low, high = row["preferred_distance_range_m"]
        distance = row["estimated_view_distance_m"]
        viewing.append(max(0.0, (low - distance) / max(low, 1e-6), (distance - high) / max(high, 1e-6)))
    return round(shortfall, 5), round(max(viewing, default=0.0), 5), round(sum(viewing), 5)


def studio_comfort_score(issues: dict[str, Any]) -> tuple[float, float, int, int]:
    """Worst intended viewer first, then total distance, exposed media and polish."""
    _, worst_view, total_view = layout_issue_magnitudes(issues)
    floating = sum(row.get("kind") == "media_support_floating" for row in issues.get("media_group_violations") or [])
    return worst_view, total_view, floating, layout_issue_score(issues)[2]


def layout_change_summary(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    previous = before or {}
    current = after or {}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(
        uid for uid in set(previous) & set(current) if previous[uid] != current[uid]
    )
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "changed_count": len(changed),
        "added_count": len(added),
        "removed_count": len(removed),
    }

def layout_changes(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for uid, after_placement in after.items():
        before_placement = before.get(uid)
        if not before_placement:
            continue
        before_pos = list(before_placement.get("position", [0.0, 0.0, 0.0]))
        after_pos = list(after_placement.get("position", [0.0, 0.0, 0.0]))
        before_rot = list(before_placement.get("rotation", [0.0, 0.0, 0.0]))
        after_rot = list(after_placement.get("rotation", [0.0, 0.0, 0.0]))
        before_pos = (before_pos + [0.0, 0.0, 0.0])[:3]
        after_pos = (after_pos + [0.0, 0.0, 0.0])[:3]
        before_rot = (before_rot + [0.0, 0.0, 0.0])[:3]
        after_rot = (after_rot + [0.0, 0.0, 0.0])[:3]
        if any(
            abs(float(after_pos[i]) - float(before_pos[i])) > 1e-6 for i in range(3)
        ) or any(
            abs(float(after_rot[i]) - float(before_rot[i])) > 1e-6
            for i in range(3)
        ):
            changes.append(
                {
                    "uid": uid,
                    "from_position": [float(value) for value in before_pos],
                    "to_position": [float(value) for value in after_pos],
                    "from_rotation": [float(value) for value in before_rot],
                    "to_rotation": [float(value) for value in after_rot],
                }
            )
    return changes

def serialize_layout_issues(issues: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: list(issues.get(key, [])) for key in (*BEDROOM_ISSUE_KEYS, *DINING_ISSUE_KEYS, *STUDIO_ISSUE_KEYS)},
        "overlaps": [
            {"a": uid_a, "b": uid_b}
            for uid_a, uid_b in issues.get("overlaps", [])
        ],
        "boundary_violations": list(issues.get("boundary_violations", [])),
        "door_violations": [
            {"uid": uid, "door": door_idx}
            for uid, door_idx in issues.get("door_violations", [])
        ],
        "obstacle_violations": list(issues.get("obstacle_violations", [])),
        "wall_mount_violations": list(issues.get("wall_mount_violations", [])),
        "ceiling_mount_violations": list(issues.get("ceiling_mount_violations", [])),
        "wall_aligned_violations": list(issues.get("wall_aligned_violations", [])),
        "protected_path_violations": list(
            issues.get("protected_path_violations", [])
        ),
        "sofa_coffee_table_violations": list(
            issues.get("sofa_coffee_table_violations", [])
        ),
        "sofa_wall_gap_violations": list(issues.get("sofa_wall_gap_violations", [])),
        "media_focal_alignment_violations": list(
            issues.get("media_focal_alignment_violations", [])
        ),
        "media_group_violations": list(issues.get("media_group_violations", [])),
        "rug_composition_violations": list(issues.get("rug_composition_violations", [])),
        "coffee_table_axis_violations": list(
            issues.get("coffee_table_axis_violations", [])
        ),
        "living_group_cohesion_violations": list(
            issues.get("living_group_cohesion_violations", [])
        ),
        "dining_group_violations": list(issues.get("dining_group_violations", [])),
        "living_dining_clearance_violations": list(
            issues.get("living_dining_clearance_violations", [])
        ),
        "task_chair_orientation_violations": list(
            issues.get("task_chair_orientation_violations", [])
        ),
        "side_table_reach_violations": list(
            issues.get("side_table_reach_violations", [])
        ),
        "floor_lamp_reach_violations": list(
            issues.get("floor_lamp_reach_violations", [])
        ),
        "table_lamp_support_violations": list(
            issues.get("table_lamp_support_violations", [])
        ),
        "service_corridor_violations": list(
            issues.get("service_corridor_violations", [])
        ),
    }

def issue_summary(counts: dict[str, int]) -> str:
    return (
        f"overlaps={counts['overlaps']}, "
        f"boundary={counts['boundary']}, "
        f"door={counts['door']}, "
        f"obstacle={counts['obstacle']}, "
        f"wall_mount={counts['wall_mount']}, "
        f"ceiling_mount={counts['ceiling_mount']}, "
        f"wall_align={counts['wall_aligned']}, "
        f"protected_path={counts['protected_path']}, "
        f"sofa_table={counts['sofa_table']}, "
        f"sofa_wall={counts['sofa_wall']}, "
        f"media_align={counts['media_alignment']}, "
        f"media_group={counts['media_group']}, "
        f"rug_composition={counts['rug_composition']}, "
        f"table_axis={counts['table_axis']}, "
        f"living_group={counts['living_group']}, "
        f"dining_group={counts['dining_group']}, "
        f"living_dining={counts['living_dining_clearance']}, "
        f"task_chair={counts['task_chair_orientation']}, "
        f"side_table={counts['side_table_reach']}, "
        f"floor_lamp={counts['floor_lamp_reach']}, "
        f"table_lamp={counts['table_lamp_support']}, "
        f"service_corridor={counts['service_corridor']}"
    )

def total_issue_count(counts: dict[str, int]) -> int:
    return sum(int(value) for value in counts.values())

def get_asset_by_uid(uid: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
    lookup_uids = {uid, base_asset_uid(uid)}
    for asset in assets:
        if asset.get("uid") in lookup_uids:
            return asset
    return {}

class FloorAsset(NamedTuple):
    uid: str
    asset: dict[str, Any]
    pos: list[float]
    rot_z: float
    width: float
    depth: float
    poly: Polygon

def issue_counts(
    issues: dict[str, Any],
    tiers: tuple[str, ...] = ("P0", "P1", "P2"),
) -> dict[str, int]:
    return {
        **{key.removesuffix("_violations"): len(issues.get(key, [])) if "P2" in tiers else 0
           for key in (*BEDROOM_ISSUE_KEYS, *DINING_ISSUE_KEYS, *STUDIO_ISSUE_KEYS)},
        "overlaps": len(issues.get("overlaps", [])),
        "boundary": len(issues.get("boundary_violations", [])),
        "door": len(issues.get("door_violations", [])),
        "obstacle": len(issues.get("obstacle_violations", [])),
        "wall_mount": len(issues.get("wall_mount_violations", [])),
        "ceiling_mount": len(issues.get("ceiling_mount_violations", [])),
        "wall_aligned": len(issues.get("wall_aligned_violations", []))
        if "P1" in tiers
        else 0,
        "protected_path": len(issues.get("protected_path_violations", []))
        if "P1" in tiers
        else 0,
        "sofa_table": len(issues.get("sofa_coffee_table_violations", []))
        if "P2" in tiers
        else 0,
        "sofa_wall": len(issues.get("sofa_wall_gap_violations", []))
        if "P2" in tiers
        else 0,
        "media_alignment": len(issues.get("media_focal_alignment_violations", []))
        if "P2" in tiers
        else 0,
        "media_group": len(issues.get("media_group_violations", []))
        if "P2" in tiers
        else 0,
        "rug_composition": len(issues.get("rug_composition_violations", []))
        if "P2" in tiers
        else 0,
        "table_axis": len(issues.get("coffee_table_axis_violations", []))
        if "P2" in tiers
        else 0,
        "living_group": len(issues.get("living_group_cohesion_violations", []))
        if "P2" in tiers
        else 0,
        "living_group_critical": critical_p2_issue_count(
            {"living_group_cohesion_violations": issues.get("living_group_cohesion_violations", [])}
        )
        if "P2" in tiers
        else 0,
        "dining_group": len(issues.get("dining_group_violations", []))
        if "P2" in tiers
        else 0,
        "living_dining_clearance": len(
            issues.get("living_dining_clearance_violations", [])
        )
        if "P2" in tiers
        else 0,
        "task_chair_orientation": len(
            issues.get("task_chair_orientation_violations", [])
        )
        if "P2" in tiers
        else 0,
        "side_table_reach": len(issues.get("side_table_reach_violations", []))
        if "P2" in tiers
        else 0,
        "floor_lamp_reach": len(issues.get("floor_lamp_reach_violations", []))
        if "P2" in tiers
        else 0,
        "table_lamp_support": len(issues.get("table_lamp_support_violations", []))
        if "P2" in tiers
        else 0,
        "service_corridor": len(issues.get("service_corridor_violations", []))
        if "P2" in tiers
        else 0,
    }
