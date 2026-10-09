import type { PipelineRequest, ReadyVariant } from "./types";

// Mock of the Studio V2 design service (legacy `POST /designs`, `GET /designs/{id}`), kept in
// localStorage so a /studio URL still opens after a reload.

export interface DesignIdentity {
  workspace: string;
  room: string;
  design: string;
}

export interface SavedDesign {
  identity: DesignIdentity;
  name: string;
  request: PipelineRequest;
  variant: ReadyVariant;
}

const KEY = "livinit.mock-designs.v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function readAll(): Record<string, SavedDesign> {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, SavedDesign>;
  } catch {
    return {};
  }
}

export function createDesign(variant: ReadyVariant, request: PipelineRequest, name: string): DesignIdentity {
  const identity = { workspace: crypto.randomUUID(), room: crypto.randomUUID(), design: crypto.randomUUID() };
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...readAll(), [identity.design]: { identity, name, request, variant } }));
  } catch {
    // Storage is full or blocked: Studio then shows its empty state for this link.
  }
  return identity;
}

/** Legacy `studioV2Href`. */
export const studioHref = ({ workspace, room, design }: DesignIdentity) =>
  `/studio?${new URLSearchParams({ v: "2", workspace, room, design })}`;

/** Legacy `parseStudioRouteIdentity` plus the design load: null for a bad link or an unknown design. */
export function loadDesign(params: URLSearchParams): SavedDesign | null {
  const [workspace, room, design] = ["workspace", "room", "design"].map((key) => params.get(key) ?? "");
  if (params.get("v") !== "2" || ![workspace, room, design].every((id) => UUID.test(id))) return null;
  const saved = readAll()[design];
  return saved && saved.identity.workspace === workspace && saved.identity.room === room ? saved : null;
}
