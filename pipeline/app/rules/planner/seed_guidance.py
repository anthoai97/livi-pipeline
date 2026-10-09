from collections.abc import Mapping
from typing import Any
from app.rules.room_policy import normalize_room_type, studio_viewing_assets

from app.rules.pipeline_shared import (
    is_floor_lamp_asset,
    is_table_lamp_asset,
    is_wall_aligned_asset,
    normalize_asset_label,
)

from ._util import clean_list as _clean_list
from .taxonomy import ROLE_CATEGORIES, normalize_category

# Placement buckets, not language taxonomy: these decide seed layout behavior.
_CENTER_SURFACE_BUCKET = {"coffee_table", "center_table", "ottoman"}
_SIDE_SURFACE_BUCKET = {"side_table", "end_table"}
_WALL_STORAGE_BUCKET = ROLE_CATEGORIES["storage"] | {"console_table"}
_ROLE_PRIORITY = (
    "sleeping_anchor",
    "bedside",
    "anchor",
    "media",
    "rug",
    "center_surface",
    "side_surface",
    "conversation_support",
    "dining_table",
    "dining_chair",
    "desk",
    "office_chair",
    "floor_lamp",
    "table_lamp",
    "lighting",
    "wall_storage",
    "generic_wall",
    "generic_floor",
)


def _unique(values: list[str | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text and text not in result:
            result.append(text)
    return result


def _asset_area(asset: Mapping[str, Any]) -> float:
    return float(asset.get("width", 0.0) or 0.0) * float(asset.get("depth", 0.0) or 0.0)


def _opposite_wall(wall: str | None) -> str | None:
    mapping = {
        "left": "right",
        "right": "left",
        "top": "bottom",
        "bottom": "top",
    }
    return mapping.get(str(wall or "").strip().lower())


def _adjacent_walls(wall: str | None) -> list[str]:
    wall_name = str(wall or "").strip().lower()
    if wall_name in {"left", "right"}:
        return ["top", "bottom"]
    if wall_name in {"top", "bottom"}:
        return ["left", "right"]
    return []


def _normalized_category(asset: Mapping[str, Any]) -> str:
    label = normalize_asset_label(str(asset.get("category", "") or ""))
    return normalize_category(label) or label


def _classify_asset_role(asset: Mapping[str, Any]) -> str:
    category = _normalized_category(asset)
    uid = str(asset.get("uid", "") or "")

    if category == "bed":
        return "sleeping_anchor"
    if category == "nightstand":
        return "bedside"
    if category in ROLE_CATEGORIES["anchor_seating"]:
        return "anchor"
    if category in ROLE_CATEGORIES["media"]:
        return "media"
    if category == "dining_table":
        return "dining_table"
    if category == "desk":
        return "desk"
    if category == "dining_chair":
        return "dining_chair"
    if category == "office_chair":
        return "office_chair"
    if category in ROLE_CATEGORIES["accent_seating"]:
        return "conversation_support"
    if category in _CENTER_SURFACE_BUCKET:
        return "center_surface"
    if category in _SIDE_SURFACE_BUCKET:
        return "side_surface"
    if category in ROLE_CATEGORIES["rug"]:
        return "rug"
    if is_floor_lamp_asset(category, uid):
        return "floor_lamp"
    if is_table_lamp_asset(category, uid):
        return "table_lamp"
    if category in ROLE_CATEGORIES["lighting"]:
        return "lighting"
    if category in _WALL_STORAGE_BUCKET:
        return "wall_storage"
    if is_wall_aligned_asset(category, uid):
        return "generic_wall"
    return "generic_floor"


def _sort_role_assets(assets: list[Mapping[str, Any]]) -> list[str]:
    return [
        str(asset.get("uid"))
        for asset in sorted(
            assets,
            key=lambda asset: (-_asset_area(asset), str(asset.get("uid", ""))),
        )
        if asset.get("uid")
    ]


def _sort_media_assets(assets: list[Mapping[str, Any]]) -> list[str]:
    def media_rank(asset: Mapping[str, Any]) -> int:
        category = _normalized_category(asset)
        if category in {"tv_stand", "media_unit"}:
            return 0
        if category in {"tv", "projector"}:
            return 1
        return 2

    return [
        str(asset.get("uid"))
        for asset in sorted(
            assets,
            key=lambda asset: (
                media_rank(asset),
                -_asset_area(asset),
                str(asset.get("uid", "")),
            ),
        )
        if asset.get("uid")
    ]


def _room_mode(
    *,
    requested_categories: set[str],
    functional_hints: set[str],
    role_buckets: dict[str, list[str]],
) -> str:
    media_requested = bool(requested_categories & ROLE_CATEGORIES["media"]) or "tv_focused" in functional_hints or bool(role_buckets["media"])
    if media_requested:
        return "tv_plus_conversation"
    if "reading" in functional_hints and not role_buckets["media"]:
        return "reading_first"
    if "conversation" in functional_hints or role_buckets["conversation_support"]:
        return "conversation_first"
    return "open"


def build_seed_guidance(
    *,
    room_facts: Mapping[str, Any],
    intent_packet: Mapping[str, Any],
    feasibility_digest: Mapping[str, Any],
    selected_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assets = studio_viewing_assets(selected_assets or [], intent_packet)
    role_assets: dict[str, list[Mapping[str, Any]]] = {role: [] for role in _ROLE_PRIORITY}
    roles_by_uid: dict[str, str] = {}
    for asset in assets:
        uid = str(asset.get("uid", "") or "")
        if not uid:
            continue
        role = _classify_asset_role(asset)
        role_assets[role].append(asset)
        roles_by_uid[uid] = role

    candidate_anchor_walls = [
        str(item.get("wall") or "").strip().lower()
        for item in (room_facts.get("candidate_anchor_walls") or [])
        if str(item.get("wall") or "").strip().lower()
    ]
    digest_anchor_wall = str((feasibility_digest.get("anchor_wall") or {}).get("wall") or "").strip().lower()
    anchor_wall = digest_anchor_wall or (candidate_anchor_walls[0] if candidate_anchor_walls else None)
    focal_wall = _opposite_wall(anchor_wall) if role_assets["media"] else None
    support_walls = _adjacent_walls(anchor_wall)
    requested_categories = set(_clean_list(intent_packet.get("requested_categories")))
    functional_hints = set(_clean_list(intent_packet.get("functional_hints")))

    ordered_role_uids = {
        role: (
            _sort_media_assets(role_assets[role])
            if role == "media"
            else _sort_role_assets(role_assets[role])
        )
        for role in _ROLE_PRIORITY
    }
    room_type = normalize_room_type(intent_packet.get("room_type"))
    bedroom = room_type == "bedroom"
    dining = room_type == "dining_room"
    studio = room_type == "studio"
    tv_viewing_target = next((asset["viewing_target"] for asset in assets if asset.get("viewing_target")), "sitting")
    sitting_wall = (anchor_wall if studio and tv_viewing_target == "shared" else
                    next((wall for wall in candidate_anchor_walls if wall != anchor_wall), _opposite_wall(anchor_wall)))
    group_anchors = {
        group: next(iter(ordered_role_uids[role]), None)
        for group, role in (("sleeping", "sleeping_anchor"), ("sitting", "anchor"), ("dining", "dining_table"))
    } if studio else {}
    room_mode = "studio" if studio else "sleeping" if bedroom else "dining" if dining else _room_mode(
        requested_categories=requested_categories,
        functional_hints=functional_hints,
        role_buckets=ordered_role_uids,
    )

    return {
        "version": 1,
        "room_mode": room_mode,
        "anchor_wall": anchor_wall,
        "focal_wall": focal_wall,
        "support_walls": support_walls,
        "candidate_anchor_walls": candidate_anchor_walls,
        "primary_anchor_uid": next(iter(ordered_role_uids["sleeping_anchor" if bedroom or studio else "dining_table" if dining else "anchor"]), None),
        **({"group_anchors": group_anchors, "sitting_wall": sitting_wall,
            "tv_viewing_target": tv_viewing_target} if studio else {}),
        "roles_by_uid": roles_by_uid,
        "role_uids": ordered_role_uids,
    }


def build_seed_layout_plan(
    selected_assets: list[dict[str, Any]] | None,
    guidance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    assets = selected_assets or []
    uid_set = {str(asset.get("uid", "") or "") for asset in assets if asset.get("uid")}
    guide = dict(guidance or {})
    role_uids = guide.get("role_uids") or {}
    anchor_wall = guide.get("anchor_wall")
    focal_wall = guide.get("focal_wall")
    support_walls = _clean_list(guide.get("support_walls"))
    candidate_anchor_walls = _clean_list(guide.get("candidate_anchor_walls"))
    fallback_walls = _unique([anchor_wall, focal_wall, *support_walls, *candidate_anchor_walls, "top", "bottom", "left", "right"])

    ordered_uids: list[str] = []
    for role in _ROLE_PRIORITY:
        for uid in _clean_list(role_uids.get(role)):
            if uid in uid_set and uid not in ordered_uids:
                ordered_uids.append(uid)
    for asset in assets:
        uid = str(asset.get("uid", "") or "")
        if uid and uid not in ordered_uids:
            ordered_uids.append(uid)

    preferred_walls_by_uid: dict[str, list[str]] = {}
    placement_mode_by_uid: dict[str, str] = {}
    roles_by_uid = {
        str(uid): str(role)
        for uid, role in (guide.get("roles_by_uid") or {}).items()
        if str(uid)
    }
    studio = guide.get("room_mode") == "studio"
    group_anchors = guide.get("group_anchors") or {}
    assigned_groups = {str(asset.get("uid")): asset.get("functional_group") for asset in assets}
    anchor_by_uid: dict[str, str] = {}
    if studio:
        # Reserve all three anchors before their optional accessories.
        anchors = [uid for uid in group_anchors.values() if uid in uid_set]
        ordered_uids = anchors + [uid for uid in ordered_uids if uid not in anchors]

    def set_plan(uid: str, mode: str, walls: list[str | None]) -> None:
        placement_mode_by_uid[uid] = mode
        preferred_walls_by_uid[uid] = _unique([*walls, *fallback_walls])

    for uid in ordered_uids:
        role = roles_by_uid.get(uid, "generic_floor")
        if studio:
            group = "sleeping" if role in {"sleeping_anchor", "bedside"} else "dining" if role in {"dining_table", "dining_chair"} else "sitting"
            if assigned_groups.get(uid) in group_anchors:
                group = assigned_groups[uid]
            if role == "media" and guide.get("tv_viewing_target") == "bed":
                group = "sleeping"
            if group_anchors.get(group):
                anchor_by_uid[uid] = group_anchors[group]
        if role in {"anchor", "sleeping_anchor"}:
            set_plan(uid, "anchor_wall", [guide.get("sitting_wall") if studio and role == "anchor" else anchor_wall])
        elif role == "bedside" and studio:
            placement_mode_by_uid[uid] = "beside_bed_head"
        elif role == "media":
            media_wall = _opposite_wall(guide.get("sitting_wall")) if studio and guide.get("tv_viewing_target") != "bed" else focal_wall
            set_plan(uid, "focal_wall", [media_wall, *_adjacent_walls(media_wall), anchor_wall])
        elif role == "wall_storage":
            set_plan(uid, "support_wall", [*support_walls, focal_wall, *_adjacent_walls(focal_wall)])
        elif role == "lighting":
            set_plan(uid, "support_wall", [*support_walls, anchor_wall, focal_wall])
        elif role == "generic_wall":
            set_plan(uid, "wall", [anchor_wall, focal_wall, *support_walls])
        elif role == "center_surface":
            placement_mode_by_uid[uid] = "front_of_anchor"
        elif role == "side_surface":
            placement_mode_by_uid[uid] = "beside_anchor"
        elif role == "conversation_support":
            placement_mode_by_uid[uid] = "around_anchor"
        elif role == "dining_table":
            placement_mode_by_uid[uid] = "dining_table_anchor"
        elif role == "dining_chair":
            placement_mode_by_uid[uid] = "around_dining_table"
        elif role == "desk":
            set_plan(uid, "desk_wall", [*support_walls, anchor_wall, focal_wall])
        elif role == "office_chair":
            placement_mode_by_uid[uid] = "front_of_desk"
        elif role == "rug":
            placement_mode_by_uid[uid] = "under_anchor"
        elif role == "floor_lamp":
            placement_mode_by_uid[uid] = "beside_seat"
        elif role == "table_lamp":
            placement_mode_by_uid[uid] = "on_support_surface"
        else:
            placement_mode_by_uid[uid] = "free"

    return {
        "version": 1,
        "ordered_uids": ordered_uids,
        "preferred_walls_by_uid": preferred_walls_by_uid,
        "placement_mode_by_uid": placement_mode_by_uid,
        "primary_anchor_uid": guide.get("primary_anchor_uid"),
        "room_mode": guide.get("room_mode"),
        **({"anchor_by_uid": anchor_by_uid, "group_anchors": group_anchors} if studio else {}),
    }
