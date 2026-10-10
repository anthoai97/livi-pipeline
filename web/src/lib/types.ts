import { z } from "zod";

// Shapes of POST /pipeline and its SSE events (pipeline/app/contracts.py).
export type RoomType = "living_room" | "bedroom" | "dining_room" | "studio";
const vec2 = z.tuple([z.number(), z.number()]);
const vec3 = z.tuple([z.number(), z.number(), z.number()]);
export type Vec2 = z.infer<typeof vec2>;

const opening = z.object({
  id: z.string(), center: vec2, width: z.number(), depth: z.number(), height: z.number(),
  sill_height: z.number().optional(), wall: z.enum(["top", "right", "bottom", "left"]).optional(),
});
export type Opening = z.infer<typeof opening>;

export interface PipelineRequest {
  user_intent: string;
  budget: number;
  room_type: RoomType;
  room_area: Vec2;
  room_vertices: Vec2[];
  wall_height: number;
  room_doors: Opening[];
  room_windows: Opening[];
}

const nodeName = z.enum([
  "interpret", "extract_room", "rag_scope_assets", "select_asset_intent", "layout_initial", "layout_fix", "render_scene",
]);
export type NodeName = z.infer<typeof nodeName>;
const count = z.number().int().nonnegative();
const variantIndex = count.max(2);
const product = z.object({
  asset_id: z.string(), name: z.string().nullable(), category: z.string().nullable(),
  image_url: z.string().nullable(), price: z.number().nullable(),
});
export type Product = z.infer<typeof product>;

const nodeData = {
  interpret: z.object({ style_hints: z.array(z.string()), requested_categories: z.array(z.string()) }),
  extract_room: z.object({
    room_area: vec2, wall_height: z.number(), doors: count, windows: count,
    floor_area_sqm: z.number(), usable_area_sqm: z.number(), protected_paths: count,
    fit: z.string(), fit_message: z.string(),
  }),
  rag_scope_assets: z.object({
    slots: count, candidates: count, gaps: z.array(z.string()),
    preview: z.array(product.extend({ slot: z.string(), kind: z.string() })),
  }),
  // Shared rank and per-variant selection use the same legacy node name.
  select_asset_intent: z.object({
    ranked_slots: count.optional(), shared_products: count.optional(),
    turn: count.optional(), valid: z.boolean().optional(), errors: count.optional(),
    fit_step: z.string().nullable().optional(), total_cost: z.number().optional(), items: z.array(product).optional(),
  }),
  layout_initial: z.object({ placed: count, findings: count, blocking: count, swaps: count, drops: count }),
  layout_fix: z.object({ placed: count, findings: count, blocking: count, improved: z.boolean() }),
  render_scene: z.object({ valid: z.boolean(), errors: count }),
};
export type NodeData = { [N in NodeName]: z.infer<(typeof nodeData)[N]> };

const manifestAsset = z.object({
  instance_key: z.string(), asset_id: z.string(), name: z.string(), category: z.string(),
  image_url: z.string().nullable(), glb_url: z.string(), frontView: z.number().nullable(),
  width: z.number(), depth: z.number(), height: z.number(), placement_mode: z.string(), is_decor_item: z.boolean(),
});
export type ManifestAsset = z.infer<typeof manifestAsset>;
const placement = z.object({
  instance_key: z.string(), category: z.string(),
  position: vec3, // plan x, y, z_bottom
  rotation: vec3, // [0, 0, yaw]
});
export type Placement = z.infer<typeof placement>;
const renderManifest = z.object({
  room_area: vec2, room_vertices: z.array(vec2).min(3), room_doors: z.array(opening), room_windows: z.array(opening),
  wall_height: z.number(), layout: z.record(z.string(), placement), assets: z.record(z.string(), manifestAsset),
});
export type RenderManifest = z.infer<typeof renderManifest>;
const selectedAsset = product.extend({
  instance_key: z.string(), name: z.string(), category: z.string(), is_decor_item: z.boolean(),
});
export type SelectedAsset = z.infer<typeof selectedAsset>;
const readyVariant = z.object({
  variant_index: variantIndex, variant_id: z.string(), render_manifest: renderManifest,
  selected_assets: z.array(selectedAsset), total_cost: z.number(),
  preview_url: z.string().regex(/^\/runs\/[0-9a-f]{32}\/previews\/[0-2]\.png$/).nullable(),
});
export type ReadyVariant = z.infer<typeof readyVariant>;

const nodeFields = { node: nodeName, index: count, variant_index: variantIndex.nullable() };
export const pipelineEvent = z.discriminatedUnion("type", [
  z.object({ type: z.literal("start"), run_id: z.string().regex(/^[0-9a-f]{32}$/), nodes: z.array(nodeName) }),
  z.object({ type: z.literal("node_start"), ...nodeFields }),
  z.object({
    type: z.literal("node_complete"), ...nodeFields, elapsed: z.number().nonnegative(),
    data: z.record(z.string(), z.unknown()),
  }).transform((event, ctx) => {
    const parsed = nodeData[event.node].safeParse(event.data);
    if (!parsed.success) {
      ctx.addIssue({ code: "custom", message: `Invalid ${event.node} display data` });
      return z.NEVER;
    }
    return { ...event, data: parsed.data };
  }),
  z.object({ type: z.literal("heartbeat"), node: nodeName.nullable(), variant_index: variantIndex.nullable(), elapsed: z.number() }),
  z.object({ type: z.literal("variant_ready"), data: z.object({ variant: readyVariant }) }),
  z.object({ type: z.literal("variant_failed"), variant_index: variantIndex, reason: z.string(), message: z.string(), errors: z.array(z.string()) }),
  z.object({ type: z.literal("complete"), data: z.object({ run_dir: z.string(), variants: z.array(readyVariant) }) }),
  z.object({ type: z.literal("error"), message: z.string(), code: z.string() }),
]);
export type PipelineEvent = z.infer<typeof pipelineEvent>;

export interface ClientTiming {
  variant_index: number;
  received_ms: number;
  loaded_ms: number;
  displayed_ms: number;
  models: number;
  failed_models: number;
  screen: "generate" | "chooser" | "studio";
}

export interface SceneTiming {
  loadedAt: number;
  displayedAt: number;
  models: number;
  failed_models: number;
}
