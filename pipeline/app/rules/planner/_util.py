"""Small shared helpers for planner modules."""

from typing import Any


def round4(value: float) -> float:
    return round(float(value), 4)


def wall_text(record: Any) -> str:
    """A wall record for a prompt: "wall 3 (Back (inner))", or "none"."""
    if not record or record.get("wall") is None:
        return "none"
    return f"wall {record['wall']} ({record.get('label') or record.get('wall_label') or '?'})"


def clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def room_scale(area_sqm: float) -> str:
    """Size class for every room type: compact below 18 m2, medium below 30 m2, spacious above."""
    if area_sqm < 18.0:
        return "compact"
    if area_sqm < 30.0:
        return "medium"
    return "spacious"
