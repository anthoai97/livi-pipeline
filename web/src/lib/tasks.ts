import type { Task, TaskStatus } from "../components/ui";
import { humanize, metres, money, plural } from "./format";
import { nodeData, type NodeState, type VariantProgress } from "./run";
import type { NodeName } from "./types";

type Nodes = Partial<Record<NodeName, NodeState>>;

const status = (node?: NodeState): TaskStatus => (!node ? "queued" : node.status === "done" ? "done" : "working");

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
        ? [asked && `Asked for ${asked}`, brief.style_hints?.length && brief.style_hints.join(", ")].filter(Boolean).join(", ") ||
          "Room details read"
        : "Identifying furniture and style from your description",
    },
    {
      id: "shell",
      title: "Room dimensions",
      status: roomStatus,
      detail: room?.room_area
        ? `${walls === 4 ? "" : `${walls} walls, `}${metres(room.room_area[0])} x ${metres(room.room_area[1])}${walls === 4 ? "" : " overall"}, ${metres(room.wall_height ?? 0)} ceiling`
        : "Walls, floor, and ceiling height",
    },
    {
      id: "openings",
      title: "Doors and windows",
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
        : "Calculating floor space outside door clearances",
    },
    {
      id: "fit",
      title: "Space check",
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

export function rankTask(shared: Nodes): Task {
  const ranked = nodeData(shared, "select_asset_intent");
  return {
    id: "rank", title: "Comparing products", status: status(shared.select_asset_intent),
    detail: ranked
      ? `${plural(ranked.ranked_slots ?? 0, "need")} ranked across three designs, ${plural(ranked.shared_products ?? 0, "shared product")}`
      : "Comparing products for each design",
  };
}

/** A design's row, reporting where the design really is whichever step the page shows. */
export function designTask(variant: VariantProgress, index: number): Task {
  const base = { id: `design-${index}`, title: designName(index) };
  if (variant.ready) {
    const { selected_assets, total_cost } = variant.ready;
    return { ...base, status: "done", detail: `${plural(selected_assets.length, "piece")}, ${money(total_cost)}` };
  }
  if (variant.failed) return { ...base, status: "failed", detail: variant.failed.message };
  const { select_asset_intent: selecting, layout_initial: placed, layout_fix: fixing } = variant.nodes;
  if (placed) {
    if (placed.status === "working") return { ...base, status: "working", detail: "Placing every piece in the room" };
    if (fixing?.status === "working") {
      return { ...base, status: "working", detail: `Adjusting furniture positions${fixing.runs > 1 ? `, pass ${fixing.runs}` : ""}` };
    }
    const initial = nodeData(variant.nodes, "layout_initial");
    const repair = nodeData(variant.nodes, "layout_fix");
    const blocking = repair?.blocking ?? initial?.blocking;
    const changes = [
      initial?.swaps ? `${plural(initial.swaps, "product swap")}` : "",
      initial?.drops ? `${plural(initial.drops, "piece")} removed` : "",
    ].filter(Boolean).join(", ");
    // Passing the checks is not the end: the final check and delivery are still to come.
    if (blocking === 0) return { ...base, status: "working", detail: `Layout checks passed${changes ? `; ${changes}` : ""}` };
    return {
      ...base,
      status: "working",
      detail: `${blocking === undefined ? "Checking layout issues" : `${plural(blocking, "blocking layout issue")} remaining`}${repair ? (repair.improved ? "; spacing improved" : "; keeping the previous layout") : ""}`,
    };
  }
  if (selecting) {
    const picked = nodeData(variant.nodes, "select_asset_intent");
    if (selecting.status === "done" && picked?.valid) {
      const compact = picked.fit_step === "compact" ? ", sized down to fit" : "";
      return { ...base, status: "working", detail: `${plural(picked.items?.length ?? 0, "product")}, ${money(picked.total_cost ?? 0)}${compact}` };
    }
    return {
      ...base,
      status: "working",
      detail: selecting.runs > 1 ? `Try ${selecting.runs}: replacing products that do not fit` : "Selecting furniture and decor",
    };
  }
  return { ...base, status: "queued", detail: "Waiting for product comparisons" };
}

/** Where a design is, in a few words, for the option switcher. */
export function variantStage(variant: VariantProgress): string {
  if (variant.ready) return money(variant.ready.total_cost);
  if (variant.failed) return "Not completed";
  if (variant.nodes.layout_initial) return "Checking fit";
  if (variant.nodes.select_asset_intent) return "Choosing pieces";
  return "Waiting";
}
