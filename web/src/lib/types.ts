// Shapes of the pipeline's POST /pipeline request and SSE events (pipeline/app/contracts.py).

export type RoomType = "living_room" | "bedroom" | "dining_room" | "studio";
export type Vec2 = [number, number];

export interface Opening {
  id: string;
  center: Vec2; // plan metres, on a wall
  width: number; // box size along plan X
  depth: number; // box size along plan Y
  height: number;
  sill_height?: number;
  wall?: "top" | "right" | "bottom" | "left";
}

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

export type NodeName =
  "interpret" | "extract_room" | "rag_scope_assets" | "select_asset_intent" | "layout_initial" | "layout_fix" | "render_scene";

export interface Product {
  asset_id: string;
  name: string | null;
  category: string | null;
  image_url: string | null;
  price: number | null;
}

/** `node_complete.data`, one shape per node. */
export interface NodeData {
  interpret: { style_hints: string[]; requested_categories: string[] };
  extract_room: {
    room_area: Vec2;
    wall_height: number;
    doors: number;
    windows: number;
    floor_area_sqm: number;
    usable_area_sqm: number;
    protected_paths: number;
    fit: string;
    fit_message: string;
  };
  rag_scope_assets: {
    slots: number;
    candidates: number;
    gaps: string[];
    preview: (Product & { slot: string; kind: string })[];
  };
  select_asset_intent: {
    turn: number;
    valid: boolean;
    errors: number;
    fit_step: string | null;
    total_cost: number;
    items: Product[];
  };
  layout_initial: { placed: number; findings: number; blocking: number };
  layout_fix: { placed?: number; findings: number; blocking: number; improved: boolean };
  render_scene: { valid: boolean; errors: number };
}

export interface ManifestAsset {
  instance_key: string;
  asset_id: string;
  name: string;
  category: string;
  image_url: string | null;
  glb_url: string;
  frontView: number | null;
  width: number;
  depth: number;
  height: number;
  placement_mode: string;
  is_decor_item: boolean;
}

export interface Placement {
  instance_key: string;
  category: string;
  position: [number, number, number]; // plan x, y, z_bottom
  rotation: [number, number, number]; // [0, 0, yaw]
}

export interface RenderManifest {
  room_area: Vec2;
  room_vertices: Vec2[];
  room_doors: Opening[];
  room_windows: Opening[];
  wall_height: number;
  layout: Record<string, Placement>;
  assets: Record<string, ManifestAsset>;
}

export interface SelectedAsset {
  instance_key: string;
  asset_id: string;
  name: string;
  category: string;
  image_url: string | null;
  price: number | null;
  is_decor_item: boolean;
}

export interface ReadyVariant {
  variant_index: number;
  variant_id: string;
  render_manifest: RenderManifest;
  selected_assets: SelectedAsset[];
  total_cost: number;
}

type NodeEvent<T extends string> = { type: T; node: NodeName; index: number; variant_index: number | null };

export type PipelineEvent =
  | { type: "start"; run_id: string; nodes: NodeName[] }
  | NodeEvent<"node_start">
  | (NodeEvent<"node_complete"> & { elapsed: number; data: Partial<NodeData[NodeName]> })
  | { type: "heartbeat"; node: NodeName | null; variant_index: number | null; elapsed: number }
  | { type: "variant_ready"; data: { variant: ReadyVariant } }
  | { type: "variant_failed"; variant_index: number; reason: string; message: string; errors: string[] }
  | { type: "complete"; data: { run_dir: string; variants: ReadyVariant[] } }
  | { type: "error"; message: string; code: string };
