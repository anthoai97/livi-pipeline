"""Selection validation and retry-context construction."""

import copy
import json
from collections import Counter
from typing import Any
from app.rules.categories import normalize_asset_label
from app.rules.layout.comfort import sitting_rug_selection_errors
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.intent_packet import count_constraints_from_intent_packet
from app.rules.planner.selection_count_guidance import count_guidance_feedback
from app.rules.planner.taxonomy import COUNT_GUIDANCE_ROLE_CATEGORIES, normalize_category
from app.rules.room_policy import (
    asset_is_eligible_for_room,
    dining_chair_count,
    excluded_room_categories,
    normalize_room_type,
    studio_viewing_assets,
)

from app.rules.selection.catalog import (
    _asset_matches_brand_preferences,
    _asset_price_float,
    _asset_satisfies_price_constraints,
    _asset_width_depth,
    _price_constraint_category_matches,
    _recommendation_price_constraints,
    catalog_asset,
)
from app.rules.selection.constants import (
    BLOCKING_REQUIRED_ROLES,
    BUDGET_EXCLUDED_CATEGORIES,
    BUDGET_FLEX_PCT,
    FIT_ACCENT_SEATING,
    FIT_ANCHOR_SEATING,
    PERIMETER_STORAGE_CATEGORIES,
    RECOMMENDED_CATEGORIES,
    REQUIRED_CATEGORIES,
)
from app.rules.selection.fit import (
    FIT_STEP_DECISIONS,
    _fit_target_validation_feedback,
    _fit_warning_counts,
)
from app.rules.selection.preflight import (
    _annotate_tv_placement,
    _canonical_living_arrangement_footprints,
    _fits_rect,
    _is_living_accent_seating,
    _layout_preflight_for_assets,
    _requested_role_validation_errors,
)


_SPARSE_ROOM_PHRASES = (
    "as few pieces",
    "bare room",
    "minimal furniture",
    "no extra furniture",
    "only essentials",
    "very sparse",
)

# Stored width/depth of these follow the 3D model axes, so size limits accept either orientation.
_ORIENTATION_FREE_CATEGORIES = {"rug", "door_mat", "doormat"}

_ITEM_FIELDS = ("reason", "functional_group", "functional_role", "viewing_target")


def is_decor_plant(asset: dict[str, Any]) -> bool:
    return (
        normalize_category(asset.get("category")) == "planter"
        and asset.get("is_decor_item") is True
    )


def requires_spacious_perimeter_storage(
    *,
    room_type: str,
    intent_packet: dict[str, Any],
    feasibility_digest: dict[str, Any],
    fit_step: str | None,
) -> bool:
    if room_type != "living_room":
        return False
    if fit_step:
        return False
    if feasibility_digest.get("room_scale") != "spacious":
        return False

    excluded = {
        normalize_category(category)
        for category in [
            *(intent_packet.get("excluded_categories") or []),
            *(intent_packet.get("avoid_categories") or []),
        ]
    }
    if "storage_unit" in excluded or PERIMETER_STORAGE_CATEGORIES <= excluded:
        return False

    intent_text = " ".join(
        str(value or "")
        for value in (
            intent_packet.get("normalized_prompt"),
            intent_packet.get("room_goal"),
        )
    ).lower()
    return not any(phrase in intent_text for phrase in _SPARSE_ROOM_PHRASES)


def build_instances(items: list[dict[str, Any]], intent: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand selection items into the instance records placement and validation use.

    items: [{"asset": <pipeline_assets_v2 record>, "quantity": int, optional
    reason/functional_group/functional_role/viewing_target}]. Each instance is
    the record plus catalog_asset fields, keyed uid = instance_key =
    "<normalized category>_<n>" (n counts per category from 1). A TV that fits a
    selected media support becomes tabletop with paired_support_uid; studio
    media gets its viewing_target.
    """
    counts: Counter[str] = Counter()
    instances: list[dict[str, Any]] = []
    for item in items:
        asset = catalog_asset(item["asset"])
        extras = {key: item[key] for key in _ITEM_FIELDS if item.get(key)}
        category = normalize_category(asset.get("category")) or "asset"
        for _ in range(int(item.get("quantity") or 0)):
            counts[category] += 1
            key = f"{category}_{counts[category]}"
            instances.append({**asset, **extras, "uid": key, "instance_key": key})
    return studio_viewing_assets(_annotate_tv_placement(instances), intent)


def room_requirements(
    *,
    room_type: str,
    intent: dict[str, Any],
    digest: dict[str, Any],
) -> dict[str, Any]:
    """Items a fresh design must include, and the optional furniture roles, for a slot planner.

    required: [{"role", "categories", "count", "decor_only"}], the items
    validate_selection blocks on: anchor seating, a surface, and a rug in a
    living room; one bed and a rug in a bedroom; one dining table and
    dining_chair_count chairs in a dining room; bed, sofa/loveseat, dining table
    and chairs in a studio; perimeter storage in a spacious living room; and a
    design-only plant outside studios. A rug or plant the intent excludes is
    not required.
    optional_roles: {role: categories} from the count-guidance roles, without
    room exclusions (no bed outside bedrooms and studios), intent exclusions,
    zero-capped categories, and categories already required.
    """
    room_type = normalize_room_type(room_type)
    excluded = {normalize_category(category) for category in intent.get("excluded_categories") or []}
    required: list[dict[str, Any]] = []

    def need(role: str, categories: set[str], count: int = 1, *, decor_only: bool = False) -> None:
        required.append({"role": role, "categories": sorted(categories), "count": count, "decor_only": decor_only})

    if room_type == "living_room":
        need("anchor_seating", REQUIRED_CATEGORIES["anchor_seating"])
        need("surface", REQUIRED_CATEGORIES["surface"])
    if room_type in {"bedroom", "studio"}:
        need("sleeping_anchor", {"bed"})
    if room_type == "studio":
        need("anchor_seating", {"sofa", "loveseat", "sectional", "sleeper_sofa"})
    if room_type in {"dining_room", "studio"}:
        need("dining_table", {"dining_table"})
        need("dining_seating", {"dining_chair"}, dining_chair_count({**intent, "room_type": room_type}))
    if room_type in {"living_room", "bedroom"} and "rug" not in excluded:
        need("rug", REQUIRED_CATEGORIES["rug"])
    if room_type == "living_room" and requires_spacious_perimeter_storage(
        room_type=room_type, intent_packet=intent, feasibility_digest=digest, fit_step=None,
    ):
        need("storage", PERIMETER_STORAGE_CATEGORIES)
    if room_type != "studio" and "planter" not in excluded:
        need("decor_plant", {"planter"}, decor_only=True)

    taken = {category for item in required for category in item["categories"]}
    blocked = excluded_room_categories(room_type) | excluded | {
        category for category, cap in (digest.get("max_counts_by_category") or {}).items() if int(cap) <= 0
    }
    optional_roles = {
        role: sorted(categories - blocked - taken)
        for role, categories in COUNT_GUIDANCE_ROLE_CATEGORIES.items()
        if categories - blocked - taken
    }
    return {"required": required, "optional_roles": optional_roles}


def _size_limits_met(asset: dict[str, Any], item: dict[str, Any]) -> bool:
    def within(axis: str, value: float) -> bool:
        low, high = item.get(f"min_{axis}_m"), item.get(f"max_{axis}_m")
        return (low is None or value >= low - 1e-9) and (high is None or value <= high + 1e-9)

    width, depth = _asset_width_depth(asset)
    plan_fits = within("width", width) and within("depth", depth)
    if normalize_category(asset.get("category")) in _ORIENTATION_FREE_CATEGORIES or (
        normalize_asset_label(asset.get("category", "")) in _ORIENTATION_FREE_CATEGORIES
    ):
        plan_fits = plan_fits or (within("width", depth) and within("depth", width))
    return plan_fits and within("height", float(asset.get("height") or 0.0))


def _attribute_errors(selected_assets: list[dict[str, Any]], intent_packet: dict[str, Any]) -> list[str]:
    """Check prepared fields against the intent's strict size, attribute, brand, and price limits."""
    errors: list[str] = []
    for item in intent_packet.get("requested_items") or []:
        targets = {item["canonical_category"], *(item.get("acceptable_substitutes") or [])}
        for asset in selected_assets:
            if normalize_category(asset.get("category")) not in targets:
                continue
            problems = []
            if not _size_limits_met(asset, item):
                limits = {key: item[key] for key in item if key.endswith("_m")}
                width, depth = _asset_width_depth(asset)
                problems.append(
                    f"size {width:.2f}m x {depth:.2f}m x {float(asset.get('height') or 0.0):.2f}m is outside {limits}"
                )
            for field in ("colors", "styles", "materials"):
                missing = [value for value in item.get(field) or [] if value not in (asset.get(field) or [])]
                if missing:
                    problems.append(f"{field} lacks {missing}")
            if problems:
                errors.append(
                    f"ATTRIBUTE LIMIT: {asset.get('uid')} does not meet \"{item['label']}\": {'; '.join(problems)}."
                )
    brand_constraints = [
        constraint
        for constraint in intent_packet.get("asset_attribute_constraints") or []
        if constraint.get("attribute_type") == "brand" and constraint.get("required")
    ]
    for asset in selected_assets:
        category = normalize_category(asset.get("category"))
        if not asset.get("is_purchasable") or category in BUDGET_EXCLUDED_CATEGORIES:
            continue
        brands = [
            constraint["value"]
            for constraint in brand_constraints
            if not constraint.get("category")
            or _price_constraint_category_matches(constraint["category"], category)
        ]
        if brands and not _asset_matches_brand_preferences(asset, brands):
            errors.append(f"ATTRIBUTE LIMIT: {asset.get('uid')} brand {asset.get('brand')!r} is not one of {brands}.")
        price_constraints = _recommendation_price_constraints(intent_packet, category)
        if price_constraints and not _asset_satisfies_price_constraints(asset, price_constraints):
            errors.append(
                f"ATTRIBUTE LIMIT: {asset.get('uid')} price {_asset_price_float(asset)} is outside {price_constraints}."
            )
    return errors


def validate_selection(
    *,
    items: list[dict[str, Any]],
    intent: dict[str, Any],
    room: dict[str, Any],
    budget: float,
    candidates: list[dict[str, Any]],
    gaps: list[str] | tuple[str, ...] = (),
    fit_step: str | None = None,
    fit_satisfaction: Any = None,
    constraint_audit: Any = None,
) -> dict[str, Any]:
    """Validate a fresh-design selection with the ported legacy rules.

    items: [{"asset": <pipeline_assets_v2 record>, "quantity": int, optional
    functional_group (studio: sleeping, sitting, dining), functional_role,
    viewing_target, reason}]. intent: cleaned intent. room: build_room_context
    output. candidates: the retrieved pool of pipeline_assets_v2 records; an
    item outside it fails as unknown. gaps: requested categories with no
    eligible product, which are then not required. fit_step: None, "compact",
    or "capped" (see selection.fit). fit_satisfaction and constraint_audit are
    the selection model's optional self-reports, shaped like the legacy
    validate_selection tool arguments, with asset_id as the uid.

    Checks budget (110% allowance), footprint and density, category caps and
    counts, required items, fit targets, layout preflight fit, and strict
    attributes. Returns valid, status, errors, warnings, fit_failed (an
    over-crowded footprint or a layout preflight failure), metrics,
    retry_context (when invalid), feedback (JSON text for the model, with
    asset_id as the uid), and instances (build_instances output).
    """
    room_type = normalize_room_type(room["room_type"])
    feasibility_digest = room["digest"]
    furniture_area = float(room["furniture_area_sqm"])
    decision = FIT_STEP_DECISIONS.get(str(fit_step or ""))
    waived = {normalize_category(category) for category in gaps}
    # Gap items are not required: drop them from the requested items and counts.
    intent_packet = {
        **intent,
        "requested_items": [
            item for item in intent.get("requested_items") or []
            if normalize_category(item.get("canonical_category")) not in waived
        ],
        "requested_categories": [
            category for category in intent.get("requested_categories") or []
            if normalize_category(category) not in waived
        ],
    }
    assets_by_uid = {str(record["asset_id"]): catalog_asset(record) for record in candidates}
    canonical_uids: list[str] = []
    item_fields: list[dict[str, Any]] = []
    for item in items:
        extras = {key: item[key] for key in _ITEM_FIELDS if item.get(key)}
        for _ in range(int(item.get("quantity") or 0)):
            canonical_uids.append(str(item["asset"]["asset_id"]))
            item_fields.append(extras)
    instances = build_instances(items, intent)

    dining = room_type == "dining_room"
    studio = room_type == "studio"
    excluded_categories = {
        normalize_category(category)
        for category in (intent_packet.get("excluded_categories") or [])
    }
    total_cost = 0
    total_footprint = 0
    not_found = []
    accepted_fit_decision = decision in {
        "use_recommendation",
        "continue_anyway",
    }

    for uid in canonical_uids:
        asset = assets_by_uid.get(uid)
        if asset:
            cat = normalize_category(asset.get("category"))

            # TV is not sold through the pipeline, so it never counts
            # against the budget cap.
            if cat not in BUDGET_EXCLUDED_CATEGORIES:
                total_cost += asset.get("price", 0)

            if cat != "rug":
                total_footprint += (
                    asset.get("width", 0) * asset.get("depth", 0)
                ) * 2
        else:
            not_found.append(uid)

    max_allowed_footprint = furniture_area
    flex_budget = budget * (1 + BUDGET_FLEX_PCT)
    is_over_budget = total_cost > flex_budget
    is_over_crowded = total_footprint > max_allowed_footprint

    errors = []
    warnings = []
    if is_over_budget:
        budget_message = (
            f"Selection costs ${total_cost:.2f} "
            f"(Flex limit: ${flex_budget:.2f})."
        )
        errors.append(f"OVER BUDGET: {budget_message}")
    if is_over_crowded:
        errors.append(
            f"OVER CROWDED: Footprint is {total_footprint:.2f}m² (Limit: {max_allowed_footprint:.2f}m²)."
        )
    if not_found:
        errors.append(
            "UNKNOWN ASSET UID: "
            + ", ".join(str(uid) for uid in not_found[:5])
            + ("..." if len(not_found) > 5 else "")
        )
    is_valid = not errors

    # Density floor: a fresh design must furnish the room.
    density_budget = feasibility_digest.get("density_budget") or {}
    footprint_floor = float(density_budget.get("sparse_below_load_sqm") or 0.0)
    comfortable_load = float(density_budget.get("comfortable_load_sqm") or 0.0)
    if (
        is_valid
        and not accepted_fit_decision
        and not dining and not studio
        and footprint_floor > 0
        and total_footprint < footprint_floor
    ):
        errors.append(
            f"UNDER-FURNISHED: Footprint is {total_footprint:.2f}m² of "
            f"{furniture_area:.2f}m² available (minimum {footprint_floor:.2f}m²). "
            f"Add more coherent pieces across the room's roles — the comfortable "
            f"load for this room is {comfortable_load:.2f}m²."
        )
        is_valid = False

    # Room-specific requirements never mutate the candidate membership.
    selected_cats = {
        normalize_category(assets_by_uid[uid].get("category", ""))
        for uid in canonical_uids
        if uid in assets_by_uid
    }
    bedroom = room_type in {"bedroom", "studio"}
    if bedroom:
        requested_categories = set(intent_packet.get("requested_categories") or [])
        work_pair = {"desk", "office_chair"}
        if (
            work_pair <= requested_categories
            and len(work_pair & selected_cats) == 1
        ):
            errors.append(
                "BEDROOM WORK SET: Select the requested desk and office_chair together, "
                "or omit both when optional and space is insufficient. Do not leave half a workstation."
            )
            is_valid = False
        unsupported = [
            uid for uid in canonical_uids
            if uid in assets_by_uid and not asset_is_eligible_for_room(assets_by_uid[uid], room_type)
        ]
        if unsupported:
            errors.append(
                "UNSUPPORTED SLEEP ASSET: Select a standalone complete adult bed, not a daybed, bunk, component or bundled bedside set: "
                + ", ".join(sorted(unsupported)) + "."
            )
            is_valid = False
        bed_count = sum(
            normalize_category(assets_by_uid[uid].get("category")) == "bed"
            for uid in canonical_uids if uid in assets_by_uid
        )
        if bed_count > 1 or bed_count != 1:
            errors.append(
                "BED COUNT: A sleeping room requires exactly one complete adult bed (category=bed)."
            )
            is_valid = False
    if studio and not selected_cats & {"sofa", "loveseat", "sectional", "sleeper_sofa"}:
        errors.append("STUDIO SITTING GROUP: Include separate sofa/loveseat seating; a bed or dining chair does not substitute.")
        is_valid = False
    if dining or studio:
        dining_assets = [assets_by_uid[uid] for uid in canonical_uids if uid in assets_by_uid]
        table_count = sum(normalize_category(asset.get("category")) == "dining_table" for asset in dining_assets)
        chair_count = sum(normalize_category(asset.get("category")) == "dining_chair" for asset in dining_assets)
        target_count = dining_chair_count(intent_packet)
        seat_constraints = count_constraints_from_intent_packet(intent_packet)
        seat_request = seat_constraints.get("dining_chair") or seat_constraints.get("chair") or {}
        mandatory_count = not seat_request.get("optional") and (
            seat_request.get("exact") or seat_request.get("source") == "counted"
        )
        flexible_seating = not mandatory_count and (
            intent_packet.get("fit_flexibility") == "space_first"
            or (
                bool(seat_request.get("optional"))
                and (seat_request.get("exact") or seat_request.get("source") == "counted")
            )
        )
        minimum_count = min(2, target_count) if flexible_seating else target_count
        if table_count != 1 or not minimum_count <= chair_count <= target_count:
            seat_range = str(target_count) if minimum_count == target_count else f"{minimum_count}–{target_count}"
            errors.append(
                f"DINING GROUP: Select exactly one standalone dining_table and {seat_range} dining_chairs; "
                f"found {table_count} tables and {chair_count} chairs. Preserve the requested seating count."
            )
            is_valid = False
        for asset in dining_assets:
            if not asset_is_eligible_for_room(asset, room_type):
                errors.append("UNSUPPORTED SLEEP ASSET: A dining room cannot include a bed or sleep components.")
                is_valid = False
            if normalize_category(asset.get("category")) == "dining_chair":
                width, depth = _asset_width_depth(asset)
                if not (0.2 <= width <= 1.5 and 0.2 <= depth <= 1.5):
                    errors.append(
                        f"DINING CHAIR DIMENSIONS: {asset.get('uid')} has an unusable single-chair footprint "
                        f"({width:.2f}m x {depth:.2f}m); select a correctly scaled dining chair."
                    )
                    is_valid = False
    decor_plant_count = sum(
        is_decor_plant(assets_by_uid[uid])
        for uid in canonical_uids
        if uid in assets_by_uid
    )
    if not studio and "planter" not in excluded_categories and decor_plant_count < 1:
        errors.append(
            "MISSING REQUIRED: Include at least one non-shoppable decor plant "
            "(category=planter, is_decor_item=true), including after a fit decision. "
            "A retail planter or another decor category does not satisfy this requirement."
        )
        is_valid = False
    # Fresh designs require seating and a surface. Lighting stays advisory;
    # rugs use a separate fresh-design rule so accepted fit decisions keep one.
    enforce_required_categories = (
        not is_over_crowded
        and not accepted_fit_decision
    )
    required_categories = {
        label: cats for label, cats in REQUIRED_CATEGORIES.items()
        if not dining and not studio and (not bedroom or label not in {"anchor_seating", "surface"})
    }
    for label, cats in required_categories.items():
        if not selected_cats & cats:
            if label == "rug" and "rug" in excluded_categories:
                continue
            message = (
                f"MISSING REQUIRED: No {label.replace('_', ' ')} found. "
                f"Add one of: {', '.join(sorted(cats))}."
            )
            blocks_selection = (
                enforce_required_categories and label in BLOCKING_REQUIRED_ROLES
            ) or label == "rug"
            if blocks_selection:
                errors.append(message)
                is_valid = False
            else:
                warnings.append(message)
    for label, cats in RECOMMENDED_CATEGORIES.items():
        if bedroom or dining:
            continue
        if not selected_cats & cats:
            warnings.append(
                f"MISSING RECOMMENDED: Consider adding {label.replace('_', ' ')} "
                f"({', '.join(sorted(cats))})."
            )
    if requires_spacious_perimeter_storage(
        room_type=room_type,
        intent_packet=intent_packet,
        feasibility_digest=feasibility_digest,
        fit_step=fit_step,
    ) and not selected_cats & PERIMETER_STORAGE_CATEGORIES:
        errors.append(
            "MISSING REQUIRED: Spacious rooms need one purposeful perimeter "
            "storage/display piece: bookcase, cabinet, or sideboard."
        )
        is_valid = False
    requested_fit_counts, recommended_fit_counts = _fit_warning_counts(room)
    requested_fit_counts = {
        category: count for category, count in requested_fit_counts.items() if category not in waived
    }
    selected_for_fit = [
        {**assets_by_uid[uid], **fields, "uid": uid}
        for uid, fields in zip(canonical_uids, item_fields, strict=True)
        if uid in assets_by_uid
    ]
    fit_target_feedback = _fit_target_validation_feedback(
        decision=decision,
        requested_counts=requested_fit_counts,
        recommended_counts=recommended_fit_counts,
        selected_assets=selected_for_fit,
        assets_by_uid=assets_by_uid,
        fit_satisfaction=fit_satisfaction,
    )
    if fit_target_feedback["errors"]:
        errors.extend(fit_target_feedback["errors"])
        is_valid = False
    explicit_count_constraints = {}
    if not accepted_fit_decision:
        explicit_count_constraints = count_constraints_from_intent_packet(
            intent_packet
        )
    count_guidance = count_guidance_feedback(
        canonical_uids,
        assets_by_uid,
        feasibility_digest,
        explicit_count_constraints=explicit_count_constraints,
        fit_satisfaction=fit_satisfaction,
        enforce_inferred_max=True,
    )
    if count_guidance["errors"]:
        if decision == "continue_anyway":
            warnings.extend(count_guidance["errors"])
        else:
            errors.extend(count_guidance["errors"])
            is_valid = False
    warnings.extend(count_guidance["warnings"])
    requested_role_errors = _requested_role_validation_errors(count_guidance)
    if requested_role_errors:
        errors.extend(requested_role_errors)
        is_valid = False

    layout_preflight = _layout_preflight_for_assets(
        selected_assets=selected_for_fit,
        room=room,
        budget=budget,
        total_cost=total_cost,
    )
    if layout_preflight["errors"]:
        errors.extend(layout_preflight["errors"])
        is_valid = False
    warnings.extend(layout_preflight["warnings"])

    if studio:
        for asset in selected_for_fit:
            if (asset.get("category") in {"rug", "side_table"} or placement_mode_for_asset(asset) == "ceiling") and not asset.get("functional_group"):
                errors.insert(0, f"SELECTION TOOL PROTOCOL: Studio accessory {asset['uid']} requires a functional_group: sleeping, sitting or dining.")
                is_valid = False
                break
    if is_valid:
        unresolved = [
            str(asset.get("uid") or "")
            for asset in selected_for_fit
            if placement_mode_for_asset(asset) == "unknown"
        ]
        if unresolved:
            errors.append(
                "PLACEMENT SEMANTICS UNRESOLVED: "
                + ", ".join(unresolved)
                + ". Replace these assets with catalog items whose floor, "
                "tabletop, wall, or ceiling placement is clear."
            )
            is_valid = False
    if is_valid and studio:
        rug_errors = sitting_rug_selection_errors(selected_for_fit)
        if rug_errors:
            errors.extend(rug_errors)
            is_valid = False
    audit_errors, audit_warnings = _selection_constraint_audit_errors(
        constraint_audit,
        selected_uids=canonical_uids,
        attribute_constraints=intent_packet.get("asset_attribute_constraints") or [],
    )
    warnings.extend(f"SELECTION CONSTRAINT AUDIT: {warning}" for warning in audit_warnings)
    if audit_errors:
        errors.extend(f"SELECTION CONSTRAINT AUDIT: {error}" for error in audit_errors)
        is_valid = False
    attribute_errors = _attribute_errors(selected_for_fit, intent_packet)
    if attribute_errors:
        errors.extend(attribute_errors)
        is_valid = False

    result = {
        "valid": is_valid,
        "status": "PASS" if is_valid else "FAIL - ADJUST SELECTION",
        "errors": errors,
        "warnings": warnings,
        "fit_failed": is_over_crowded or bool(layout_preflight["errors"]),
        "metrics": {
            "total_cost": round(total_cost, 2),
            "remaining_budget": round(budget - total_cost, 2),
            "total_footprint": round(total_footprint, 2),
            "footprint_limit_80pct": round(max_allowed_footprint, 2),
            "footprint_floor": round(footprint_floor, 2),
            "footprint_comfortable_load": round(comfortable_load, 2),
            "category_counts": count_guidance["metrics"]["category_counts"],
            "decor_plant_count": decor_plant_count,
            "role_counts": count_guidance["metrics"]["role_counts"],
            "max_counts_by_category": count_guidance["metrics"][
                "max_counts_by_category"
            ],
            "explicit_count_constraints": count_guidance["metrics"].get(
                "explicit_count_constraints", {}
            ),
            "fit_target_counts": fit_target_feedback["target_counts"],
            "fit_selected_counts": fit_target_feedback["selected_counts"],
            "layout_preflight": layout_preflight["metrics"],
        },
        "assets_not_found": not_found,
        "validated_uids": canonical_uids,
    }
    if not is_valid:
        result["retry_context"] = _selection_retry_context(
            result,
            budget=budget,
            room_type=room_type,
            assets_by_uid=assets_by_uid,
            annotated_assets=copy.deepcopy(selected_for_fit),
        )
    result["feedback"] = json.dumps(result, default=str)
    result["instances"] = instances
    return result


def _selection_constraint_audit_errors(
    audit: Any,
    *,
    selected_uids: list[str] | None = None,
    attribute_constraints: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Split the model's self-audit into blocking and advisory findings.

    Blocking findings are substantive: the model reporting that its own
    selection violates a constraint. Bookkeeping findings — a stale UID left in
    the audit after the asset was dropped, a missing or malformed entry — are
    advisory. The design is validated on its own terms elsewhere, so a good
    selection must not die because the model's scratchpad drifted.
    """
    if not isinstance(audit, dict):
        return [], ["constraint audit is missing"]
    checked_constraints = [
        str(item).strip()
        for item in audit.get("checked_constraints") or []
        if str(item).strip()
    ]
    errors: list[str] = []
    warnings: list[str] = (
        [] if checked_constraints else ["explicit user constraints were not audited"]
    )
    exact_count_violations = [
        str(item).strip()
        for item in audit.get("exact_count_violations") or []
        if str(item).strip()
    ]
    coordination_violations = [
        str(item).strip()
        for item in audit.get("coordination_violations") or []
        if str(item).strip()
    ]
    violations = exact_count_violations + coordination_violations

    selected = {
        str(uid).strip()
        for uid in selected_uids or []
        if str(uid).strip()
    }
    raw_checks = audit.get("attribute_constraint_checks")
    checks = [
        check
        for check in raw_checks or []
        if isinstance(check, dict)
    ]
    for constraint in attribute_constraints or []:
        if not isinstance(constraint, dict) or not constraint.get("required", True):
            continue
        source_label = str(
            constraint.get("source_label")
            or constraint.get("label")
            or constraint.get("value")
            or ""
        ).strip()
        if not source_label:
            continue
        check = next(
            (
                item
                for item in checks
                if str(item.get("source_label") or "").strip() == source_label
            ),
            None,
        )
        if check is None:
            warnings.append(
                f'attribute constraint "{source_label}" was not audited'
            )
            continue
        targets = {
            str(uid).strip()
            for uid in check.get("target_uids") or []
            if str(uid).strip()
        }
        satisfied = {
            str(uid).strip()
            for uid in check.get("satisfied_uids") or []
            if str(uid).strip()
        }
        unsatisfied = {
            str(uid).strip()
            for uid in check.get("unsatisfied_uids") or []
            if str(uid).strip()
        }
        if not targets:
            warnings.append(
                f'attribute constraint "{source_label}" has no audited target UIDs'
            )
            continue
        unknown_targets = sorted(targets - selected)
        if unknown_targets:
            warnings.append(
                f'attribute constraint "{source_label}" audits unselected UIDs: '
                f"{unknown_targets}"
            )
        if satisfied & unsatisfied:
            warnings.append(
                f'attribute constraint "{source_label}" marks the same UID both '
                "satisfied and unsatisfied"
            )
        unaudited_targets = sorted(targets - satisfied - unsatisfied)
        unexpected_results = sorted((satisfied | unsatisfied) - targets)
        if unaudited_targets or unexpected_results:
            warnings.append(
                f'attribute constraint "{source_label}" has inconsistent UID '
                f"coverage; unaudited={unaudited_targets}, "
                f"outside_target={unexpected_results}"
            )
        # Substantive: the model says its own pick fails the constraint, and
        # the offending asset is actually in the selection.
        live_unsatisfied = sorted(unsatisfied & selected)
        if live_unsatisfied:
            errors.append(
                f'attribute constraint "{source_label}" is not satisfied by '
                f"{live_unsatisfied}"
            )

    errors.extend(violations)
    if audit.get("passed") is not True and not violations and not errors:
        errors.append("selected assets do not satisfy all explicit user constraints")
    return errors, warnings


def _selection_retry_context(
    result: dict[str, Any],
    *,
    budget: float,
    room_type: str,
    assets_by_uid: dict[str, dict],
    annotated_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Give the selector focused catalog facts without choosing replacements."""
    max_total = budget * (1 + BUDGET_FLEX_PCT)
    selected_counts = Counter(list(result.get("validated_uids") or []))
    feasible_anchor_table_pairs: list[dict[str, Any]] = []
    circulation_feasible_living_clusters: list[dict[str, Any]] = []
    annotated_assets_by_uid = {
        str(asset.get("uid") or asset.get("instance_key") or ""): asset
        for asset in annotated_assets or []
    }

    layout_preflight = (
        (result.get("metrics") or {}).get("layout_preflight") or {}
    )
    protected_path_zones = layout_preflight.get("protected_path_zones") or []
    if room_type != "living_room" and not any(
        normalize_category(assets_by_uid[uid].get("category")) in FIT_ANCHOR_SEATING
        for uid in selected_counts if uid in assets_by_uid
    ):
        # Other room purposes must not prescribe an absent living-room group.
        protected_path_zones = []
    if protected_path_zones:
        metrics = result.get("metrics") or {}
        explicit_constraints = metrics.get("explicit_count_constraints") or {}
        accent_constraints = [
            (normalize_category(category), constraint)
            for category, constraint in explicit_constraints.items()
            if isinstance(constraint, dict)
            and not constraint.get("optional")
            and normalize_category(category) in FIT_ACCENT_SEATING
        ]
        required_seat_count = max(
            (
                int(constraint.get("count") or 0)
                for _, constraint in accent_constraints
            ),
            default=0,
        )
        if required_seat_count <= 0:
            required_seat_count = max(
                (
                    len(cluster.get("accent_seat_uids") or [])
                    for cluster in layout_preflight.get("clusters") or []
                    if cluster.get("type") == "living_seating"
                ),
                default=0,
            )
        exact_seat_categories = {
            category
            for category, constraint in accent_constraints
            if constraint.get("exact")
        }

        anchor_seating = [
            (uid, asset)
            for uid, asset in assets_by_uid.items()
            if normalize_category(asset.get("category")) in FIT_ANCHOR_SEATING
            and (_asset_price_float(asset) or 0.0) <= max_total
        ]
        coffee_tables = [
            (uid, asset)
            for uid, asset in assets_by_uid.items()
            if normalize_category(asset.get("category")) == "coffee_table"
            and (_asset_price_float(asset) or 0.0) <= max_total
        ]
        anchor_seating.sort(
            key=lambda item: (
                _asset_width_depth(item[1])[0] * _asset_width_depth(item[1])[1],
                item[0],
            )
        )
        for anchor_uid, anchor in anchor_seating:
            anchor_width, anchor_depth = _asset_width_depth(anchor)
            compatible_tables = []
            for table_uid, table in coffee_tables:
                table_width, table_depth = _asset_width_depth(table)
                arrangements = _canonical_living_arrangement_footprints(
                    anchor,
                    table,
                    [],
                )
                if not all(
                    any(
                        any(
                            _fits_rect(
                                float(arrangement["width"]),
                                float(arrangement["depth"]),
                                max(0.0, float(zone["width"]) - 0.08),
                                max(0.0, float(zone["depth"]) - 0.08),
                            )
                            for arrangement in arrangements
                        )
                        for zone in path["zones"]
                    )
                    for path in protected_path_zones
                ):
                    continue
                compatible_tables.append(
                    {
                        "uid": table_uid,
                        "width": round(table_width, 3),
                        "depth": round(table_depth, 3),
                        "price": round(_asset_price_float(table) or 0.0, 2),
                        "styles": table.get("styles") or [],
                        "materials": table.get("materials") or "",
                        "colors": table.get("colors")
                        or table.get("available_colors")
                        or [],
                    }
                )
            if not compatible_tables:
                continue
            compatible_tables.sort(
                key=lambda item: (
                    item["width"] * item["depth"],
                    item["uid"],
                )
            )
            feasible_anchor_table_pairs.append(
                {
                    "anchor_seating": {
                        "uid": anchor_uid,
                        "category": normalize_category(anchor.get("category")),
                        "width": round(anchor_width, 3),
                        "depth": round(anchor_depth, 3),
                        "price": round(_asset_price_float(anchor) or 0.0, 2),
                        "styles": anchor.get("styles") or [],
                        "materials": anchor.get("materials") or "",
                        "colors": anchor.get("colors")
                        or anchor.get("available_colors")
                        or [],
                    },
                    "compatible_coffee_tables": compatible_tables,
                }
            )

        compatible_seats: list[tuple[str, dict[str, Any]]] = []
        if required_seat_count > 0:
            for candidate_uid, catalog_candidate in assets_by_uid.items():
                candidate = {
                    **catalog_candidate,
                    **annotated_assets_by_uid.get(candidate_uid, {}),
                }
                category = normalize_category(candidate.get("category"))
                candidate_price = _asset_price_float(candidate)
                if (
                    candidate_price is None
                    or candidate_price > max_total
                    or (
                        exact_seat_categories
                        and category not in exact_seat_categories
                    )
                    or not _is_living_accent_seating(candidate)
                ):
                    continue
                compatible_seats.append((candidate_uid, candidate))
        else:
            compatible_seats.append(("", {}))

        complete_options: list[dict[str, Any]] = []
        for pair in feasible_anchor_table_pairs:
            anchor_fact = pair["anchor_seating"]
            anchor_uid = anchor_fact["uid"]
            for table_fact in pair["compatible_coffee_tables"]:
                table_uid = table_fact["uid"]
                for seat_uid, seat in compatible_seats:
                    arrangements = _canonical_living_arrangement_footprints(
                        assets_by_uid[anchor_uid],
                        assets_by_uid[table_uid],
                        [seat] * required_seat_count,
                    )

                    certified_arrangements = [
                        arrangement
                        for arrangement in arrangements
                        if all(
                            any(
                                _fits_rect(
                                    float(arrangement["width"]),
                                    float(arrangement["depth"]),
                                    max(
                                        0.0,
                                        float(zone["width"]) - 0.08,
                                    ),
                                    max(
                                        0.0,
                                        float(zone["depth"]) - 0.08,
                                    ),
                                )
                                for zone in path["zones"]
                            )
                            for path in protected_path_zones
                        )
                    ]
                    if not certified_arrangements:
                        continue

                    seat_price = _asset_price_float(seat) or 0.0
                    seat_width, seat_depth = (
                        _asset_width_depth(seat)
                        if required_seat_count > 0
                        else (0.0, 0.0)
                    )
                    option = {
                        "anchor_seating": anchor_fact,
                        "coffee_table": table_fact,
                        "required_matching_seat_count": required_seat_count,
                        "accent_seating": (
                            {
                                "uid": seat_uid,
                                "category": normalize_category(
                                    seat.get("category")
                                ),
                                "width": round(seat_width, 3),
                                "depth": round(seat_depth, 3),
                                "unit_price": round(seat_price, 2),
                                "repeated_cost": round(
                                    seat_price * required_seat_count,
                                    2,
                                ),
                                "styles": seat.get("styles") or [],
                                "materials": seat.get("materials") or "",
                                "colors": seat.get("colors")
                                or seat.get("available_colors")
                                or [],
                            }
                            if required_seat_count > 0
                            else None
                        ),
                        "group_cost": round(
                            float(anchor_fact["price"])
                            + float(table_fact["price"])
                            + seat_price * required_seat_count,
                            2,
                        ),
                        "certified_arrangements": [
                            {
                                "name": arrangement["name"],
                                "width": round(arrangement["width"], 3),
                                "depth": round(arrangement["depth"], 3),
                            }
                            for arrangement in certified_arrangements
                        ],
                    }
                    complete_options.append(option)

        complete_options.sort(
            key=lambda option: (
                min(
                    arrangement["width"] * arrangement["depth"]
                    for arrangement in option["certified_arrangements"]
                ),
                option["group_cost"],
                option["anchor_seating"]["uid"],
                option["coffee_table"]["uid"],
                (option.get("accent_seating") or {}).get("uid", ""),
            )
        )
        max_cluster_options = 32
        if len(complete_options) <= max_cluster_options:
            circulation_feasible_living_clusters = complete_options
        else:
            sampled_indexes = sorted(
                {
                    round(
                        index
                        * (len(complete_options) - 1)
                        / (max_cluster_options - 1)
                    )
                    for index in range(max_cluster_options)
                }
            )
            circulation_feasible_living_clusters = [
                complete_options[index] for index in sampled_indexes
            ]

    metrics = result.get("metrics") or {}
    current_total = float(metrics.get("total_cost") or 0.0)
    current_footprint = float(metrics.get("total_footprint") or 0.0)
    footprint_floor = float(metrics.get("footprint_floor") or 0.0)
    return {
        "budget_facts": {
            "budget_cap": round(max_total, 2),
            "current_total": round(current_total, 2),
            "remove_at_least": round(max(0.0, current_total - max_total), 2),
        },
        "density_facts": {
            "current_footprint_sqm": round(current_footprint, 2),
            "footprint_floor_sqm": round(footprint_floor, 2),
            "add_footprint_at_least_sqm": round(
                max(0.0, footprint_floor - current_footprint), 2
            ),
        },
        "circulation_feasible_living_clusters": (
            circulation_feasible_living_clusters
        ),
        "support_requirements": layout_preflight.get(
            "support_requirements",
            [],
        ),
        "instruction": (
            "Choose the design yourself from the original catalog metadata. "
            "When circulation_feasible_living_clusters is non-empty, select one "
            "complete anchor-seating, coffee-table, and repeated accent-seat "
            "group from those exact geometry-certified options. Do not mix "
            "components across options. Use your own semantic and style judgment "
            "among the options; their ordering and sampling are physical catalog "
            "coverage, not a design recommendation. "
            "Stay under the budget cap; when under-furnished, add the fewest "
            "coherent, useful pieces needed to meet the footprint floor, preferring "
            "one or two substantial additions over several small fillers."
        ),
    }
