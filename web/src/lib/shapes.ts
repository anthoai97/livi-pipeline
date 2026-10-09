import type { Vec2 } from "./types";

// Room shapes as the legacy room builder makes them (web-pipeline lib/rooms/shapes.ts): metres in the
// pipeline's plan frame, counter-clockwise, width x length is the bounding box. Vertex order is wall order.

export type ShapeId = "Rectangle" | "L-Shaped" | "T-Shaped" | "Angled";
export type CutCorner = "top-right" | "top-left" | "bottom-right" | "bottom-left";

export const SHAPES: { value: ShapeId; label: string; cuts: boolean }[] = [
  { value: "Rectangle", label: "Rectangle", cuts: false },
  { value: "L-Shaped", label: "L-shaped", cuts: true },
  { value: "T-Shaped", label: "T-shaped", cuts: false },
  { value: "Angled", label: "Angled", cuts: true },
];

export const CORNERS: { value: CutCorner; label: string }[] = [
  { value: "top-right", label: "Back right" },
  { value: "top-left", label: "Back left" },
  { value: "bottom-right", label: "Front right" },
  { value: "bottom-left", label: "Front left" },
];

/** The floor polygon, with the legacy default proportions for each shape. */
export function shapeVertices(shape: ShapeId, corner: CutCorner, w: number, l: number): Vec2[] {
  switch (shape) {
    case "L-Shaped": {
      const [cw, cd] = [w * 0.4, l * 0.4];
      const [keepW, keepL] = [w - cw, l - cd];
      switch (corner) {
        case "top-right":
          return [
            [0, 0],
            [w, 0],
            [w, keepL],
            [keepW, keepL],
            [keepW, l],
            [0, l],
          ];
        case "top-left":
          return [
            [0, 0],
            [w, 0],
            [w, l],
            [cw, l],
            [cw, keepL],
            [0, keepL],
          ];
        case "bottom-right":
          return [
            [0, 0],
            [keepW, 0],
            [keepW, cd],
            [w, cd],
            [w, l],
            [0, l],
          ];
        case "bottom-left":
          return [
            [cw, 0],
            [w, 0],
            [w, l],
            [0, l],
            [0, cd],
            [cw, cd],
          ];
      }
      break;
    }
    case "T-Shaped": {
      const [stemL, stemR, barFront] = [w * 0.3, w * 0.7, l - l * 0.45];
      return [
        [stemL, 0],
        [stemR, 0],
        [stemR, barFront],
        [w, barFront],
        [w, l],
        [0, l],
        [0, barFront],
        [stemL, barFront],
      ];
    }
    case "Angled": {
      const [cx, cy] = [w * 0.27, l * 0.37];
      switch (corner) {
        case "top-left":
          return [
            [0, 0],
            [w, 0],
            [w, l],
            [cx, l],
            [0, l - cy],
          ];
        case "top-right":
          return [
            [0, 0],
            [w, 0],
            [w, l - cy],
            [w - cx, l],
            [0, l],
          ];
        case "bottom-right":
          return [
            [0, 0],
            [w - cx, 0],
            [w, cy],
            [w, l],
            [0, l],
          ];
        case "bottom-left":
          return [
            [cx, 0],
            [w, 0],
            [w, l],
            [0, l],
            [0, cy],
          ];
      }
      break;
    }
  }
  return [
    [0, 0],
    [w, 0],
    [w, l],
    [0, l],
  ];
}

export function polygonArea(vertices: Vec2[]): number {
  return (
    Math.abs(
      vertices.reduce((sum, [x1, y1], i) => {
        const [x2, y2] = vertices[(i + 1) % vertices.length];
        return sum + x1 * y2 - x2 * y1;
      }, 0),
    ) / 2
  );
}

/** Area centroid: a floor point that stays inside L and T rooms, unlike the bounding-box centre. */
export function polygonCentroid(vertices: Vec2[]): Vec2 {
  let [a, cx, cy] = [0, 0, 0];
  vertices.forEach(([x1, y1], i) => {
    const [x2, y2] = vertices[(i + 1) % vertices.length];
    const cross = x1 * y2 - x2 * y1;
    a += cross;
    cx += (x1 + x2) * cross;
    cy += (y1 + y2) * cross;
  });
  return a ? [cx / (3 * a), cy / (3 * a)] : vertices[0];
}

export function pointInPolygon([x, y]: Vec2, vertices: Vec2[]): boolean {
  let inside = false;
  for (let i = 0, j = vertices.length - 1; i < vertices.length; j = i++) {
    const [xi, yi] = vertices[i];
    const [xj, yj] = vertices[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Where the line x = c (axis 0) or y = c (axis 1) runs inside the polygon, as [from, to] spans. */
export function insideSpans(vertices: Vec2[], axis: 0 | 1, c: number): [number, number][] {
  const other = axis === 0 ? 1 : 0;
  const hits: number[] = [];
  vertices.forEach((a, i) => {
    const b = vertices[(i + 1) % vertices.length];
    if (a[axis] > c !== b[axis] > c) hits.push(a[other] + ((c - a[axis]) / (b[axis] - a[axis])) * (b[other] - a[other]));
  });
  hits.sort((p, q) => p - q);
  const spans: [number, number][] = [];
  for (let i = 0; i + 1 < hits.length; i += 2) spans.push([hits[i], hits[i + 1]]);
  return spans;
}

export interface Edge {
  index: number;
  start: Vec2;
  end: Vec2;
  length: number;
  normal: Vec2; // inward, unit
  label: string;
}

/** Walls in vertex order, named by the way they face, as "Back wall" or "Inner right wall". */
export function polygonEdges(vertices: Vec2[]): Edge[] {
  const xs = vertices.map(([x]) => x);
  const ys = vertices.map(([, y]) => y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const on = (value: number, bound: number) => Math.abs(value - bound) < 1e-6;
  return vertices.map((start, index) => {
    const end = vertices[(index + 1) % vertices.length];
    const length = Math.hypot(end[0] - start[0], end[1] - start[1]);
    const normal: Vec2 = [-(end[1] - start[1]) / length, (end[0] - start[0]) / length];
    const [nx, ny] = normal;
    const [side, outer] =
      ny < -0.7
        ? ["back", on(start[1], maxY)]
        : ny > 0.7
          ? ["front", on(start[1], minY)]
          : nx > 0.7
            ? ["left", on(start[0], minX)]
            : nx < -0.7
              ? ["right", on(start[0], maxX)]
              : ["angled", true];
    const name = `${outer ? "" : "inner "}${side} wall`;
    return { index, start, end, length, normal, label: name[0].toUpperCase() + name.slice(1) };
  });
}

/**
 * The bounding-box corner the 3D view looks in from, as [x side, y side] (1, -1 is front-right).
 * Front-right by default; a room with inner walls is seen from a cut-away corner instead, so those
 * walls face away and the dollhouse cutaway hides them like the outer ones.
 */
export function viewCorner(vertices: Vec2[], [w, l]: Vec2): [number, number] {
  const corners: [number, number][] = [
    [1, -1],
    [-1, -1],
    [1, 1],
    [-1, 1],
  ];
  const turns = vertices.map((a, i) => {
    const [b, c] = [vertices[(i + 1) % vertices.length], vertices[(i + 2) % vertices.length]];
    return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]);
  });
  if (turns.every((turn) => turn >= -1e-9)) return corners[0]; // convex: nothing to hide
  const cut = corners.find(([sx, sy]) => !pointInPolygon([w / 2 + sx * (w / 2 - 0.05), l / 2 + sy * (l / 2 - 0.05)], vertices));
  return cut ?? corners[0];
}

/** Where the door and window go by default: the longest wall the view shows, the back wall first. */
export function defaultWall(edges: Edge[], [sx, sy]: [number, number]): number {
  const seen = edges.filter((e) => e.normal[0] * sx + e.normal[1] * sy > 0.3);
  const score = (e: Edge) => e.length + (e.normal[1] < -0.7 ? 100 : 0);
  return (seen.length ? seen : edges).reduce((best, e) => (score(e) > score(best) ? e : best)).index;
}

/** The largest axis-aligned rectangle inside the polygon whose sides lie on vertex coordinates. */
export function largestInnerRect(vertices: Vec2[]): { min: Vec2; max: Vec2 } {
  const xs = [...new Set(vertices.map(([x]) => x))].sort((a, b) => a - b);
  const ys = [...new Set(vertices.map(([, y]) => y))].sort((a, b) => a - b);
  const inside = (p: Vec2) => pointInPolygon(p, vertices);
  let best = { min: [xs[0], ys[0]] as Vec2, max: [xs[xs.length - 1], ys[ys.length - 1]] as Vec2, area: -1 };
  for (const x1 of xs)
    for (const x2 of xs.filter((x) => x > x1))
      for (const y1 of ys)
        for (const y2 of ys.filter((y) => y > y1)) {
          const e = 1e-3;
          const probes: Vec2[] = [
            [x1 + e, y1 + e],
            [x2 - e, y1 + e],
            [x2 - e, y2 - e],
            [x1 + e, y2 - e],
            [(x1 + x2) / 2, y1 + e],
            [(x1 + x2) / 2, y2 - e],
            [x1 + e, (y1 + y2) / 2],
            [x2 - e, (y1 + y2) / 2],
          ];
          const vertexInside = vertices.some(([x, y]) => x > x1 + e && x < x2 - e && y > y1 + e && y < y2 - e);
          const area = (x2 - x1) * (y2 - y1);
          if (area > best.area && !vertexInside && probes.every(inside)) best = { min: [x1, y1], max: [x2, y2], area };
        }
  return { min: best.min, max: best.max };
}
