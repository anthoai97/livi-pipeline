"""Shared canonical living-group geometry."""

from __future__ import annotations

import math

from app.rules.geometry.primitives import projected_half_extents


def canonical_living_group_poses(
    *,
    sofa_uid: str,
    coffee_table_uid: str,
    accent_seat_uids: list[str],
    dimensions: dict[str, tuple[float, float]],
    sofa_table_gaps: list[float],
    chair_gap: float = 0.30,
    chair_spacing: float = 0.08,
) -> list[tuple[str, dict[str, tuple[float, float, float]]]]:
    """Return the canonical arrangements consumed by preflight and the solver."""
    sofa_width, sofa_depth = dimensions[sofa_uid]
    table_width, table_depth = dimensions[coffee_table_uid]
    arrangements: list[tuple[str, dict[str, str]]] = [
        (
            "chairs_opposite",
            {uid: "opposite" for uid in accent_seat_uids},
        )
    ]
    for uid in accent_seat_uids:
        for side in ("left", "right"):
            arrangements.append(
                (
                    f"one_side_{side}_{uid}",
                    {
                        chair_uid: side if chair_uid == uid else "opposite"
                        for chair_uid in accent_seat_uids
                    },
                )
            )
    if len(accent_seat_uids) >= 2:
        for left_uid in accent_seat_uids:
            for right_uid in accent_seat_uids:
                if left_uid == right_uid:
                    continue
                arrangements.append(
                    (
                        f"both_sides_{left_uid}_{right_uid}",
                        {
                            chair_uid: (
                                "left"
                                if chair_uid == left_uid
                                else (
                                    "right"
                                    if chair_uid == right_uid
                                    else "opposite"
                                )
                            )
                            for chair_uid in accent_seat_uids
                        },
                    )
                )

    sofa_rotation = math.pi / 2.0
    sofa_axis_offset = math.pi / 2.0 if sofa_width >= sofa_depth else 0.0
    table_axis_offset = math.pi / 2.0 if table_width >= table_depth else 0.0
    table_rotation = sofa_rotation + sofa_axis_offset - table_axis_offset
    _, sofa_half_y = projected_half_extents(
        sofa_width,
        sofa_depth,
        sofa_rotation,
    )
    table_half_x, table_half_y = projected_half_extents(
        table_width,
        table_depth,
        table_rotation,
    )
    distinct_gaps: list[float] = []
    for raw_gap in sofa_table_gaps:
        gap = float(raw_gap)
        if not any(
            math.isclose(gap, seen_gap, abs_tol=1e-9)
            for seen_gap in distinct_gaps
        ):
            distinct_gaps.append(gap)

    result: list[tuple[str, dict[str, tuple[float, float, float]]]] = []
    for sofa_table_gap in distinct_gaps:
        for arrangement_name, roles in arrangements:
            table_y = 2.0 * sofa_half_y + sofa_table_gap + table_half_y
            relative_poses: dict[str, tuple[float, float, float]] = {
                sofa_uid: (0.0, sofa_half_y, sofa_rotation),
                coffee_table_uid: (0.0, table_y, table_rotation),
            }
            left_uid = next(
                (uid for uid, role in roles.items() if role == "left"),
                "",
            )
            right_uid = next(
                (uid for uid, role in roles.items() if role == "right"),
                "",
            )
            if left_uid:
                chair_half_x, _ = projected_half_extents(
                    *dimensions[left_uid],
                    0.0,
                )
                relative_poses[left_uid] = (
                    -(table_half_x + chair_gap + chair_half_x),
                    table_y,
                    0.0,
                )
            if right_uid:
                chair_half_x, _ = projected_half_extents(
                    *dimensions[right_uid],
                    math.pi,
                )
                relative_poses[right_uid] = (
                    table_half_x + chair_gap + chair_half_x,
                    table_y,
                    math.pi,
                )

            opposite_uids = [
                uid for uid in accent_seat_uids if roles.get(uid) == "opposite"
            ]
            if opposite_uids:
                opposite_extents = {
                    uid: projected_half_extents(
                        *dimensions[uid],
                        -math.pi / 2.0,
                    )
                    for uid in opposite_uids
                }
                row_width = sum(
                    2.0 * opposite_extents[uid][0] for uid in opposite_uids
                )
                row_width += chair_spacing * max(0, len(opposite_uids) - 1)
                row_half_y = max(
                    opposite_extents[uid][1] for uid in opposite_uids
                )
                cursor_x = -row_width / 2.0
                row_y = table_y + table_half_y + chair_gap + row_half_y
                for uid in opposite_uids:
                    chair_half_x, _ = opposite_extents[uid]
                    relative_poses[uid] = (
                        cursor_x + chair_half_x,
                        row_y,
                        -math.pi / 2.0,
                    )
                    cursor_x += 2.0 * chair_half_x + chair_spacing
            result.append((arrangement_name, relative_poses))
    return result


def oriented_living_group_bounds(
    relative_poses: dict[str, tuple[float, float, float]],
    dimensions: dict[str, tuple[float, float]],
    quarter_turns: int,
) -> tuple[
    dict[str, tuple[float, float, float]],
    tuple[float, float, float, float],
]:
    """Rotate a canonical group and return exact projected min/max bounds."""
    angle = quarter_turns * math.pi / 2.0
    cos_angle = round(math.cos(angle))
    sin_angle = round(math.sin(angle))
    oriented: dict[str, tuple[float, float, float]] = {}
    min_x = math.inf
    min_y = math.inf
    max_x = -math.inf
    max_y = -math.inf
    for uid, (x, y, rotation_z) in relative_poses.items():
        oriented_x = x * cos_angle - y * sin_angle
        oriented_y = x * sin_angle + y * cos_angle
        oriented_rotation = rotation_z + angle
        oriented[uid] = (oriented_x, oriented_y, oriented_rotation)
        half_x, half_y = projected_half_extents(
            *dimensions[uid],
            oriented_rotation,
        )
        min_x = min(min_x, oriented_x - half_x)
        min_y = min(min_y, oriented_y - half_y)
        max_x = max(max_x, oriented_x + half_x)
        max_y = max(max_y, oriented_y + half_y)
    return oriented, (min_x, min_y, max_x, max_y)
