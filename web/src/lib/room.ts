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
  doorAt: number; // the door centre along its wall, 0 at the wall's start and 1 at its end
  windowWall: number;
  windowAt: number;
}

export const DEFAULT_DRAFT: RoomDraft = {
  prompt: "A calm dining room for four with a coordinated table and chairs, two paintings on the walls, and a pendant above the table.",
  roomType: "dining_room",
  budget: 10000,
  shape: "Rectangle",
  corner: "top-right",
  width: 5.5,
  length: 5.8,
  height: 2.7,
  doorWall: 2, // the back wall of a rectangle
  doorAt: 0.28,
  windowWall: 2,
  windowAt: 0.72,
};

export const DOOR_WIDTH = 0.9;
export const WINDOW_WIDTH = 1.1;
// Door and window share a wall without overlapping: the door sits at 28%, the window at 72%.
const SHARED_SPOTS = [0.28, 0.72];

/** Openings placed on these walls: centred on their own walls, or at the shared spots on one wall. */
export function placeOpenings(draft: RoomDraft): RoomDraft {
  const [doorAt, windowAt] = draft.doorWall === draft.windowWall ? SHARED_SPOTS : [0.5, 0.5];
  return { ...draft, doorAt, windowAt };
}

export const draftVertices = (draft: RoomDraft) => shapeVertices(draft.shape, draft.corner, draft.width, draft.length);

export const draftEdges = (draft: RoomDraft) => polygonEdges(draftVertices(draft));

const round = (value: number) => Math.round(value * 1000) / 1000;

function pointOn(edge: Edge, t: number): Vec2 {
  return [round(edge.start[0] + t * (edge.end[0] - edge.start[0])), round(edge.start[1] + t * (edge.end[1] - edge.start[1]))];
}

/**
 * An opening `size` wide at `t` along the edge, sent as the room builder sends them: a `size` x 0.05 box
 * on every wall. The pipeline finds the wall from the centre.
 */
function opening(edge: Edge, t: number, size: number, rest: Pick<Opening, "id" | "height" | "sill_height">): Opening {
  return { ...rest, center: pointOn(edge, t), width: size, depth: 0.05 };
}

export function buildRequest(draft: RoomDraft): PipelineRequest {
  const vertices = draftVertices(draft);
  const edges = polygonEdges(vertices);
  return {
    user_intent: draft.prompt.trim(),
    budget: draft.budget,
    room_type: draft.roomType,
    room_area: [draft.width, draft.length],
    room_vertices: vertices.map(([x, y]) => [round(x), round(y)]),
    wall_height: draft.height,
    room_doors: [opening(edges[draft.doorWall], draft.doorAt, DOOR_WIDTH, { id: "door-1", height: 2.08 })],
    room_windows: [opening(edges[draft.windowWall], draft.windowAt, WINDOW_WIDTH, { id: "window-1", height: 1.2, sill_height: 0.9 })],
  };
}

/** Why the openings do not fit the chosen walls, or null when they do. */
export function openingsProblem(draft: RoomDraft): string | null {
  const edges = draftEdges(draft);
  const door = edges[draft.doorWall];
  const window = edges[draft.windowWall];
  if (!door || !window) return "Pick a wall for the door and the window.";
  // Each opening keeps 0.1 m to the wall's ends, and 0.05 m to the other opening on a shared wall.
  const fits = (edge: Edge, at: number, size: number) => Math.min(at, 1 - at) * edge.length >= size / 2 + 0.1 - 1e-6;
  if (!fits(door, draft.doorAt, DOOR_WIDTH)) return "The door does not fit on that wall.";
  if (!fits(window, draft.windowAt, WINDOW_WIDTH)) return "The window does not fit on that wall.";
  if (door === window && Math.abs(draft.doorAt - draft.windowAt) * door.length < (DOOR_WIDTH + WINDOW_WIDTH) / 2 + 0.05)
    return "The door and window do not both fit on that wall.";
  return null;
}

export const ROOM_TYPES: { value: RoomType; label: string }[] = [
  { value: "living_room", label: "Living room" },
  { value: "bedroom", label: "Bedroom" },
  { value: "dining_room", label: "Dining room" },
  { value: "studio", label: "Studio" },
];

export interface Example {
  group: string; // the list heading: a room type, or "Benchmark cases"
  label: string;
  draft: RoomDraft;
}

/** A room's example; door and window are [wall index, position along the wall]. Cut corners are the builder's defaults. */
function example(
  group: string,
  label: string,
  roomType: RoomType,
  prompt: string,
  budget: number,
  [width, length, height]: [number, number, number],
  [doorWall, doorAt]: [number, number],
  [windowWall, windowAt]: [number, number],
  shape: ShapeId = "Rectangle",
): Example {
  const corner = shape === "Angled" ? "top-left" : "top-right";
  return { group, label, draft: { prompt, roomType, budget, shape, corner, width, length, height, doorWall, doorAt, windowWall, windowAt } };
}

// Rectangle walls in vertex order: 0 front, 1 right, 2 back, 3 left. The door goes on the back wall
// near a corner and the window on the left wall, both in view of the preview.
const DOOR: [number, number] = [2, 0.25];
const WINDOW: [number, number] = [3, 0.5];

// Prompt starters from web-pipeline/components/dashboard/DesignPromptSection.tsx, each with a room size and budget that suit it.
// Benchmark cases are the requests in pipeline/scripts/benchmark_requests.json: the same prompt, budget, room size,
// ceiling and opening positions, with this form's standard 0.9 m door and 1.1 m window (the studio case's first window).
export const EXAMPLES: Example[] = [
  example("Living room", "Warm minimal", "living_room",
    "A warm, minimal living room in oak and linen. A three-seat sofa, an armchair, a coffee table, and a rug, with the window left clear for natural light.",
    9000, [5.2, 6, 2.7], DOOR, WINDOW),
  example("Living room", "Coastal calm", "living_room",
    "An airy coastal living room in sandy neutrals with pale blue accents. A linen sofa, two accent chairs, a round coffee table, and a jute rug. No nautical decor.",
    10000, [5.5, 6.4, 2.7], DOOR, WINDOW),
  example("Living room", "Mid-century", "living_room",
    "A mid-century living room with walnut and mustard accents. A low sofa, a lounge chair with a floor lamp for reading, a coffee table, and a TV stand. Keep walkways open.",
    8000, [4.8, 5.6, 2.7], DOOR, WINDOW),
  example("Bedroom", "Warm minimal", "bedroom",
    "A warm, minimal bedroom in oak and soft neutrals. A queen bed with two nightstands and a dresser, with clear space to walk around the bed.",
    6000, [3.8, 4.2, 2.7], DOOR, WINDOW),
  example("Bedroom", "Cosy Scandi", "bedroom",
    "A cosy Scandinavian bedroom with wool and boucle textures. A wooden double bed, a nightstand with a reading lamp, and a wardrobe for clothes.",
    5000, [3.6, 4, 2.7], DOOR, WINDOW),
  example("Bedroom", "Moody industrial", "bedroom",
    "A moody industrial bedroom in charcoal, leather, and black metal. A king bed with two nightstands, a dresser, and warm bedside lamps.",
    8000, [4.2, 4.6, 2.7], DOOR, WINDOW),
  example("Bedroom", "Bright Japandi", "bedroom",
    "A bright Japandi bedroom with a low queen platform bed, light wood nightstands, a bench at the foot of the bed, and a tall plant. Calm and uncluttered.",
    7000, [4, 4.4, 2.7], DOOR, WINDOW),
  example("Dining room", "Warm minimal", "dining_room",
    "A warm, minimal dining room for four: an oak table with four matching chairs and a pendant above. Leave room to pull out every chair.",
    6000, [3.6, 4, 2.7], DOOR, WINDOW),
  example("Dining room", "Family dining", "dining_room",
    "A contemporary dining room for six: a long rectangular table, six comfortable chairs, and a sideboard for dishes. Keep space around every chair.",
    9000, [4.4, 5.4, 2.7], DOOR, WINDOW),
  example("Dining room", "Dark modern", "dining_room",
    "A dark, moody modern dining room for four: a black oval table, four velvet chairs, a large painting on the wall, and a statement pendant above the table.",
    8000, [3.8, 4.4, 2.7], DOOR, WINDOW),
  example("Studio", "Warm minimal", "studio",
    "A warm, minimal studio in oak and linen. A double bed, a compact sofa, and a small dining table with two chairs, with clear paths between each area.",
    8000, [5.2, 6.6, 2.7], DOOR, WINDOW),
  example("Studio", "Cosy Scandi", "studio",
    "A cosy Scandinavian studio with a wooden double bed, a loveseat, and a round dining table with two chairs. Soft lighting, and keep the entrance clear.",
    8000, [5, 6.5, 2.7], DOOR, WINDOW),
  example("Studio", "Movie evenings", "studio",
    "A relaxed studio for movie nights: a queen bed, a sofa facing a TV stand, and a dining table with two chairs. Keep the TV easy to see from the sofa.",
    9000, [5.5, 7, 2.7], DOOR, WINDOW),
  example("Studio", "Artful studio", "studio",
    "A calm, gallery-like studio with a double bed, a compact sofa on a rug, a dining table with two chairs, and two paintings on the walls.",
    9000, [5.4, 6.8, 2.7], DOOR, WINDOW),
  example("Benchmark cases", "living_room", "living_room",
    "A cozy modern living room with a cream sofa, a wooden coffee table, a floor lamp for reading, and a TV for movie nights.",
    7000, [5, 6, 2.7], [0, 0.5], [2, 0.5]),
  example("Benchmark cases", "bedroom", "bedroom",
    "A calm Scandinavian bedroom with a queen bed, two nightstands with table lamps, and a dresser for clothes.",
    6000, [4, 4.5, 2.7], [0, 0.5], [2, 0.5]),
  example("Benchmark cases", "dining_room", "dining_room",
    "A cozy Scandinavian dining room for four with light wood and soft neutral dining chairs. Keep the floor bare and the room uncluttered.",
    7000, [4.2672, 4.8768, 3.3528], [2, 0.72], [2, 0.28]),
  example("Benchmark cases", "studio", "studio",
    "A compact modern studio apartment with a bed, a small sofa facing a TV, and a dining table for two.",
    8000, [5.5, 7, 2.7], [0, 0.5], [2, 0.6667]),
  example("Benchmark cases", "living_room_scandi", "living_room",
    "A cosy Scandinavian living room — soft neutrals, wool and boucle textures, low wooden furniture, airy and serene.",
    10000, [4.27, 4.88, 2.7], [2, 0.2801], [2, 0.7199]),
  example("Benchmark cases", "studio_relaxed", "studio",
    "A relaxed studio with a complete bed, a sofa facing a TV, and dining for two. Keep the TV comfortable to watch from the sofa and allow space to pull out the dining chairs.",
    10000, [5, 6.5, 2.7], [2, 0.28], [2, 0.72]),
  example("Benchmark cases", "dining_room_tight", "dining_room",
    "A cosy Scandinavian dining room for four with a round light wood table, four soft upholstered chairs, and a rug underneath. Keep the door clear.",
    10000, [4, 4.5, 2.7], [2, 0.28], [2, 0.72]),
  example("Benchmark cases", "bedroom_small", "bedroom",
    "A calm small bedroom with a queen bed, two nightstands, and a dresser.",
    6000, [3, 3, 2.7], [2, 0.2], [0, 0.5]),
  example("Benchmark cases", "living_room_small", "living_room",
    "A compact living room with a sofa, a coffee table, an armchair, and a TV.",
    5000, [3, 3, 2.7], [2, 0.2], [0, 0.5]),
  example("Benchmark cases", "studio_small", "studio",
    "A small studio with a full bed, a loveseat facing a TV, and dining for two.",
    8000, [4, 4.5, 2.7], [2, 0.15], [0, 0.5]),
  example("Benchmark cases", "living_room_large", "living_room",
    "A spacious family living room with a large sectional sofa, two armchairs, a coffee table, a TV, and a reading corner with a floor lamp.",
    15000, [7, 8, 2.8], [2, 0.1429], [0, 0.5]),
  example("Benchmark cases", "bedroom_large", "bedroom",
    "A large primary bedroom with a king bed, two nightstands, a dresser, a bench at the foot of the bed, and a reading chair.",
    12000, [5.5, 6, 2.8], [2, 0.1636], [0, 0.5]),
  example("Benchmark cases", "dining_room_large", "dining_room",
    "A formal dining room for eight with a long wooden table, eight upholstered chairs, a sideboard, and a large rug.",
    15000, [5.5, 6.5, 2.8], [2, 0.1636], [0, 0.5]),
  // Each shape's door on its outer back wall and window on its left wall, as the preview shows them.
  example("Room shapes", "L-shaped living room", "living_room",
    "A warm living room in the L's main space: a three-seat sofa, an armchair, a coffee table, and a rug, with a reading lamp in the short leg.",
    9000, [5.5, 6.5, 2.7], [4, 0.5], [5, 0.5], "L-Shaped"),
  example("Room shapes", "Alcove bedroom", "bedroom",
    "A calm bedroom with the queen bed set into the alcove, two nightstands, and a dresser on the main wall.",
    7000, [4.5, 5, 2.7], [7, 0.5], [4, 0.5], "Alcove"),
  example("Room shapes", "T-shaped living room", "living_room",
    "A family living room across the wide bar: a sofa facing a TV stand, two armchairs, and a coffee table. Keep the stem clear as the entrance.",
    10000, [6, 6.5, 2.7], [4, 0.3], [5, 0.5], "T-Shaped"),
  example("Room shapes", "Angled dining room", "dining_room",
    "A modern dining room for six with a long table, six chairs, and a sideboard along the straight wall. Keep the angled corner open.",
    9000, [4.5, 5.5, 2.7], [2, 0.4], [4, 0.5], "Angled"),
  example("Room shapes", "Z-shaped studio", "studio",
    "A studio with a double bed in the back half, a compact sofa facing a TV in the front half, and a small dining table for two between them.",
    9000, [5.5, 7, 2.7], [4, 0.5], [5, 0.5], "Z-Shaped"),
  example("Room shapes", "U-shaped living room", "living_room",
    "A living room with a sofa and coffee table in one leg of the U, a reading chair and floor lamp in the other, and a rug tying them together.",
    10000, [6, 6, 2.7], [7, 0.5], [6, 0.5], "U-Shaped"),
];

export const roomLabel = (type: RoomType) => ROOM_TYPES.find((room) => room.value === type)?.label ?? type;

/** `/generate` query params, named as the legacy app names them; `room` packs the geometry inputs. */
export function draftToParams(draft: RoomDraft): URLSearchParams {
  const room = [draft.width, draft.length, draft.height, draft.shape, draft.corner, draft.doorWall, draft.windowWall, draft.doorAt, draft.windowAt].join(",");
  return new URLSearchParams({ roomPurpose: draft.roomType, budget: String(draft.budget), prompt: draft.prompt, room });
}

/** Read `/generate` params back; null when any of them is missing or malformed. */
export function draftFromParams(params: URLSearchParams): RoomDraft | null {
  const [width, length, height, shape, corner, doorWall, windowWall, doorAt, windowAt] = (params.get("room") ?? "").split(",");
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
    doorAt: Number(doorAt),
    windowWall: Number(windowWall),
    windowAt: Number(windowAt),
  };
  const valid =
    draft.prompt.trim() &&
    ROOM_TYPES.some((room) => room.value === draft.roomType) &&
    SHAPES.some((s) => s.value === draft.shape) &&
    CORNERS.some((c) => c.value === draft.corner) &&
    [draft.budget, draft.width, draft.length, draft.height].every((value) => Number.isFinite(value) && value > 0) &&
    Number.isInteger(draft.doorWall) &&
    Number.isInteger(draft.windowWall) &&
    [draft.doorAt, draft.windowAt].every((at) => at > 0 && at < 1) &&
    !openingsProblem(draft);
  return valid ? draft : null;
}
