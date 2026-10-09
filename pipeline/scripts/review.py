"""Score benchmark run output with an LLM reviewer, from product selection to layout.

Reads run records from pipeline/.data/runs, draws a top-down plan of each ready
variant, and asks the reviewer model (through the local proxy, llm_proxy.ProxyClient)
for 1-5 scores on selection and layout, plus one call per run that compares its
ready variants. Writes report.md, review.json, and images/ to
pipeline/.data/reviews/<UTC timestamp>/.

Run from pipeline/: python scripts/review.py --latest 12
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from app.rules.door_geometry import door_opening_rect, door_wall
from app.rules.geometry.primitives import asset_polygon, is_rug
from llm_proxy import ProxyClient  # product-data/src, on the path through app

RUNS_DIR = PIPELINE / ".data" / "runs"
REVIEWS_DIR = PIPELINE / ".data" / "reviews"
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_EFFORT = "medium"

Record = dict[str, Any]

# --- rubric and schema -------------------------------------------------------

# Each criterion: what it judges, then what scores 1, 3, and 5 mean.
SELECTION = {
    "prompt_match": "Do the products deliver what the prompt asked for: named items, colors, materials, style, mood? "
                    "1 = ignores or contradicts the prompt; 3 = covers the main request but misses some named details; "
                    "5 = every explicit request is met and the mood matches.",
    "style_coherence": "Do the products read as one design (palette, materials, style) that follows the variant direction? "
                       "1 = clashing mix; 3 = mostly coherent with one or two pieces off; 5 = one clear, consistent look.",
    "budget_use": "Is the money spent well? 1 = over budget, or so little spent that the room is clearly underfurnished; "
                  "3 = within budget but poorly allocated (cheap anchor piece, costly accessories); "
                  "5 = within budget with spending focused on the anchor pieces.",
    "completeness": "Are the right items there for the room type and prompt? 1 = core pieces missing (seating in a living "
                    "room, a bed in a bedroom, a table or enough chairs in a dining room); 3 = core pieces present but "
                    "obvious supporting pieces missing (lighting, storage, side tables, nightstands) or items out of place; "
                    "5 = everything the room needs, nothing out of place.",
    "scale_fit": "Are product sizes right for the room and for each other? 1 = pieces clearly too big or too small; "
                 "3 = workable with one or two pieces off-scale; 5 = all pieces well proportioned.",
}
LAYOUT = {
    "circulation": "Can a person walk in from the door and reach every piece? 1 = door or main path blocked, items crammed; "
                   "3 = passable but some paths tight (under about 0.6 m) or awkward; "
                   "5 = clear paths of about 0.8 m or more from the door to every zone.",
    "functional_grouping": "Do groups make sense and face the right way: seats face the focal point or TV, coffee table in "
                           "front of the sofa, chairs at and facing the dining table, bed reachable from its sides with "
                           "nightstands beside it, lamps next to seats or beds? 1 = groups broken or pieces face walls; "
                           "3 = the main group works, some pieces misplaced or misoriented; "
                           "5 = every group complete and correctly oriented.",
    "space_use": "Is the furniture spread well over the room? 1 = cramped into one area, or so sparse the room feels empty; "
                 "3 = acceptable with a noticeable empty or crowded area; 5 = balanced, each zone sized to its use.",
    "focal_point": "Is there a clear focal point (TV, bed, dining table, sofa group) that the layout organizes around? "
                   "1 = none; 3 = present but the layout does not organize around it; 5 = clear, and everything relates to it.",
}
OVERALL = "Overall quality as a design to show a client. 1 = unusable; 3 = acceptable but needs clear fixes; 5 = ready to show."
DISTINCTNESS = ("Do the variants offer the client real choices? 1 = near copies (same products or same look); "
                "3 = some differences in products or palette but the same overall impression; "
                "5 = clearly different looks and product sets, each a valid answer to the prompt.")
ABBREVIATIONS = {"prompt_match": "prompt", "style_coherence": "style", "budget_use": "budget", "completeness": "complete",
                 "scale_fit": "scale", "circulation": "circ", "functional_grouping": "group", "space_use": "space",
                 "focal_point": "focal", "overall": "overall"}
CRITERIA = [*SELECTION, *LAYOUT, "overall"]


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


SCORE = _object({"score": {"type": "integer", "minimum": 1, "maximum": 5}, "reason": {"type": "string"}})
REVIEW_SCHEMA = _object({
    "selection": _object({name: SCORE for name in SELECTION}),
    "layout": _object({name: SCORE for name in LAYOUT}),
    "overall": SCORE,
    "top_issues": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    "strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
})
COMPARE_SCHEMA = _object({"distinctness": SCORE})

# --- top-down render ---------------------------------------------------------

WIDTH = 900
MARGIN = 40
TOP = 64
BOTTOM = 72
FLOOR = (248, 246, 240, 255)
WALL = (55, 55, 55, 255)
GRID = (226, 224, 218, 255)
DOOR = (150, 90, 40, 255)
WINDOW = (80, 165, 230, 255)
INK = (25, 25, 25, 255)
# Kind -> (fill, outline). Drawn in this order, so supported items land on top.
KINDS = {
    "rug": ((205, 175, 120, 90), (170, 140, 90, 255)),
    "floor": ((120, 150, 195, 215), (40, 60, 95, 255)),
    "wall": ((235, 130, 60, 50), (205, 95, 30, 255)),
    "ceiling": (None, (140, 140, 140, 255)),
    "support": ((95, 175, 115, 235), (30, 95, 50, 255)),
}
KIND_LABELS = {"rug": "rug", "floor": "floor", "wall": "wall-mounted", "ceiling": "ceiling", "support": "on a support"}


def _kind(product: Record, pose: Record) -> str:
    mode = product.get("placement_mode")
    if is_rug(product["instance_key"], product):
        return "rug"
    if pose.get("on_top_of") or mode == "tabletop":
        return "support"
    if mode == "wall_mounted":
        return "wall"
    return "ceiling" if mode == "ceiling_mounted" else "floor"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def render_plan(record: Record, variant: Record) -> Image.Image:
    """Top-down plan of one ready variant: +x right, +y up, 1 m grid, each item's rotated footprint."""
    request = record["request"]
    vertices = [(float(x), float(y)) for x, y in request["room_vertices"]]
    room_area = tuple(request["room_area"])
    min_x, min_y = min(x for x, _ in vertices), min(y for _, y in vertices)
    max_x, max_y = max(x for x, _ in vertices), max(y for _, y in vertices)
    scale = (WIDTH - 2 * MARGIN) / max(max_x - min_x, max_y - min_y)
    height = int(TOP + (max_y - min_y) * scale + BOTTOM)
    image = Image.new("RGB", (WIDTH, height), (255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")  # RGBA fills blend onto the RGB plan

    def px(point: tuple[float, float] | list[float]) -> tuple[float, float]:
        return MARGIN + (point[0] - min_x) * scale, TOP + (max_y - point[1]) * scale

    draw.polygon([px(v) for v in vertices], fill=FLOOR)
    for x in range(math.ceil(min_x), math.floor(max_x) + 1):
        draw.line([px((x, min_y)), px((x, max_y))], fill=GRID)
    for y in range(math.ceil(min_y), math.floor(max_y) + 1):
        draw.line([px((min_x, y)), px((max_x, y))], fill=GRID)
    draw.polygon([px(v) for v in vertices], outline=WALL, width=4)

    for window in request.get("room_windows") or []:
        opening = door_opening_rect(window, boundary=request["room_vertices"], room_area=room_area)
        draw.line(_along_wall(opening, px), fill=WINDOW, width=8)
    for door in request.get("room_doors") or []:
        opening = door_opening_rect(door, boundary=request["room_vertices"], room_area=room_area)
        hinge, end = _along_wall(opening, px, points=True)
        draw.line([px(hinge), px(end)], fill=FLOOR, width=8)  # the gap in the wall
        nx, ny = opening["inward_normal"]
        radius = opening["opening_width"]
        ux, uy = (end[0] - hinge[0]) / radius, (end[1] - hinge[1]) / radius
        arc = [px((hinge[0] + radius * (math.cos(t) * ux + math.sin(t) * nx),
                   hinge[1] + radius * (math.cos(t) * uy + math.sin(t) * ny)))
               for t in (math.radians(a) for a in range(0, 91, 5))]
        draw.line(arc, fill=DOOR, width=2)
        draw.line([px(hinge), arc[-1]], fill=DOOR, width=4)  # the open door leaf

    products = {product["instance_key"]: product for product in variant["products"]}
    shapes = []
    for key, pose in variant["layout"].items():
        product = products.get(key)
        if product is None:
            continue
        yaw = float(pose["rotation"][2])
        position = [float(value) for value in pose["position"]]
        polygon = asset_polygon(position, yaw, float(product["width_m"] or 0.5), float(product["depth_m"] or 0.5))
        shapes.append((_kind(product, pose), key, position, yaw, float(product["depth_m"] or 0.5), polygon))
    order = list(KINDS)
    shapes.sort(key=lambda shape: order.index(shape[0]))
    label_font = _font(12)
    for kind, _, position, yaw, depth, polygon in shapes:
        fill, outline = KINDS[kind]
        draw.polygon([px(p) for p in polygon.exterior.coords], fill=fill, outline=outline, width=3 if kind == "wall" else 2)
        if kind != "rug":
            front = (position[0] + math.cos(yaw) * depth / 2, position[1] + math.sin(yaw) * depth / 2)
            draw.line([px(position), px(front)], fill=INK, width=2)
            fx, fy = px(front)
            draw.ellipse([fx - 3, fy - 3, fx + 3, fy + 3], fill=INK)
    for kind, key, position, *_ in shapes:
        x, y = px(position)
        draw.text((x, y + (14 if kind == "support" else 0)), key.replace("_", " "), fill=INK, font=label_font,
                  anchor="mm", stroke_width=2, stroke_fill=(255, 255, 255, 255))

    title = (f"{request['room_type'].replace('_', ' ')}  {max_x - min_x:.1f} x {max_y - min_y:.1f} m  "
             f"variant {variant['variant_index']}  ${variant.get('total_cost', 0):,.0f} of ${request['budget']:,.0f}")
    draw.text((MARGIN, 20), title, fill=INK, font=_font(20))
    base = height - BOTTOM + 34
    for metre in range(3):
        draw.line([(MARGIN + metre * scale, base - 6), (MARGIN + metre * scale, base + 6)], fill=INK, width=2)
        draw.text((MARGIN + metre * scale, base + 10), f"{metre} m", fill=INK, font=_font(12), anchor="mt")
    draw.line([(MARGIN, base), (MARGIN + 2 * scale, base)], fill=INK, width=3)
    x = MARGIN + 2 * scale + 40
    for kind, (fill, outline) in KINDS.items():
        draw.rectangle([x, base - 7, x + 14, base + 7], fill=fill, outline=outline, width=2)
        draw.text((x + 20, base), KIND_LABELS[kind], fill=INK, font=_font(13), anchor="lm")
        x += 40 + draw.textlength(KIND_LABELS[kind], font=_font(13))
    return image


def _along_wall(opening: Record, px, points: bool = False):
    """The opening's two ends along its wall, as plan points or as pixel points."""
    nx, ny = opening["inward_normal"]
    ux, uy = abs(ny), abs(nx)
    cx, cy = opening["opening_center"]
    half = opening["opening_width"] / 2
    ends = [(cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)]
    return ends if points else [px(end) for end in ends]


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- prompts -----------------------------------------------------------------

IMAGE_KEY = ("The image is a top-down plan: +x right, +y up, light 1 m grid, scale bar in metres. Light tan translucent = "
             "rugs; blue = floor furniture; orange outline = wall-mounted items; gray outline = ceiling items; green = "
             "items resting on another item (lamps, decor). The dark line from each item's center ends at its front. "
             "Doors are gaps in the wall with a brown swing arc; windows are blue segments on the wall.")


def _facing(yaw: float) -> str:
    degrees = math.degrees(yaw) % 360
    names = ("right (+x)", "up (+y)", "left (-x)", "down (-y)")
    nearest = round(degrees / 90) % 4
    return names[nearest] if abs(degrees - nearest * 90) % 360 < 10 else f"{degrees:.0f} deg"


def _list(values: list) -> str:
    return ", ".join(str(value) for value in values) or "-"


def room_text(record: Record) -> str:
    request = record["request"]
    width, depth = request["room_area"]
    lines = [(f"Room type: {request['room_type'].replace('_', ' ')}, {width:.2f} x {depth:.2f} m "
              f"({width * depth:.1f} m2), wall height {float(request['wall_height']):.2f} m.")]
    boundary, room_area = request["room_vertices"], tuple(request["room_area"])
    for kind, openings in (("Door", request.get("room_doors") or []), ("Window", request.get("room_windows") or [])):
        for opening in openings:
            center = opening.get("center") or [0, 0]
            wall = door_wall(opening, boundary=boundary, room_area=room_area)
            lines.append(f"{kind} on the {wall} wall at ({center[0]:.2f}, {center[1]:.2f}), {opening.get('width', 0.9)} m wide.")
    return "\n".join(lines)


def product_table(variant: Record) -> str:
    rows = ["| key | title | category | price | W x D x H m | colors | materials | styles | position (x, y) m | faces | on top of |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for product in variant["products"]:
        pose = variant["layout"].get(product["instance_key"], {})
        position = pose.get("position") or [0, 0, 0]
        size = " x ".join(f"{float(product[f'{axis}_m'] or 0):.2f}" for axis in ("width", "depth", "height"))
        rows.append(
            f"| {product['instance_key']} | {product['title']} | {product['category']} | ${float(product['price'] or 0):,.0f} "
            f"| {size} | {_list(product['colors'])} | {_list(product['materials'])} | {_list(product['styles'])} "
            f"| ({position[0]:.2f}, {position[1]:.2f}) | {_facing(float((pose.get('rotation') or [0, 0, 0])[2]))} "
            f"| {pose.get('on_top_of') or '-'} |"
        )
    return "\n".join(rows)


def _rubric(criteria: dict) -> str:
    return "\n".join(f"- {name}: {text}" for name, text in criteria.items())


def review_prompt(record: Record, variant: Record) -> str:
    request = record["request"]
    cost, budget = float(variant.get("total_cost") or 0), float(request["budget"])
    share = f" ({cost / budget:.0%} of budget)" if budget else ""
    findings = _list(variant.get("non_blocking_findings") or [])
    return f"""You review one furnished room design produced by an automated pipeline. Judge only from the data and the image given here; do not assume anything that is not shown.

USER PROMPT: {request['user_intent']}
BUDGET: ${budget:,.0f}. Total cost: ${cost:,.0f}{share}. The total counts purchasable prices and leaves TVs out.
{room_text(record)}
{variant.get('direction') or 'VARIANT DIRECTION: none (the default proposal).'}

PRODUCTS AND PLACEMENT (one row per placed item; position is the footprint center):
{product_table(variant)}

MINOR LAYOUT FINDINGS from the pipeline's validator (non-blocking): {findings}

{IMAGE_KEY}

Score each criterion with an integer from 1 to 5 (2 and 4 fall between the anchors) and give a one-sentence reason.

SELECTION
{_rubric(SELECTION)}

LAYOUT
{_rubric(LAYOUT)}

OVERALL
- overall: {OVERALL}

Also list up to 3 top issues (short, concrete, most important first) and up to 2 strengths."""


def compare_prompt(record: Record, variants: list[Record]) -> str:
    request = record["request"]
    blocks = []
    for variant in variants:
        items = "\n".join(f"  - {p['title']} ({p['category']}; {_list(p['colors'])}; {_list(p['materials'])})"
                          for p in variant["products"])
        blocks.append(f"Variant {variant['variant_index']}, total ${float(variant.get('total_cost') or 0):,.0f}\n"
                      f"{variant.get('direction') or 'VARIANT DIRECTION: none (the default proposal).'}\n{items}")
    images = ", ".join(f"variant {variant['variant_index']}" for variant in variants)
    return f"""You compare the design variants that one automated pipeline run produced for the same request. Judge only from the data and the images given here.

USER PROMPT: {request['user_intent']}
BUDGET: ${float(request['budget']):,.0f}. {room_text(record)}

{chr(10).join(blocks)}

The images follow in this order: {images}. {IMAGE_KEY}

- distinctness: {DISTINCTNESS}
Give an integer score from 1 to 5 and a one-sentence reason."""


def _image_part(png: bytes) -> Record:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}


def _call(client: ProxyClient, content: list, schema: dict, name: str) -> Record:
    started = time.monotonic()
    try:
        result = client.complete(content, schema, name=name)
        return {"result": result, "latency_s": round(time.monotonic() - started, 2), "error": None}
    except Exception as exc:  # one failed call is reported, the rest go on
        return {"result": None, "latency_s": round(time.monotonic() - started, 2), "error": str(exc)[:500]}


def review_variant(client: ProxyClient, record: Record, variant: Record, png: bytes) -> Record:
    return _call(client, [{"type": "text", "text": review_prompt(record, variant)}, _image_part(png)],
                 REVIEW_SCHEMA, "variant_review")


def compare_variants(client: ProxyClient, record: Record, variants: list[Record], pngs: list[bytes]) -> Record:
    return _call(client, [{"type": "text", "text": compare_prompt(record, variants)}, *map(_image_part, pngs)],
                 COMPARE_SCHEMA, "variant_comparison")


# --- review runs and report --------------------------------------------------


def scores(review: Record | None) -> dict[str, int | None]:
    """Criterion -> score for one variant review, None when the call failed."""
    result = (review or {}).get("result") or {}
    flat = {**result.get("selection", {}), **result.get("layout", {}), "overall": result.get("overall")}
    return {name: (flat.get(name) or {}).get("score") for name in CRITERIA}


def review_runs(records: list[Record], client: ProxyClient, out_dir: Path, workers: int = 3) -> Record:
    """Render, review, and compare every ready variant with a stored result; write images, review.json, and report.md."""
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    runs, jobs = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for record in records:
            run = {"run_id": record["run_id"], "room_type": record["request"]["room_type"],
                   "prompt": record["request"]["user_intent"], "budget": record["request"]["budget"],
                   "variants": [], "comparison": None, "note": None}
            ready, pngs = [], []
            for variant in record["variants"]:
                entry = {"variant_index": variant["variant_index"], "outcome": variant["outcome"],
                         "reason": variant.get("reason"), "total_cost": variant.get("total_cost"),
                         "image": None, "review": None}
                if variant["outcome"] == "ready" and "products" in variant:
                    png = png_bytes(render_plan(record, variant))
                    entry["image"] = f"images/{record['run_id']}-v{variant['variant_index']}.png"
                    (out_dir / entry["image"]).write_bytes(png)
                    jobs.append((entry, pool.submit(review_variant, client, record, variant, png)))
                    ready.append(variant)
                    pngs.append(png)
                run["variants"].append(entry)
            if any(v["outcome"] == "ready" and "products" not in v for v in record["variants"]):
                run["note"] = "ready variants without a stored result (the record predates review support) were skipped"
            if len(ready) > 1:
                jobs.append((run, pool.submit(compare_variants, client, record, ready, pngs)))
            runs.append(run)
        for owner, future in jobs:
            owner["review" if "variant_index" in owner else "comparison"] = future.result()
    review = {"model": client.model, "effort": client.effort,
              "created_at": datetime.now(timezone.utc).isoformat(), "runs": runs}
    (out_dir / "review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(report(review), encoding="utf-8")
    return review


def _average(values: list) -> str:
    values = [value for value in values if value is not None]
    return f"{sum(values) / len(values):.2f}" if values else "-"


def _table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join("-" if cell is None else str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def score_rows(review: Record) -> tuple[list[str], list[list]]:
    """The summary table: one row per reviewed variant."""
    header = ["run", "room", "v", *(ABBREVIATIONS[name] for name in CRITERIA), "distinct", "cost"]
    rows = []
    for run in review["runs"]:
        distinct = ((run["comparison"] or {}).get("result") or {}).get("distinctness", {}).get("score")
        for entry in run["variants"]:
            if entry["review"] is not None:
                rows.append([run["run_id"][:8], run["room_type"], entry["variant_index"],
                             *scores(entry["review"]).values(), distinct, f"{float(entry['total_cost'] or 0):,.0f}"])
    return header, rows


def report(review: Record) -> str:
    header, rows = score_rows(review)
    reviews = [(run, entry) for run in review["runs"] for entry in run["variants"] if entry["review"] is not None]
    all_scores = [(run["room_type"], scores(entry["review"])) for run, entry in reviews]
    rooms = sorted({room for room, _ in all_scores})
    distinct = {run["room_type"]: [] for run in review["runs"]}
    for run in review["runs"]:
        distinct[run["room_type"]].append(((run["comparison"] or {}).get("result") or {}).get("distinctness", {}).get("score"))
    averages = [["all", len(all_scores), *(_average([s[name] for _, s in all_scores]) for name in CRITERIA),
                 _average([d for values in distinct.values() for d in values])]]
    averages += [[room, sum(r == room for r, _ in all_scores),
                  *(_average([s[name] for r, s in all_scores if r == room]) for name in CRITERIA),
                  _average(distinct.get(room, []))] for room in rooms]
    lines = [f"# Benchmark review {review['created_at'][:19]}Z", "",
             (f"Reviewer `{review['model']}` at effort `{review['effort']}`. "
              f"{len(review['runs'])} runs, {len(reviews)} variants reviewed. Scores 1-5."), "",
             "## Scores", "", _table(header, rows), "",
             "## Averages", "", _table(["room", "variants", *(ABBREVIATIONS[n] for n in CRITERIA), "distinct"], averages), ""]
    for run in review["runs"]:
        lines += [f"## {run['room_type']} `{run['run_id']}`", "", f"Prompt: {run['prompt']} Budget ${float(run['budget']):,.0f}.", ""]
        if run["note"]:
            lines += [f"Note: {run['note']}.", ""]
        comparison = run["comparison"]
        if comparison:
            result = comparison["result"]
            lines += [f"Distinctness: {result['distinctness']['score']} - {result['distinctness']['reason']}" if result
                      else f"Comparison failed: {comparison['error']}", ""]
        for entry in run["variants"]:
            name = f"Variant {entry['variant_index']}"
            if entry["outcome"] != "ready":
                lines += [f"- {name}: failed ({entry['reason']}), not reviewed.", ""]
                continue
            if entry["review"] is None:
                lines += [f"- {name}: ready, no stored result, not reviewed.", ""]
                continue
            result = entry["review"]["result"]
            lines += [f"### {name}", "", f"![{name}]({entry['image']})", ""]
            if result is None:
                lines += [f"Review failed: {entry['review']['error']}", ""]
                continue
            lines += [f"Overall {result['overall']['score']}: {result['overall']['reason']}", "",
                      "Top issues:", *(f"- {issue}" for issue in result["top_issues"]), "",
                      "Strengths:", *(f"- {strength}" for strength in result["strengths"]), "",
                      _table(["criterion", "score", "reason"],
                             [[name, item["score"], item["reason"]]
                              for group in ("selection", "layout") for name, item in result[group].items()]), ""]
    return "\n".join(lines)


def print_table(header: list[str], rows: list[list]) -> None:
    cells = [[("-" if cell is None else str(cell)) for cell in row] for row in rows]
    widths = [max(len(h), *(len(line[i]) for line in cells)) for i, h in enumerate(header)]
    for line in [header, *cells]:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Score benchmark run output with an LLM reviewer")
    runs = parser.add_mutually_exclusive_group(required=True)
    runs.add_argument("--runs", nargs="+", metavar="RUN_ID", help="run records to review")
    runs.add_argument("--latest", type=int, metavar="N", help="review the N most recent run records")
    parser.add_argument("--model", default=os.environ.get("REVIEW_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--effort", default=os.environ.get("REVIEW_EFFORT") or DEFAULT_EFFORT)
    parser.add_argument("--workers", type=int, default=3, help="reviewer calls in parallel")
    args = parser.parse_args()
    load_dotenv(PIPELINE.parent / ".env")
    if args.runs:
        paths = [RUNS_DIR / f"{run_id}.json" for run_id in args.runs]
        missing = [path.name for path in paths if not path.exists()]
        if missing:
            parser.error(f"no run record: {', '.join(missing)}")
    else:
        paths = sorted(RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)[-args.latest:]
    records = sorted((json.loads(path.read_text()) for path in paths), key=lambda record: record["started_at"])
    client = ProxyClient(model=args.model, effort=args.effort)
    out_dir = REVIEWS_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    review = review_runs(records, client, out_dir, workers=max(1, args.workers))

    print_table(*score_rows(review))
    for run in review["runs"]:
        for entry in run["variants"]:
            if entry["outcome"] != "ready":
                print(f"{run['run_id'][:8]} v{entry['variant_index']}: failed ({entry['reason']}), not reviewed")
            elif entry["review"] and entry["review"]["error"]:
                print(f"{run['run_id'][:8]} v{entry['variant_index']}: review failed: {entry['review']['error']}")
        if run["note"]:
            print(f"{run['run_id'][:8]}: {run['note']}")
    print(f"\nreport: {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
