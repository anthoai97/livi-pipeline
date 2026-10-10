import { create } from "zustand";
import { pipeline } from "./pipeline";
import type { ClientTiming, NodeData, NodeName, PipelineEvent, PipelineRequest, ReadyVariant, SceneTiming } from "./types";

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
  receivedMs: number | null;
  timing: ClientTiming | null;
}

export type RunStatus = "idle" | "running" | "complete" | "error" | "cancelled";

interface RunState {
  status: RunStatus;
  key: string | null; // the /generate params this run belongs to
  request: PipelineRequest | null;
  startedAt: number;
  submittedAt: number; // monotonic clock for client timing
  runId: string | null;
  shared: Nodes;
  variants: VariantProgress[];
  error: string | null;
  selected: number; // variant index shown on /select-variant
  start: (request: PipelineRequest, key: string) => void;
  cancel: () => void;
  reset: () => void;
  select: (index: number) => void;
  recordTiming: (runId: string | null, variantId: string, screen: ClientTiming["screen"], timing: SceneTiming) => void;
}

const emptyVariant = (): VariantProgress => ({ nodes: {}, current: null, ready: null, failed: null, receivedMs: null, timing: null });

const idle = {
  status: "idle" as RunStatus,
  key: null,
  request: null,
  startedAt: 0,
  submittedAt: 0,
  runId: null,
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
  return {
    ...nodes,
    [event.node]: { runs: previous?.runs ?? 1, status: "done", elapsed: event.elapsed, doneAt: Date.now(), data: event.data },
  };
}

function reduce(state: RunState, event: PipelineEvent): Partial<RunState> {
  switch (event.type) {
    case "start":
      return { runId: event.run_id };
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
      variants[ready.variant_index] = { ...variants[ready.variant_index], ready, current: null, receivedMs: performance.now() - state.submittedAt };
      return { variants };
    }
    case "variant_failed": {
      const variants = [...state.variants];
      variants[event.variant_index] = {
        ...variants[event.variant_index],
        failed: { reason: event.reason, message: event.message || event.errors.join("; ") || event.reason },
        current: null,
      };
      return { variants };
    }
    case "complete":
      return { status: "complete", variants: stoppedVariants(state.variants, "This design did not finish.") };
    case "error":
      return { status: "error", error: event.message, variants: stoppedVariants(state.variants, event.message) };
    default:
      return {};
  }
}

function stoppedVariants(variants: VariantProgress[], message: string): VariantProgress[] {
  return variants.map((variant) => variant.ready || variant.failed ? variant : {
    ...variant, current: null, failed: { reason: "stopped", message },
  });
}

let controller: AbortController | null = null;

export const useRun = create<RunState>((set, get) => {
  async function run(request: PipelineRequest, key: string) {
    controller?.abort();
    const current = new AbortController();
    controller = current;
    set({ ...idle, status: "running", key, request, startedAt: Date.now(), submittedAt: performance.now() });
    try {
      await pipeline(request, current.signal, (event) => {
        if (controller === current && !current.signal.aborted) set((state) => reduce(state, event));
      });
    } catch (error) {
      if (current.signal.aborted || controller !== current) return;
      const message = error instanceof Error ? error.message : String(error);
      set((state) => ({ status: "error", error: message, variants: stoppedVariants(state.variants, message) }));
    }
  }

  return {
    ...idle,
    start: (request, key) => void run(request, key),
    cancel: () => {
      controller?.abort();
      set((state) => ({ status: "cancelled", variants: stoppedVariants(state.variants, "Generation stopped.") }));
    },
    reset: () => {
      controller?.abort();
      set({ ...idle });
    },
    select: (selected) => set({ selected }),
    recordTiming: (runId, variantId, screen, timing) => {
      const state = get();
      if (!runId || state.runId !== runId) return;
      const index = state.variants.findIndex((variant) => variant.ready?.variant_id === variantId);
      const variant = state.variants[index];
      if (!variant || variant.timing || variant.receivedMs === null) return;
      const entry: ClientTiming = {
        variant_index: index,
        received_ms: variant.receivedMs,
        loaded_ms: Math.max(variant.receivedMs, timing.loadedAt - state.submittedAt),
        displayed_ms: Math.max(variant.receivedMs, timing.loadedAt - state.submittedAt, timing.displayedAt - state.submittedAt),
        models: timing.models,
        failed_models: timing.failed_models,
        screen,
      };
      const variants = [...state.variants];
      variants[index] = { ...variant, timing: entry };
      set({ variants }); // Mark before posting: rerenders and route changes cannot post twice.
      void fetch(`/api/runs/${runId}/client-timing`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(entry), keepalive: true,
      }).catch(() => {});
    },
  };
});

const STEP_OF: Record<NodeName, number> = {
  interpret: 1,
  extract_room: 1,
  rag_scope_assets: 2,
  select_asset_intent: 2,
  layout_initial: 3,
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
