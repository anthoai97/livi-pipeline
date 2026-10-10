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
  width: 5.5,
  length: 5.8,
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

// Prompt starters from web-pipeline/components/dashboard/DesignPromptSection.tsx.
// Each starter carries a room size that suits it; Cosy Scandi living room reproduces a recorded benchmark case.
export const PROMPT_STARTERS: { roomType: RoomType; label: string; prompt: string; width: number; length: number }[] = [
  {
    roomType: "living_room", label: "Warm minimal", width: 5.2, length: 6,
    prompt: "A warm, minimal living room in oak and linen. A three-seat sofa, an armchair, a coffee table, and a rug, with the window left clear for natural light.",
  },
  {
    roomType: "living_room", label: "Cosy Scandi", width: 4.27, length: 4.88,
    prompt: "A cosy Scandinavian living room — soft neutrals, wool and boucle textures, low wooden furniture, airy and serene.",
  },
  {
    roomType: "living_room", label: "Coastal calm", width: 5.5, length: 6.4,
    prompt: "An airy coastal living room in sandy neutrals with pale blue accents. A linen sofa, two accent chairs, a round coffee table, and a jute rug. No nautical decor.",
  },
  {
    roomType: "living_room", label: "Mid-century", width: 4.8, length: 5.6,
    prompt: "A mid-century living room with walnut and mustard accents. A low sofa, a lounge chair with a floor lamp for reading, a coffee table, and a TV stand. Keep walkways open.",
  },
  {
    roomType: "bedroom", label: "Warm minimal", width: 3.8, length: 4.2,
    prompt: "A warm, minimal bedroom in oak and soft neutrals. A queen bed with two nightstands and a dresser, with clear space to walk around the bed.",
  },
  {
    roomType: "bedroom", label: "Cosy Scandi", width: 3.6, length: 4,
    prompt: "A cosy Scandinavian bedroom with wool and boucle textures. A wooden double bed, a nightstand with a reading lamp, and a wardrobe for clothes.",
  },
  {
    roomType: "bedroom", label: "Moody industrial", width: 4.2, length: 4.6,
    prompt: "A moody industrial bedroom in charcoal, leather, and black metal. A king bed with two nightstands, a dresser, and warm bedside lamps.",
  },
  {
    roomType: "bedroom", label: "Bright Japandi", width: 4, length: 4.4,
    prompt: "A bright Japandi bedroom with a low queen platform bed, light wood nightstands, a bench at the foot of the bed, and a tall plant. Calm and uncluttered.",
  },
  {
    roomType: "dining_room", label: "Warm minimal", width: 3.6, length: 4,
    prompt: "A warm, minimal dining room for four: an oak table with four matching chairs and a pendant above. Leave room to pull out every chair.",
  },
  {
    roomType: "dining_room", label: "Cosy Scandi", width: 3.4, length: 3.8,
    prompt: "A cosy Scandinavian dining room for four with a round light wood table, four soft upholstered chairs, and a rug underneath. Keep the door clear.",
  },
  {
    roomType: "dining_room", label: "Family dining", width: 4.4, length: 5.4,
    prompt: "A contemporary dining room for six: a long rectangular table, six comfortable chairs, and a sideboard for dishes. Keep space around every chair.",
  },
  {
    roomType: "dining_room", label: "Dark modern", width: 3.8, length: 4.4,
    prompt: "A dark, moody modern dining room for four: a black oval table, four velvet chairs, a large painting on the wall, and a statement pendant above the table.",
  },
  {
    roomType: "studio", label: "Warm minimal", width: 5.2, length: 6.6,
    prompt: "A warm, minimal studio in oak and linen. A double bed, a compact sofa, and a small dining table with two chairs, with clear paths between each area.",
  },
  {
    roomType: "studio", label: "Cosy Scandi", width: 5, length: 6.5,
    prompt: "A cosy Scandinavian studio with a wooden double bed, a loveseat, and a round dining table with two chairs. Soft lighting, and keep the entrance clear.",
  },
  {
    roomType: "studio", label: "Movie evenings", width: 5.5, length: 7,
    prompt: "A relaxed studio for movie nights: a queen bed, a sofa facing a TV stand, and a dining table with two chairs. Keep the TV easy to see from the sofa.",
  },
  {
    roomType: "studio", label: "Artful studio", width: 5.4, length: 6.8,
    prompt: "A calm, gallery-like studio with a double bed, a compact sofa on a rug, a dining table with two chairs, and two paintings on the walls.",
  },
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
