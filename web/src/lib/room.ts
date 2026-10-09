import { CORNERS, polygonEdges, SHAPES, shapeVertices, type CutCorner, type Edge, type ShapeId } from "./shapes";
import type { Opening, PipelineRequest, RoomType, Vec2 } from "./types";

export interface RoomDraft {
  prompt: string;
  roomType: RoomType;
  budget: number;
  shape: ShapeId;
  corner: CutCorner; // the cut corner of an L-shaped or angled room
  width: number; // bounding box, plan X, metres
  length: number; // bounding box, plan Y, metres
  height: number;
  doorWall: number; // edge index into the floor polygon
  windowWall: number;
}

export const DEFAULT_DRAFT: RoomDraft = {
  prompt: "A calm dining room for four with a coordinated table and chairs, two paintings on the walls, and a pendant above the table.",
  roomType: "dining_room",
  budget: 10000,
  shape: "Rectangle",
  corner: "top-right",
  width: 4.27,
  length: 4.88,
  height: 2.7,
  doorWall: 2, // the back wall of a rectangle
  windowWall: 2,
};

export const DOOR_WIDTH = 0.9;
export const WINDOW_WIDTH = 1.1;
// Door and window share a wall without overlapping: the door sits at 28%, the window at 72%.
const SHARED_SPOTS = [0.28, 0.72];
// Legacy wizard wall names for rectangles (web-pipeline lib/wizard/buildRoomGeometry.ts).
const LEGACY_WALL = { "Back wall": "top", "Front wall": "bottom", "Left wall": "left", "Right wall": "right" } as const;

export const draftVertices = (draft: RoomDraft) => shapeVertices(draft.shape, draft.corner, draft.width, draft.length);

export const draftEdges = (draft: RoomDraft) => polygonEdges(draftVertices(draft));

const round = (value: number) => Math.round(value * 1000) / 1000;

function pointOn(edge: Edge, t: number): Vec2 {
  return [round(edge.start[0] + t * (edge.end[0] - edge.start[0])), round(edge.start[1] + t * (edge.end[1] - edge.start[1]))];
}

/**
 * An opening `size` wide at `t` along the edge. As the legacy wizard sends them, a door box is always
 * `size` x 0.05, and a window box turns to 0.05 x `size` on a side wall (`turnOnSideWall`).
 */
function opening(
  edge: Edge,
  t: number,
  size: number,
  turnOnSideWall: boolean,
  rectangle: boolean,
  rest: Pick<Opening, "id" | "height" | "sill_height">,
): Opening {
  const turned = turnOnSideWall && Math.abs(edge.normal[0]) > 0.7;
  // Rectangles carry the legacy wall name; on other shapes the pipeline finds the wall from the centre.
  const wall = rectangle ? LEGACY_WALL[edge.label as keyof typeof LEGACY_WALL] : undefined;
  return { ...rest, center: pointOn(edge, t), width: turned ? 0.05 : size, depth: turned ? size : 0.05, ...(wall ? { wall } : {}) };
}

export function buildRequest(draft: RoomDraft): PipelineRequest {
  const vertices = draftVertices(draft);
  const edges = polygonEdges(vertices);
  const shared = draft.doorWall === draft.windowWall;
  const rectangle = draft.shape === "Rectangle";
  return {
    user_intent: draft.prompt.trim(),
    budget: draft.budget,
    room_type: draft.roomType,
    room_area: [draft.width, draft.length],
    room_vertices: vertices.map(([x, y]) => [round(x), round(y)]),
    wall_height: draft.height,
    room_doors: [
      opening(edges[draft.doorWall], shared ? SHARED_SPOTS[0] : 0.5, DOOR_WIDTH, false, rectangle, { id: "door-1", height: 2.08 }),
    ],
    room_windows: [
      opening(edges[draft.windowWall], shared ? SHARED_SPOTS[1] : 0.5, WINDOW_WIDTH, true, rectangle, {
        id: "window-1",
        height: 1.2,
        sill_height: 0.9,
      }),
    ],
  };
}

/** Why the openings do not fit the chosen walls, or null when they do. */
export function openingsProblem(draft: RoomDraft): string | null {
  const edges = draftEdges(draft);
  const door = edges[draft.doorWall];
  const window = edges[draft.windowWall];
  if (!door || !window) return "Pick a wall for the door and the window.";
  if (door.length < DOOR_WIDTH + 0.2) return "The door does not fit on that wall.";
  if (window.length < WINDOW_WIDTH + 0.2) return "The window does not fit on that wall.";
  if (door === window && door.length < 2.4) return "The door and window do not both fit on that wall.";
  return null;
}

export const ROOM_TYPES: { value: RoomType; label: string }[] = [
  { value: "living_room", label: "Living room" },
  { value: "bedroom", label: "Bedroom" },
  { value: "dining_room", label: "Dining room" },
  { value: "studio", label: "Studio" },
];

export const roomLabel = (type: RoomType) => ROOM_TYPES.find((room) => room.value === type)?.label ?? type;

/** `/generate` query params, named as the legacy app names them; `room` packs the geometry inputs. */
export function draftToParams(draft: RoomDraft): URLSearchParams {
  const room = [draft.width, draft.length, draft.height, draft.shape, draft.corner, draft.doorWall, draft.windowWall].join(",");
  return new URLSearchParams({ roomPurpose: draft.roomType, budget: String(draft.budget), prompt: draft.prompt, room });
}

/** Read `/generate` params back; null when any of them is missing or malformed. */
export function draftFromParams(params: URLSearchParams): RoomDraft | null {
  const [width, length, height, shape, corner, doorWall, windowWall] = (params.get("room") ?? "").split(",");
  const draft: RoomDraft = {
    prompt: params.get("prompt") ?? "",
    roomType: params.get("roomPurpose") as RoomType,
    budget: Number(params.get("budget")),
    shape: shape as ShapeId,
    corner: corner as CutCorner,
    width: Number(width),
    length: Number(length),
    height: Number(height),
    doorWall: Number(doorWall),
    windowWall: Number(windowWall),
  };
  const valid =
    draft.prompt.trim() &&
    ROOM_TYPES.some((room) => room.value === draft.roomType) &&
    SHAPES.some((s) => s.value === draft.shape) &&
    CORNERS.some((c) => c.value === draft.corner) &&
    [draft.budget, draft.width, draft.length, draft.height].every((value) => Number.isFinite(value) && value > 0) &&
    Number.isInteger(draft.doorWall) &&
    Number.isInteger(draft.windowWall) &&
    !openingsProblem(draft);
  return valid ? draft : null;
}
