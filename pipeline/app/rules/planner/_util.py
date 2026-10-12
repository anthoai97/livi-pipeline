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


# (compact below, medium below) floor area in m2 per room type; spacious above.
ROOM_SCALE_CUTOFFS_SQM = {
    "living_room": (18.0, 30.0),
    "bedroom": (11.0, 16.0),
    "dining_room": (10.0, 16.0),
    "studio": (25.0, 40.0),
}


def room_scale(area_sqm: float, room_type: str) -> str:
    compact, medium = ROOM_SCALE_CUTOFFS_SQM[room_type]
    if area_sqm < compact:
        return "compact"
    if area_sqm < medium:
        return "medium"
    return "spacious"
