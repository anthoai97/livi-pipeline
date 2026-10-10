"""Top-down layout previews shared by browser delivery and benchmark review."""

from __future__ import annotations

import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.rules.door_geometry import door_opening_rect
from app.rules.geometry.primitives import asset_polygon, is_rug

Record = dict[str, Any]

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


