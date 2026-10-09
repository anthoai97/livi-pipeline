"""Request model, SSE event payloads, render manifest, and selected-asset entries.

Shapes follow today's pipeline (livinit_pipeline src/api/models.py, src/api/sse.py,
src/api/execution/pipeline_stream.py, src/nodes/render_scene.py) for the fields
the web app reads. Frames are flat `{"type": ..., ...}` except `variant_ready` and
`complete`, which nest under `data`. Saving fields (`create_payload`,
`turn_payload`) wait for phase 5. The stream never sends `fit_confirmation_required`.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

RoomType = Literal["living_room", "bedroom", "dining_room", "studio"]
# Event node names. The web app's progress steps key on the legacy names
# (web-pipeline lib/wizard/nodeStepMap.ts); the run record keeps the stage names.
NODE_NAMES = {
    "interpret": "interpret",
    "room": "extract_room",
    "retrieve": "rag_scope_assets",
    "rank": "select_asset_intent",
    "select": "select_asset_intent",
    "place": "layout_initial",
    "repair": "layout_fix",
    "correct": "layout_fix",
    "validate": "render_scene",
}
NODES = list(dict.fromkeys(NODE_NAMES.values()))


class PipelineRequest(BaseModel):
    """POST /pipeline body for a geometry room. Unknown fields get HTTP 422."""

    model_config = ConfigDict(extra="forbid")

    user_intent: str = Field(default="Modern minimalist living room", min_length=1)
    budget: float = Field(default=5000.0, ge=0)
    room_type: RoomType = "living_room"
    room_area: tuple[float, float]
    room_vertices: list[tuple[float, float]] = Field(min_length=3)
    wall_height: float = Field(gt=0, allow_inf_nan=False)
    room_doors: list[dict[str, Any]] = []
    room_windows: list[dict[str, Any]] = []
    wall_finishes: dict[str, Any] | None = None


class Instance(TypedDict):
    """One placed instance, the input to the manifest and selected-asset builders."""

    instance_key: str  # "<category>_<n>", such as "sofa_1"
    category: str  # normalized category
    position: list[float]  # [x, y, z_bottom] in metres, Z-up plan frame, origin at the room's min corner
    rotation: list[float]  # [0, 0, yaw] in radians
    placement_mode: str  # floor, tabletop, wall_mounted, or ceiling_mounted
    asset: dict[str, Any]  # prepared record from pipeline.pipeline_assets_v2


def start_event(run_id: str) -> dict:
    return {"type": "start", "run_id": run_id, "nodes": NODES, "route": "NEW_DESIGN", "mode": "full_pipeline"}


def node_start(stage: str, variant_index: int | None) -> dict:
    node = NODE_NAMES[stage]
    return {"type": "node_start", "node": node, "index": NODES.index(node), "variant_index": variant_index}


def node_complete(stage: str, variant_index: int | None, elapsed: float) -> dict:
    return {**node_start(stage, variant_index), "type": "node_complete", "elapsed": elapsed}


def heartbeat_event(node: str | None, variant_index: int | None, elapsed: float) -> dict:
    """`node` and `variant_index` repeat the latest node_start event."""
    return {"type": "heartbeat", "node": node, "variant_index": variant_index, "elapsed": elapsed}


def ready_variant(
    run_id: str,
    variant_index: int,
    *,
    render_manifest: dict[str, Any],
    selected_assets: list[dict[str, Any]],
    total_cost: float,
    selection_validation: dict[str, Any],
) -> dict:
    return {
        "variant_index": variant_index,
        "variant_id": f"{run_id}_v{variant_index}" if variant_index else run_id,
        "committed": False,
        "render_manifest": render_manifest,
        "selected_assets": selected_assets,
        "total_cost": total_cost,
        "selection_validation": selection_validation,
        "asset_selection_failed": False,
    }


def variant_ready(variant: dict) -> dict:
    return {"type": "variant_ready", "data": {"variant": variant}}


def variant_failed(variant_index: int, reason: str, message: str, errors: list[str]) -> dict:
    return {"type": "variant_failed", "variant_index": variant_index, "reason": reason, "message": message, "errors": errors}


def complete_event(run_id: str, variants: list[dict]) -> dict:
    return {"type": "complete", "data": {"run_dir": run_id, "response": "", "variants": variants}}


def error_event(message: str, code: str, exception_type: str | None = None) -> dict:
    """Keep `message` free of "503", "overloaded", and "UNAVAILABLE": the web app reads those as progress."""
    return {"type": "error", "message": message, "code": code, "phase": "generating", "exception_type": exception_type}


def render_manifest(request: PipelineRequest, instances: list[Instance]) -> dict:
    """Today's manifest shape, with `layout` and `assets` keyed by instance key."""
    layout = {}
    assets = {}
    for instance in instances:
        key = instance["instance_key"]
        asset = instance["asset"]
        layout[key] = {
            "uid": str(asset["asset_id"]),
            "instance_key": key,
            "category": instance["category"],
            "position": instance["position"],
            "rotation": instance["rotation"],
        }
        assets[key] = {
            "instance_key": key,
            "asset_id": str(asset["asset_id"]),
            "uid": str(asset["asset_id"]),
            "name": asset["title"],
            "category": instance["category"],
            "image_url": asset["image_url"],
            "glb_url": asset["model_url"] or "",
            "center": asset.get("center"),
            "frontView": asset.get("front_view"),
            "topdown_url": asset.get("topdown_url"),
            "mount_type": asset.get("mount_type"),
            "features": asset.get("features") or [],
            "width": asset["width_m"],
            "depth": asset["depth_m"],
            "height": asset["height_m"],
            "placement_mode": instance["placement_mode"],
            "is_decor_item": asset["source_table"] == "pipeline.decor_items",
            "is_placeholder": False,
        }
    return {
        "room_area": list(request.room_area),
        "room_vertices": [list(vertex) for vertex in request.room_vertices],
        "room_doors": request.room_doors,
        "room_windows": request.room_windows,
        "wall_height": request.wall_height,
        "layout": layout,
        "assets": assets,
    }


def selected_asset(instance: Instance) -> dict:
    """One `selected_assets` entry per instance, with the keys the web app reads."""
    asset = instance["asset"]
    price = asset["price"]
    return {
        "uid": str(asset["asset_id"]),
        "instance_key": instance["instance_key"],
        "asset_id": str(asset["asset_id"]),
        "name": asset["title"],
        "category": instance["category"],
        "image_url": asset["image_url"],
        "model_url": asset["model_url"],
        "center": asset.get("center"),
        "frontView": asset.get("front_view"),
        "topdown_url": asset.get("topdown_url"),
        "mount_type": asset.get("mount_type"),
        "features": asset.get("features") or [],
        "width": asset["width_m"],
        "depth": asset["depth_m"],
        "height": asset["height_m"],
        "price": float(price) if price is not None else None,
        "is_decor_item": asset["source_table"] == "pipeline.decor_items",
        "is_placeholder": False,
    }
