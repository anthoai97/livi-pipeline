"""Small shared helpers for planner modules."""

from typing import Any


def round4(value: float) -> float:
    return round(float(value), 4)


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
    if area_sqm < 18.0:
        return "compact"
    if area_sqm < 30.0:
        return "medium"
    return "spacious"
