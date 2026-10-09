import recorded from "../mock/recorded-run.json";
import { largestInnerRect, polygonArea, polygonEdges } from "./shapes";
import type { PipelineEvent, PipelineRequest, Placement, ReadyVariant, RenderManifest } from "./types";

// Mock flow: replays one recorded pipeline run (a dining room for four, 3 of 3 designs ready),
// fitted to the user's brief, so the frontend works without the pipeline service.

interface RecordedRun {
  request: PipelineRequest;
  frames: { t: number; event: PipelineEvent }[];
}

const RUN = recorded as unknown as RecordedRun;
const MAX_GAP_MS = 2500; // the recording took 86 s; long waits are shortened to keep the demo near 30 s
const WALL_BAND = 0.6; // pieces this close to a wall keep their distance to it
const DOOR_CLEARANCE_SQM = 0.54; // per door, as the room stage measured in the recording

/** Move one plan coordinate from the recorded room size to the new one. */
function refit(value: number, recordedSize: number, size: number): number {
  if (value < WALL_BAND) return value;
  if (recordedSize - value < WALL_BAND) return size - (recordedSize - value);
  return value + (size - recordedSize) / 2; // the middle group moves as one, so chairs stay around the table
}

/**
 * Fit the recorded rectangle layout to the brief's room: floor pieces go into the largest rectangle
 * inside the floor (all of it for a rectangle), and wall pieces move onto the nearest real wall,
 * facing into the room, so an L or T room never leaves art hanging in open floor.
 */
function refitManifest(manifest: RenderManifest, request: PipelineRequest): RenderManifest {
  const [w0, l0] = manifest.room_area;
  const { min, max } = largestInnerRect(request.room_vertices);
  const edges = polygonEdges(request.room_vertices);
  const raise = request.wall_height - manifest.wall_height;
  const layout = Object.fromEntries(
    Object.entries(manifest.layout).map(([key, placement]): [string, Placement] => {
      const [x, y, z] = placement.position;
      const asset = manifest.assets[key];
      const fitted: [number, number] = [min[0] + refit(x, w0, max[0] - min[0]), min[1] + refit(y, l0, max[1] - min[1])];
      if (asset?.placement_mode === "ceiling_mounted") return [key, { ...placement, position: [...fitted, z + raise] }];
      if (asset?.placement_mode !== "wall_mounted") return [key, { ...placement, position: [...fitted, z] }];

      const gap = Math.min(x, w0 - x, y, l0 - y); // how far the piece stood off its recorded wall
      const nearest = edges.reduce((best, edge) => (distanceTo(fitted, edge) < distanceTo(fitted, best) ? edge : best));
      const half = Math.min(asset.width / 2 + 0.1, nearest.length / 2);
      const along = Math.min(nearest.length - half, Math.max(half, projection(fitted, nearest)));
      const [dx, dy] = [(nearest.end[0] - nearest.start[0]) / nearest.length, (nearest.end[1] - nearest.start[1]) / nearest.length];
      const [nx, ny] = nearest.normal;
      return [
        key,
        {
          ...placement,
          position: [nearest.start[0] + dx * along + nx * gap, nearest.start[1] + dy * along + ny * gap, z],
          rotation: [0, 0, Math.atan2(ny, nx)], // recorded wall art faces along the inward normal
        },
      ];
    }),
  );
  return {
    ...manifest,
    room_area: request.room_area,
    room_vertices: request.room_vertices,
    room_doors: request.room_doors,
    room_windows: request.room_windows,
    wall_height: request.wall_height,
    layout,
  };
}

type Edge = ReturnType<typeof polygonEdges>[number];

function projection([x, y]: [number, number], edge: Edge): number {
  return ((x - edge.start[0]) * (edge.end[0] - edge.start[0]) + (y - edge.start[1]) * (edge.end[1] - edge.start[1])) / edge.length;
}

function distanceTo(point: [number, number], edge: Edge): number {
  const t = Math.min(edge.length, Math.max(0, projection(point, edge))) / edge.length;
  return Math.hypot(
    point[0] - (edge.start[0] + t * (edge.end[0] - edge.start[0])),
    point[1] - (edge.start[1] + t * (edge.end[1] - edge.start[1])),
  );
}

function refitEvent(event: PipelineEvent, request: PipelineRequest): PipelineEvent {
  const variant = (v: ReadyVariant): ReadyVariant => ({ ...v, render_manifest: refitManifest(v.render_manifest, request) });
  if (event.type === "variant_ready") return { ...event, data: { variant: variant(event.data.variant) } };
  if (event.type === "complete") return { ...event, data: { ...event.data, variants: event.data.variants.map(variant) } };
  if (event.type === "node_complete" && event.node === "extract_room") {
    const floor = polygonArea(request.room_vertices);
    const comfortable = floor >= 24;
    return {
      ...event,
      data: {
        room_area: request.room_area,
        wall_height: request.wall_height,
        doors: request.room_doors.length,
        windows: request.room_windows.length,
        floor_area_sqm: floor,
        usable_area_sqm: floor - DOOR_CLEARANCE_SQM * request.room_doors.length,
        protected_paths: 0,
        fit: comfortable ? "comfortable" : "tight",
        fit_message: comfortable ? "This room should fit comfortably." : "This room may feel tight.",
      },
    };
  }
  return event;
}

/** Calls `onEvent` once per pipeline event, as the SSE stream would, until the run ends or `signal` aborts. */
export async function mockPipeline(request: PipelineRequest, signal: AbortSignal, onEvent: (event: PipelineEvent) => void) {
  let previous = 0;
  for (const { t, event } of RUN.frames) {
    const wait = Math.min(t - previous, MAX_GAP_MS);
    previous = t;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, wait);
      signal.addEventListener("abort", () => (clearTimeout(timer), reject(signal.reason)), { once: true });
    });
    onEvent(refitEvent(event, request));
  }
}
