import { create } from "zustand";
import { mockPipeline } from "./mock";
import type { NodeData, NodeName, PipelineEvent, PipelineRequest, ReadyVariant } from "./types";

export const VARIANT_COUNT = 3;

export interface NodeState {
  status: "working" | "done";
  runs: number; // how many times the node started (selection turns, correction proposals)
  elapsed?: number;
  doneAt?: number; // client time the node completed
  data?: Partial<NodeData[NodeName]>;
}

type Nodes = Partial<Record<NodeName, NodeState>>;

export interface VariantProgress {
  nodes: Nodes;
  current: NodeName | null;
  ready: ReadyVariant | null;
  failed: { reason: string; message: string } | null;
}

export type RunStatus = "idle" | "running" | "complete" | "error" | "cancelled";

interface RunState {
  status: RunStatus;
  key: string | null; // the /generate params this run belongs to
  request: PipelineRequest | null;
  startedAt: number;
  shared: Nodes;
  variants: VariantProgress[];
  error: string | null;
  selected: number; // variant index shown on /select-variant
  start: (request: PipelineRequest, key: string) => void;
  cancel: () => void;
  reset: () => void;
  select: (index: number) => void;
}

const emptyVariant = (): VariantProgress => ({ nodes: {}, current: null, ready: null, failed: null });

const idle = {
  status: "idle" as RunStatus,
  key: null,
  request: null,
  startedAt: 0,
  shared: {},
  variants: Array.from({ length: VARIANT_COUNT }, emptyVariant),
  error: null,
  selected: 0,
};

/** Typed read of a node's `node_complete.data`. */
export function nodeData<N extends NodeName>(nodes: Nodes, node: N): Partial<NodeData[N]> | undefined {
  return nodes[node]?.data as Partial<NodeData[N]> | undefined;
}

function applyNode(nodes: Nodes, event: Extract<PipelineEvent, { type: "node_start" | "node_complete" }>): Nodes {
  const previous = nodes[event.node];
  if (event.type === "node_start") {
    return { ...nodes, [event.node]: { ...previous, status: "working", runs: (previous?.runs ?? 0) + 1 } };
  }
  // A correction that does not improve keeps the earlier counts, which `layout_fix` data repeats.
  return {
    ...nodes,
    [event.node]: { runs: previous?.runs ?? 1, status: "done", elapsed: event.elapsed, doneAt: Date.now(), data: event.data },
  };
}

function reduce(state: RunState, event: PipelineEvent): Partial<RunState> {
  switch (event.type) {
    case "node_start":
    case "node_complete": {
      if (event.variant_index === null) return { shared: applyNode(state.shared, event) };
      const variants = [...state.variants];
      const variant = variants[event.variant_index];
      variants[event.variant_index] = { ...variant, nodes: applyNode(variant.nodes, event), current: event.node };
      return { variants };
    }
    case "variant_ready": {
      const ready = event.data.variant;
      const variants = [...state.variants];
      variants[ready.variant_index] = { ...variants[ready.variant_index], ready, current: null };
      return { variants };
    }
    case "variant_failed": {
      const variants = [...state.variants];
      variants[event.variant_index] = {
        ...variants[event.variant_index],
        failed: { reason: event.reason, message: event.message },
        current: null,
      };
      return { variants };
    }
    case "complete":
      return { status: "complete" };
    case "error":
      return { status: "error", error: event.message };
    default:
      return {};
  }
}

let controller: AbortController | null = null;

// Mock-only until the pipeline is ready: mockPipeline emits the pipeline's SSE events in order.
export const useRun = create<RunState>((set) => {
  async function run(request: PipelineRequest, key: string) {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    set({ ...idle, status: "running", key, request, startedAt: Date.now() });
    try {
      await mockPipeline(request, current.signal, (event) => set((state) => reduce(state, event)));
    } catch (error) {
      if (current.signal.aborted) return;
      set({ status: "error", error: error instanceof Error ? error.message : String(error) });
    }
  }

  return {
    ...idle,
    start: (request, key) => void run(request, key),
    cancel: () => {
      controller?.abort();
      set({ status: "cancelled" });
    },
    reset: () => {
      controller?.abort();
      set({ ...idle });
    },
    select: (selected) => set({ selected }),
  };
});

const STEP_OF: Record<NodeName, number> = {
  interpret: 1,
  extract_room: 1,
  rag_scope_assets: 2,
  select_asset_intent: 2,
  layout_initial: 3,
  layout_fix: 3,
  render_scene: 4,
};

/** The furthest step any part of the run has reached: variants run in parallel, the leader sets the step. */
export function currentStep(shared: Nodes, variants: VariantProgress[]): number {
  let step = 1;
  for (const nodes of [shared, ...variants.map((variant) => variant.nodes)]) {
    for (const node of Object.keys(nodes) as NodeName[]) step = Math.max(step, STEP_OF[node]);
  }
  return variants.some((variant) => variant.ready) ? 4 : step;
}

export const STEPS = ["Reading room", "Choosing pieces", "Checking fit", "Placing furniture"];
