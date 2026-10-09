import type { Task, TaskStatus } from "../components/ui";
import { humanize, metres, money, plural } from "./format";
import { nodeData, type NodeState, type VariantProgress } from "./run";
import type { NodeName } from "./types";

type Nodes = Partial<Record<NodeName, NodeState>>;

const status = (node?: NodeState): TaskStatus => (!node ? "queued" : node.status === "done" ? "done" : "working");

const FAILURES: Record<string, string> = {
  asset_selection_failed: "No set of pieces passed the room rules.",
  layout_validation_failed: "The layout did not pass the final check.",
  timeout: "Ran out of time before it finished.",
};

export const failureText = (reason: string) => FAILURES[reason] ?? "This design could not be completed.";

export const designName = (index: number) => `Design ${index + 1}`;

export function roomTasks(shared: Nodes, walls: number): Task[] {
  const brief = nodeData(shared, "interpret");
  const room = nodeData(shared, "extract_room");
  const roomStatus = status(shared.extract_room);
  const asked = (brief?.requested_categories ?? []).slice(0, 4).map(humanize).join(", ");
  const tight = room?.fit && room.fit !== "comfortable";
  return [
    {
      id: "brief",
      title: "Reading your brief",
      status: status(shared.interpret),
      detail: brief
        ? [asked && `Asked for ${asked}`, brief.style_hints?.length && `${brief.style_hints.join(", ")} feel`].filter(Boolean).join(", ") ||
          "Brief understood"
        : "Turning your words into a list of pieces",
    },
    {
      id: "shell",
      title: "Room shell",
      status: roomStatus,
      detail: room?.room_area
        ? `${walls === 4 ? "" : `${walls} walls, `}${metres(room.room_area[0])} x ${metres(room.room_area[1])}${walls === 4 ? "" : " overall"}, ${metres(room.wall_height ?? 0)} ceiling`
        : "Walls, floor, and ceiling height",
    },
    {
      id: "openings",
      title: "Openings",
      status: roomStatus,
      detail: room
        ? `${plural(room.doors ?? 0, "door")} and ${plural(room.windows ?? 0, "window")} kept clear`
        : "Doors and windows to keep clear",
    },
    {
      id: "floor",
      title: "Usable floor",
      status: roomStatus,
      detail: room?.usable_area_sqm
        ? `${room.usable_area_sqm.toFixed(1)} of ${room.floor_area_sqm?.toFixed(1)} m² free to furnish`
        : "Floor left after door clearance",
    },
    {
      id: "fit",
      title: "Fit estimate",
      status: room ? (tight ? "warn" : "done") : roomStatus,
      detail: room?.fit_message ?? "How much furniture the room can hold",
    },
  ];
}

export function catalogTask(shared: Nodes): Task {
  const found = nodeData(shared, "rag_scope_assets");
  const gaps = found?.gaps ?? [];
  return {
    id: "catalog",
    title: "Catalog search",
    status: gaps.length ? "warn" : status(shared.rag_scope_assets),
    detail: found
      ? `${found.candidates} products for ${found.slots} needs${gaps.length ? `. No match for ${gaps.join(", ")}` : ""}`
      : "Finding products that fit the room and budget",
  };
}

export function selectionTask(variant: VariantProgress, index: number): Task {
  const node = variant.nodes.select_asset_intent;
  const picked = nodeData(variant.nodes, "select_asset_intent");
  const base = { id: `select-${index}`, title: designName(index) };
  if (variant.failed && !picked?.valid) return { ...base, status: "failed", detail: failureText(variant.failed.reason) };
  if (!node) return { ...base, status: "queued", detail: "Waits for the catalog search" };
  if (node.status === "done" && picked?.valid) {
    const compact = picked.fit_step === "compact" ? ", sized down to fit" : "";
    return { ...base, status: "done", detail: `${plural(picked.items?.length ?? 0, "piece")}, ${money(picked.total_cost ?? 0)}${compact}` };
  }
  return {
    ...base,
    status: "working",
    detail: node.runs > 1 ? `Try ${node.runs}: swapping pieces that broke a room rule` : "Picking one product for each piece",
  };
}

export function fitTask(variant: VariantProgress, index: number): Task {
  const { layout_initial: placed, layout_fix: fixing, render_scene: final } = variant.nodes;
  const base = { id: `fit-${index}`, title: designName(index) };
  if (variant.failed) return { ...base, status: "failed", detail: failureText(variant.failed.reason) };
  if (!placed) return { ...base, status: "queued", detail: "Waits for its pieces" };
  if (placed.status === "working") return { ...base, status: "working", detail: "Placing every piece in the room" };
  const blocking = nodeData(variant.nodes, "layout_fix")?.blocking ?? nodeData(variant.nodes, "layout_initial")?.blocking ?? 0;
  if (final || variant.ready || (blocking === 0 && fixing?.status !== "working")) {
    return { ...base, status: "done", detail: "Nothing overlaps or blocks a walkway" };
  }
  return {
    ...base,
    status: "working",
    detail: `Fixing ${plural(blocking, "layout issue")}${fixing && fixing.runs > 1 ? `, pass ${fixing.runs}` : ""}`,
  };
}

export function readyTask(variant: VariantProgress, index: number): Task {
  const base = { id: `ready-${index}`, title: designName(index) };
  if (variant.ready) {
    const { selected_assets, total_cost } = variant.ready;
    return { ...base, status: "done", detail: `${plural(selected_assets.length, "piece")}, ${money(total_cost)}` };
  }
  if (variant.failed) return { ...base, status: "failed", detail: failureText(variant.failed.reason) };
  return { ...base, status: variant.nodes.layout_initial ? "working" : "queued", detail: "Still checking the fit" };
}

/** Where a design is, in a few words, for the option switcher. */
export function variantStage(variant: VariantProgress): string {
  if (variant.ready) return money(variant.ready.total_cost);
  if (variant.failed) return "Not completed";
  if (variant.nodes.layout_initial) return "Checking fit";
  if (variant.nodes.select_asset_intent) return "Choosing pieces";
  return "Waiting";
}
