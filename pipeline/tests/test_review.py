"""Benchmark review tooling (scripts/review.py): the top-down plan and the reviewer calls, offline."""

import io
import json
import math

from PIL import Image
from scripts import review


def product(key: str, category: str, width: float, depth: float, mode: str = "floor", **extra) -> dict:
    return {"instance_key": key, "uid": f"uid-{key}", "title": f"Oak {category}", "category": category, "price": 300.0,
            "width_m": width, "depth_m": depth, "height_m": 0.8, "colors": ["oak"], "styles": ["scandinavian"],
            "materials": ["wood"], "is_decor_item": False, "mount_type": None, "placement_mode": mode, **extra}


def pose(x: float, y: float, yaw: float = 0.0, on_top_of: str | None = None) -> dict:
    return {"position": [x, y, 0.0], "rotation": [0.0, 0.0, yaw], "on_top_of": on_top_of}


PRODUCTS = [
    product("rug_1", "rug", 2.0, 1.5),
    # Yaw 0 faces +x, so the 2 m width runs along y: x 0.55-1.45, y 1.5-3.5.
    product("sofa_1", "sofa", 2.0, 0.9),
    product("tv_1", "tv", 1.2, 0.08, "wall_mounted"),
    product("side_table_1", "side_table", 0.5, 0.5),
    product("table_lamp_1", "table_lamp", 0.3, 0.3, "tabletop"),
]
LAYOUT = {
    "rug_1": pose(2.0, 2.5),
    "sofa_1": pose(1.0, 2.5),
    "tv_1": pose(3.96, 2.5, math.pi),
    "side_table_1": pose(1.0, 4.0),
    "table_lamp_1": pose(1.0, 4.0, 0.0, "side_table_1"),
}
RECORD = {
    "run_id": "run0001abcdef",
    "started_at": "2026-10-09T10:00:00+00:00",
    "request": {
        "user_intent": "A calm Scandinavian living room with an oak sofa and a TV",
        "budget": 3000.0,
        "room_type": "living_room",
        "room_area": [4.0, 5.0],
        "wall_height": 2.7,
        "room_vertices": [[0.0, 0.0], [4.0, 0.0], [4.0, 5.0], [0.0, 5.0]],
        "room_doors": [{"center": [2.0, 0.0], "width": 0.9, "depth": 0.05}],
        "room_windows": [{"center": [2.0, 5.0], "width": 1.1, "depth": 0.05}],
    },
    "variants": [
        {"variant_index": 0, "outcome": "ready", "reason": None, "non_blocking_findings": ["media_group_violations"],
         "direction": "", "total_cost": 1500.0, "products": PRODUCTS, "layout": LAYOUT},
        {"variant_index": 1, "outcome": "ready", "reason": None, "non_blocking_findings": [],
         "direction": "VARIANT DIRECTION: cool palette", "total_cost": 1400.0, "products": PRODUCTS, "layout": LAYOUT},
        {"variant_index": 2, "outcome": "failed", "reason": "layout_validation_failed", "non_blocking_findings": []},
    ],
}


def pixel(image: Image.Image, x: float, y: float) -> tuple[int, int, int]:
    """The image pixel at plan point (x, y) of the 4 x 5 m room."""
    scale = (review.WIDTH - 2 * review.MARGIN) / 5.0
    return image.getpixel((round(review.MARGIN + x * scale), round(review.TOP + (5.0 - y) * scale)))


def test_plan_draws_rotated_footprints_and_the_door_gap():
    png = review.png_bytes(review.render_plan(RECORD, RECORD["variants"][0]))
    image = Image.open(io.BytesIO(png))

    assert png.startswith(b"\x89PNG") and image.width == review.WIDTH
    floor_fill = review.KINDS["floor"][0][:3]
    # Inside the rotated sofa footprint, outside the unrotated one (x 0-2, y 2.05-2.95), and the reverse.
    assert all(abs(a - b) < 30 for a, b in zip(pixel(image, 0.7, 3.3), floor_fill))
    assert pixel(image, 1.8, 2.2) != pixel(image, 0.7, 3.3)
    # The door leaves a gap in the bottom wall; the rest of that wall is drawn.
    assert pixel(image, 1.8, 0.0) == review.FLOOR[:3]
    assert pixel(image, 3.5, 0.0) == review.WALL[:3]
    # The lamp on the side table is drawn on top of it.
    assert all(abs(a - b) < 30 for a, b in zip(pixel(image, 1.1, 4.1), review.KINDS["support"][0][:3]))


class FakeProxy:
    """ProxyClient stand-in: records each call and answers by schema name; the variant 1 review fails."""

    model = "fake-model"
    effort = "medium"

    def __init__(self):
        self.calls = []

    def complete(self, content, schema, *, name):
        self.calls.append((name, content, schema))
        if name == "variant_comparison":
            return {"distinctness": {"score": 2, "reason": "Same products."}}
        if "cool palette" in content[0]["text"]:
            raise RuntimeError("proxy call failed: 502")
        score = {"score": 4, "reason": "Fine."}
        return {"selection": {name: score for name in review.SELECTION}, "layout": {name: score for name in review.LAYOUT},
                "overall": {"score": 3, "reason": "Acceptable."}, "top_issues": ["TV far from sofa"], "strengths": ["Calm"]}


def strict(schema: dict) -> bool:
    """Every object in a strict json_schema lists all its properties as required and allows no others."""
    if schema.get("type") == "object":
        return (schema["additionalProperties"] is False and schema["required"] == list(schema["properties"])
                and all(strict(child) for child in schema["properties"].values()))
    return strict(schema["items"]) if schema.get("type") == "array" else True


def test_review_runs_reviews_ready_variants_and_reports_failures(tmp_path):
    client = FakeProxy()

    result = review.review_runs([RECORD], client, tmp_path, workers=2)

    assert sorted(name for name, _, _ in client.calls) == ["variant_comparison", "variant_review", "variant_review"]
    assert strict(review.REVIEW_SCHEMA) and strict(review.COMPARE_SCHEMA)
    for name, content, schema in client.calls:
        text, *images = content
        assert all(image["image_url"]["url"].startswith("data:image/png;base64,") for image in images)
        assert len(images) == (2 if name == "variant_comparison" else 1)
        assert RECORD["request"]["user_intent"] in text["text"] and "Judge only from the data" in text["text"]
    prompt = next(content[0]["text"] for name, content, _ in client.calls
                  if name == "variant_review" and "cool palette" not in content[0]["text"])
    for expected in ("$3,000", "up to 110% of it ($3,300)", "a total between 100% and the allowance is within budget",
                     "layout_quality", "5 = looks professionally arranged", "$1,500 (50% of budget)", "Oak sofa", "| (1.00, 2.50) | right (+x) |", "side_table_1",
                     "media_group_violations", "Door on the bottom wall", "5 = clear paths", "default proposal"):
        assert expected in prompt

    [run] = result["runs"]
    assert run["comparison"]["result"]["distinctness"]["score"] == 2
    first, second, failed = run["variants"]
    assert first["review"]["result"]["overall"]["score"] == 3 and first["review"]["latency_s"] >= 0
    assert second["review"]["result"] is None and "502" in second["review"]["error"]
    assert failed["review"] is None and failed["image"] is None
    assert (tmp_path / "images" / "run0001abcdef-v0.png").exists()
    assert json.loads((tmp_path / "review.json").read_text())["effort"] == "medium"
    report = (tmp_path / "report.md").read_text()
    assert "![Variant 0](images/run0001abcdef-v0.png)" in report
    assert "Variant 2: failed (layout_validation_failed), not reviewed" in report
    assert "Review failed: proxy call failed: 502" in report
    assert "| run | room | v | prompt | style | budget | complete | scale | circ | group | space | focal | quality | overall |" in report
    assert "| run0001a | living_room | 0 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 3 | 2 | 1,500 |" in report
    assert "| room | variants | prompt | style | budget | complete | scale | circ | group | space | focal | quality | overall | distinct |" in report
    assert "| layout_quality | 4 | Fine. |" in report
