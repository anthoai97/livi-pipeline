"""Variant stages: run once per variant inside the variant subgraph.

Each stage is `async def stage(state: VariantState, ctx: StageContext) -> VariantState`.
It reads variant-state keys and returns a partial update with only the keys it
owns. `state["shared"]` holds the request, intent, room, slots, and candidate
pool; it is shared by all variants, so copy a record before changing it.
`state["variant_index"]` and `state["direction"]` identify the variant.

The graph owns the loops and their bounds (app.graph): it counts
`selection_turns` and `correction_proposals`, repeats select until
`selection_validation["valid"]` or 8 turns, and repeats correct while
`blocking_findings` is non-empty, up to 8 proposals. A stage that raises fails
only its own variant (reason `model_call_failed` for ModelCallError, otherwise
`variant_error`).

`ctx` is the same as for shared stages (see app.shared_stages); model calls made
through `ctx.generate` are recorded under this stage and variant.

Prompts port the legacy fresh-design text (livinit_pipeline
src/nodes/asset_selection/agent.py, src/nodes/layout_generation/initial_flow.py,
src/nodes/layout_fix.py). Tool-call wording becomes one JSON response, and the
revision, seed-image, asset-feedback, and owned-asset branches are dropped.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app import contracts
from app.rules.categories import (
    is_ceiling_mounted_asset,
    is_floor_lamp_asset,
    is_table_lamp_asset,
    is_wall_aligned_asset,
    is_wall_mounted_asset,
)
from app.rules.door_geometry import format_door_for_prompt
from app.rules.layout.analysis import analyze_layout, final_layout_check, findings_by_level
from app.rules.layout.comfort import comfort_placement_facts
from app.rules.layout.constants import ISSUE_KEYS_BY_TIER
from app.rules.layout.dining import dining_chair_table_facts, dining_placement_facts
from app.rules.layout.formatting import format_issues, format_layout
from app.rules.layout.metrics import layout_issue_score
from app.rules.layout.normalization import layout_from_placements, move_asset_with_supports, normalize_layout
from app.rules.layout.studio import is_freestanding_studio_media_support
from app.rules.layout.validation_geometry import overlap_separation_facts, wall_mount_placement_facts
from app.rules.layout_rules import (
    LAYOUT_SYSTEM_INSTRUCTION,
    build_rules_block,
    build_selection_guidance_block,
    coordinate_system_block,
)
from app.rules.placement_mode import placement_mode_for_asset
from app.rules.planner.feasibility_digest import format_feasibility_digest_for_prompt
from app.rules.planner.intent_packet import format_intent_packet_for_prompt, intent_prompt_text
from app.rules.planner.room_facts import format_room_facts_for_prompt
from app.rules.planner.taxonomy import normalize_category
from app.rules.selection.catalog import _price_constraint_category_matches, catalog_asset
from app.rules.selection.constants import BUDGET_FLEX_PCT
from app.rules.selection.fit import fit_step_guidance
from app.rules.selection.validation import is_decor_plant, requires_spacious_perimeter_storage, validate_selection
from app.shared_stages import LIMIT_KEYS

if TYPE_CHECKING:
    from app.graph import VariantState
    from app.run import StageContext

Record = dict[str, Any]

# --- direction ---------------------------------------------------------------

# Legacy _VARIANT_DIRECTIVES (src/nodes/asset_selection/agent.py:264), unchanged.
_VARIANT_DIRECTIVES = (
    "",
    """
VARIANT DIRECTION (one of three parallel proposals — this one must look different):
- Anchor on a soft, low, rounded silhouette (curved or modular sofa, plinth or low legs),
  not a boxy straight-armed one.
- Dominant material family: warm tactile textiles and warm wood (boucle, woven fabric,
  oak/walnut). Avoid leather, metal, and glass as the leading material.
- Warm color temperature throughout: cream, sand, terracotta, caramel, warm greys.
- Every other pick must follow the anchor's material and warmth.
""",
    """
VARIANT DIRECTION (one of three parallel proposals — this one must look different):
- Anchor on a compact, structured silhouette (tight-back, straight arms, raised slim legs)
  with a smaller footprint than the largest option that fits.
- Scale strategy: compact pieces across only the functional categories the room needs,
  rather than a few oversized statement pieces. Do not add categories merely to create
  variety or use the remaining budget.
- Cooler, cleaner color temperature: cool greys, charcoal, black, deep blue/green accents.
""",
)
# Room-specific versions for variants 1 and 2 (legacy _build_selection_prompt).
_ROOM_DIRECTIVES = {
    "bedroom": (
        "VARIANT DIRECTION: Anchor on a soft, rounded complete-bed silhouette, warm tactile textiles "
        "and warm wood. Coordinate supporting pieces in cream, sand, terracotta or warm greys.",
        "VARIANT DIRECTION: Anchor on a compact, structured complete bed with a smaller fitting footprint. "
        "Coordinate useful supporting pieces in cool greys, charcoal or deep blue/green accents.",
    ),
    "dining_room": (
        "VARIANT DIRECTION: Choose a warm, rounded dining table and matching chairs with tactile natural materials.",
        "VARIANT DIRECTION: Choose a compact, structured dining table and matching chairs with a restrained contrasting palette.",
    ),
    "studio": (
        "VARIANT DIRECTION: Coordinate compact complete bed, sofa/loveseat and dining furniture in warm natural materials.",
        "VARIANT DIRECTION: Coordinate a compact complete bed, separate sitting and dining groups in a restrained contrasting palette.",
    ),
}


def direction(variant_index: int, room_type: str) -> str:
    """Return the variant's direction text: the legacy `_VARIANT_DIRECTIVES` text
    unchanged, with its room-specific versions. Empty for variant 0."""
    directive = _VARIANT_DIRECTIVES[variant_index]
    if directive and room_type in _ROOM_DIRECTIVES:
        directive = _ROOM_DIRECTIVES[room_type][0 if variant_index == 1 else 1]
    return directive


# --- select ------------------------------------------------------------------

# Legacy validate_selection tool arguments (src/nodes/asset_selection/contracts.py)
# as one JSON response. The repeated uid list is selected_assets itself.


class SelectedAsset(BaseModel):
    uid: str
    reason: str
    functional_group: Literal["sleeping", "sitting", "dining"] | None = Field(
        default=None,
        description="For studio rugs, side tables and ceiling lights, the functional group this accessory serves. Bedside tables serve sleeping.",
    )


class SelectionStrategy(BaseModel):
    summary: str
    higher_budget_additions: str
    lower_budget_savings: str
    gaps: str
    conflict: str


class SelectedCount(BaseModel):
    category: str
    count: int
    selected_uids: list[str]
    reason: str | None = None


class Substitution(BaseModel):
    requested_category: str
    selected_uid: str
    reason: str


class FitSatisfaction(BaseModel):
    selected_counts: list[SelectedCount] = []
    substitutions: list[Substitution] = []


class AttributeConstraintCheck(BaseModel):
    source_label: str
    target_uids: list[str]
    satisfied_uids: list[str]
    unsatisfied_uids: list[str]
    reason: str


class ConstraintAudit(BaseModel):
    passed: bool
    checked_constraints: list[str]
    exact_count_violations: list[str] = Field(
        description="Actual exact-count violations. Must be empty when passed=true; do not put success explanations here."
    )
    coordination_violations: list[str] = Field(
        description=(
            "Actual style, material, color, or coordination violations. "
            "Must be empty when passed=true; do not put success explanations here."
        )
    )
    attribute_constraint_checks: list[AttributeConstraintCheck] = Field(
        description=(
            "One entry per required asset_attribute_constraint. Infer the constraint's semantic target "
            "from its source_label and audit every selected UID in that target."
        )
    )


class Selection(BaseModel):
    selected_assets: list[SelectedAsset]
    total_cost: float | None = None
    total_footprint_sqm: float | None = None
    selection_strategy: SelectionStrategy
    fit_satisfaction: FitSatisfaction | None = None
    constraint_audit: ConstraintAudit


_CSV_HEADER = "uid,category,name,price,width,depth,height,brand,color,style,material,asset_description,mount_type,features,is_decor_item,is_placeholder"


def _csv_text(value: Any, max_len: int | None = None) -> str:
    text = ", ".join(value) if isinstance(value, list) else str(value or "")
    return f'"{text[:max_len].replace(chr(34), "")}"'


def _csv_row(asset: Record) -> str:
    """Legacy _assets_to_csv row from a prepared record. Prepared records have no shape or popularity."""
    return (
        f"{asset['uid']},{normalize_category(asset.get('category'))},{_csv_text(asset.get('title'))},"
        f"{asset['price']:.2f},{asset['width']:.3f},{asset['depth']:.3f},{asset['height']:.3f},"
        f"{_csv_text(asset.get('brand'))},{_csv_text(asset.get('colors'))},{_csv_text(asset.get('styles'))},"
        f"{_csv_text(asset.get('materials'))},{_csv_text(asset.get('description'), 120)},"
        f"{_csv_text(asset.get('mount_type'))},{_csv_text(asset.get('features'))},"
        f"{'true' if asset['is_decor_item'] else 'false'},false"
    )


def _slot_heading(slot: Record) -> str:
    if slot["kind"] == "requested":
        role = " (fills a required role)" if slot["required"] else ""
        relaxed = "; no product met the request's size, attribute, price, or brand limits, so these candidates ignore them" if slot["relaxed"] else ""
        return f'SLOT {slot["id"]}: requested "{slot["label"]}" x{slot["count"]}{role}{relaxed}'
    if slot["kind"] == "required":
        return f"SLOT {slot['id']}: required {slot['label']}" + (f" x{slot['count']}" if slot["count"] > 1 else "")
    return f"SLOT {slot['id']}: {slot['kind']} {slot['label']}"


def _catalog_block(slots: list[Record], pool: dict[str, list[Record]]) -> str:
    """Candidates grouped by slot. A product found by several slots is listed once."""
    seen: set[str] = set()
    lines = []
    for slot in slots:
        if slot["gap"]:
            continue
        rows = []
        for record in pool[slot["id"]]:
            uid = str(record["asset_id"])
            if uid not in seen:
                seen.add(uid)
                rows.append(_csv_row({**catalog_asset(record), "uid": uid}))
        if rows:
            lines += [_slot_heading(slot), _CSV_HEADER, *rows, ""]
    return "\n".join(lines).rstrip()


def _candidates(pool: dict[str, list[Record]]) -> list[Record]:
    by_id = {str(record["asset_id"]): record for rows in pool.values() for record in rows}
    return list(by_id.values())


def _gap_slots(slots: list[Record]) -> list[Record]:
    return [slot for slot in slots if slot["gap"] and slot["kind"] == "requested"]


def _selection_intent(intent: Record, slots: list[Record]) -> Record:
    """The intent without the request's limits on requested items whose slot was searched without them.

    A required item that no product could satisfy within its limits must not fail
    selection on those same limits: its size, attribute, and price limits are
    dropped, and its attribute constraints become preferences.
    """
    relaxed = [slot["category"] for slot in slots if slot["relaxed"] and slot["category"]]
    if not relaxed:
        return intent

    def scoped(constraint: Record) -> bool:
        return bool(constraint.get("category")) and any(_price_constraint_category_matches(constraint["category"], c) for c in relaxed)

    return {
        **intent,
        "requested_items": [
            {key: value for key, value in item.items() if key not in LIMIT_KEYS} if item["canonical_category"] in relaxed else item
            for item in intent.get("requested_items") or []
        ],
        "price_constraints": [c for c in intent.get("price_constraints") or [] if not scoped(c)],
        "asset_attribute_constraints": [
            {**c, "required": False} if scoped(c) else c for c in intent.get("asset_attribute_constraints") or []
        ],
    }


def _selection_prompt(state: VariantState, intent: Record, fit_step: str | None) -> str:
    """Legacy _build_selection_prompt for a fresh design, plus slot gaps and the previous turn's feedback."""
    shared = state["shared"]
    request, room = shared["request"], shared["room"]
    room_width, room_depth = request.room_area
    budget = request.budget
    room_type = room["room_type"]
    digest = room["digest"]
    furniture_area = float(room["furniture_area_sqm"])
    candidates = [{**catalog_asset(record), "uid": str(record["asset_id"])} for record in _candidates(shared["pool"])]
    density_budget = digest.get("density_budget") or {}
    footprint_floor = float(density_budget.get("sparse_below_load_sqm") or 0.0)
    comfortable_load = float(density_budget.get("comfortable_load_sqm") or 0.0)
    plant_guidance = ""
    bedroom = room_type == "bedroom"
    dining = room_type == "dining_room"
    studio = room_type == "studio"
    media_selection_facts = (
        [{key: value for key, value in row.items() if key != "viewing_target"}
         for row in comfort_placement_facts(candidates, room_type)["tv_preferred_ranges"]]
        if studio else []
    )
    # Planning reference, not a fit verdict: actual eye/screen insets and lateral
    # offsets are checked from poses later. It makes the room-scale tradeoff explicit.
    opposed_wall_reference = max(0.0, min(room_width, room_depth) - 0.6)
    wall_viewing_candidates = [row["uid"] for row in media_selection_facts
                               if row["estimated_view_distance_range_m"][1] >= opposed_wall_reference]
    anchor_name = "complete bed, compact sofa and dining table" if studio else "complete adult bed" if bedroom else "dining table" if dining else "sofa"
    required_role_guidance = (
        "Apply the studio furnishing policy in the feasibility digest: preserve one complete standalone bed, "
        "separate compact sitting group and one dining table with two chairs unless an explicit dining count changes it. "
        "These three core groups are mandatory even after a fit decision. Accessories do not substitute for a missing group. "
        "For each selected rug, side table or ceiling light, set functional_group to sleeping, sitting or dining according to "
        "the brief. This association remains fixed during layout; do not use one rug to serve multiple groups. "
        "A side table for bedside lighting belongs to sleeping, not sitting or dining. "
        "Choose a sitting area rug whose usable width spans the sofa and whose depth permits front-leg engagement "
        "plus at least 0.60m of useful rug in front; do not select a small mat as a substitute for an area rug. "
        "Keep its envelope out of the bed and dining group. In compact studios plan dining chairs AND 0.56m pull-out "
        "before choosing sofa depth and media support. For shared TV viewing consider both bed and sofa distances "
        "relative to screen width; do not assume a TV on the opposite wall will be comfortable. "
        "For bed-only or shared viewing, plan a wall-backed TV first and choose a display whose preferred range "
        "covers the anticipated viewing distance across the room, allowing for head and screen insets. "
        "Choose a compatible console wide enough for that display. A compact furniture variant must not "
        "automatically downsize its TV or replace an area rug with a runner. Prefer a larger fitting screen "
        "over pulling a small screen and console into circulation just to shorten viewing distance. "
        "Requested optional functions take priority over unrequested rugs, tables and other accessories."
        if studio else
        "Exactly one complete adult bed (category=bed) is mandatory in a fresh bedroom, including after a fit decision. "
        "Bed frames, bunk beds, separate mattresses and headboards are unsupported substitutes. "
        "Choose a standalone bed, not a bedroom set with integrated, built-in or bundled nightstands. "
        "Embedded bedside furniture cannot count as separate selected nightstands or justify adding a duplicate pair; "
        "the whole model footprint does not establish reach from the actual sleeping surface. "
        "Plan useful bedside surfaces, lighting and clothing storage as part of the bedroom's everyday functions. "
        "For a normal spacious bedroom, prefer a coordinated bedside pair where both sides are usable, "
        "purposeful clothing storage and a coherent use of the remaining zones. Honor explicit sparse, "
        "single-bedside and storage-exclusion requests; compact rooms need fewer pieces and clear access. "
        "Wardrobes are opt-in; do not add one to a generic bedroom. When requested, a dresser is not a substitute. "
        "A dresser provides folded-clothes storage, not hanging storage. Claim hanging storage only "
        "when product metadata confirms that capability; never relabel a generic cabinet as a wardrobe. "
        "Do not infer mattress size from exterior furniture dimensions. Include fitting lighting."
        if bedroom else
        "Apply the dining furnishing policy in the feasibility digest: keep the usable table/chair group and access ahead of optional additions."
        if dining else
        "Anchor seating (sofa/sectional/loveseat) and a surface (coffee_table/side_table/desk) are mandatory; a selection missing either fails validation. Lighting (floor_lamp/table_lamp/lamp) is expected in every room — include one before adding a second seat or any decor."
    )
    variant_directive = state["direction"]
    excluded_categories = set(intent.get("excluded_categories") or [])
    if not studio and "planter" not in excluded_categories:
        plant_uids = [asset["uid"] for asset in candidates if is_decor_plant(asset)]
        plant_guidance = (
            "- Include at least one fitting, non-shoppable decor plant in every fresh "
            "design, including after an accepted fit decision. This is a required "
            "finishing item, not an optional extra. Choose from these eligible "
            f"category=planter, is_decor_item=true UIDs: {', '.join(plant_uids) or 'none available'}. "
            "Retail planters and other decor categories do not satisfy this requirement. "
            "Plan room space for the plant without compromising circulation.\n"
        )
    perimeter_storage_guidance = ""
    needs_perimeter_storage = requires_spacious_perimeter_storage(
        room_type=room_type, intent_packet=intent, feasibility_digest=digest, fit_step=fit_step,
    )
    if needs_perimeter_storage:
        perimeter_storage_guidance = (
            "- This spacious room must include one purposeful perimeter storage/display "
            "piece: a bookcase, cabinet, or sideboard chosen to match the style. Default "
            "to exactly one unless the user explicitly requests more. A media console, "
            "side table, or small organizer does not satisfy this role.\n"
        )
    optional_piece_guidance = (
        "Apart from that single perimeter piece, do not add storage, media, extra "
        f"seating, side tables, or {'other decor' if plant_guidance else 'decor'} by default."
        if needs_perimeter_storage
        else f"Do not add storage, media, extra seating, side tables, or {'other decor' if plant_guidance else 'decor'} by default."
    )
    selection_size_guidance = (
        "Choose the smallest coherent set that satisfies the requested functions and reaches the footprint floor"
    )
    if bedroom:
        optional_piece_guidance = (
            "Judge bedroom completeness across the usable room, including bedside support, lighting and "
            "clothing storage. A fitting dressing or reading zone may justify additional furniture "
            "in a spacious room even after the sparse floor is met. Respect user exclusions and minimal "
            "briefs; an extra piece needs a purpose, not unused budget. Explain deliberate omissions."
        )
        selection_size_guidance = (
            "Choose a coherent, functionally complete bedroom set scaled to the usable room. "
            "The sparse floor is a lower guardrail, not proof of completeness; satisfy it"
        )
    if dining:
        optional_piece_guidance = (
            "Apply the conditional dining furnishing suggestions above after seating and access fit; "
            "they do not impose counts or a floor-fill target. Respect minimal briefs and exclusions."
        )
        selection_size_guidance = (
            "Choose a complete dining group at the requested or default capacity, then consider useful conditional additions"
        )
    rug_guidance = (
        "Rugs are optional in a dining room; include one only when requested and large enough for the table and chair movement."
        if dining else
        "Include at least one fitting rug in every fresh design unless the user explicitly excludes rugs. Rugs do not count toward the density floor, but they must physically fit the room."
    )
    selection_guidance = build_selection_guidance_block(room_width=room_width, room_depth=room_depth, room_type=room_type)
    fit_decision_guidance = fit_step_guidance(fit_step, room)
    if fit_step == "compact":
        count_guidance_constraint = (
            "Use room count guidance as context only for continue-anyway: "
            "explicit requested items/counts are the target, and max_counts_by_category are not hard caps."
        )
        invalid_fix_guidance = (
            "If invalid: fix hard errors (bring cost under the budget cap, swap to smaller if overcrowded, fix unknown UIDs, "
            "fix layout preflight failures, and preserve as many requested-category assets as "
            "validation permits). "
            "Keep max-count warnings unless a coherent change clearly improves the room."
        )
    else:
        count_guidance_constraint = (
            "Use room count guidance: explicit requested items/counts take priority; "
            "max_counts_by_category are caps. You decide how many pieces the room needs — "
            "let the room size, density floor, and the user's intent drive the count. Treat "
            "non-optional intent-packet inferred counts as maxima; optional inferred counts are "
            "design suggestions, not user-stated limits. Choose justified paired pieces within the room caps; "
            "when in doubt, omit an optional piece unless it has a clear functional or compositional purpose."
        )
        invalid_fix_guidance = (
            "If invalid: fix hard errors (bring cost under the budget cap, add coherent pieces if under-furnished, swap to smaller if overcrowded, satisfy missing fit targets "
            "with compact exact matches or justified substitutes, fix unknown UIDs, and satisfy explicit "
            "count/max-count guidance). Fix layout preflight failures by replacing oversized rugs or "
            "incompatible support/desk/dining pieces."
        )

    prompt = f"""Select furniture assets matching the user's style and design vision.

ROOM: {room_width:.2f}m x {room_depth:.2f}m | Furniture area: {furniture_area:.2f} sqm
BUDGET: ${budget:.2f}
INTENT: {intent_prompt_text(intent, request.user_intent)}
{format_intent_packet_for_prompt(intent)}
{format_room_facts_for_prompt(room["facts"])}
{format_feasibility_digest_for_prompt(digest)}

{selection_guidance}
{fit_decision_guidance}{variant_directive}

CATALOG (grouped by search slot; each slot lists only products that passed its filters):
{_catalog_block(shared["slots"], shared["pool"])}

STUDIO TV SIZE / VIEWING RANGE FACTS (metres; choose for the requested bed/sofa viewers):
{json.dumps(media_selection_facts) if media_selection_facts else "Not applicable"}
{f"For bed/shared viewing across opposing walls, the shorter room span minus a 0.60m combined eye/screen inset gives an approximate {opposed_wall_reference:.2f}m planning reference. Catalog screens whose preferred range reaches it: {wall_viewing_candidates}. Prefer these with a fitting console for a wall-backed arrangement; smaller screens need an intentionally closer arrangement. This is guidance, not a pose or feasibility guarantee." if studio and media_selection_facts else ""}

CONSTRAINTS:
- Use features as supported product capabilities. mount_type=wall_secured requires floor placement against a wall; wall_mounted and ceiling_mounted require those mounting surfaces. An empty mount_type is unknown.
- {required_role_guidance}
- {rug_guidance}
{plant_guidance}- Budget is a spending cap, not a target: total cost must stay at or under ${budget * (1 + BUDGET_FLEX_PCT):.2f} (${budget:.2f} + 10% flex). There is no minimum spend — never inflate item prices to use the budget up.
- Furnish the room: total footprint (width x depth x 2 per asset, rugs excluded) must be at least {footprint_floor:.2f} sqm and at most {furniture_area:.2f} sqm. The comfortable load of {comfortable_load:.2f} sqm is an upper reference, not a target. Once the requested functions and density floor are satisfied, do not add pieces merely to approach it.
{perimeter_storage_guidance}- Physical layout preflight must pass: rugs must fit the room; every tabletop asset must fit an eligible selected support surface; each selected TV must fit a selected media support; desk/dining clusters must fit with their chairs; and tiny rooms must not receive extra seating or floor lamps.
- {count_guidance_constraint}
- Assets with is_decor_item=true are non-shoppable: their zero budget contribution is bookkeeping, not a retail price. is_placeholder=true identifies temporary furniture for layout evaluation; a verified wardrobe placeholder can satisfy clothing storage, but cannot be purchased or count as spending more budget. Other non-shoppable finishing accents remain optional apart from any fresh-design plant requirement stated above. Never describe these items as free products.
- TVs are normal catalog assets. Treat a TV and its media support as one pair: select neither, or select both a TV and a tv_stand/media_unit/media_console (a console_table is also a valid support). The TV must fit: width <= support width - 0.07m and depth <= max(0.08m, support depth - 0.07m). A TV without a fitting support, or a media support without a TV, fails validation; the server will not add, remove, or replace either asset.
- TVs are non-sellable: exclude TV prices from every budget total while still including the TV UID in selected_assets and the layout.
- You MUST return the exact UID list as selected_assets (one entry per unit), matching per-asset reasons, and the final selection strategy; the accepted response is the final result

STRATEGY:
- Functional completeness over spend: satisfy the user's requested functions with a coherent set, then stop. Unspent budget is acceptable; never add an item solely because budget remains.
- The {anchor_name}/anchor piece sets the style direction for all other picks
- Primary goal: aesthetic coherence — select assets whose brand, color, style, shape, and description match the intent
- Treat intent-packet brand constraints as strong preferences, not hard requirements. For each needed category, use the preferred brand when a catalog option also satisfies category, fit, budget, and every required attribute. If none does, select the best eligible option from another brand and identify that category-level fallback in selection_strategy.gaps. Never sacrifice a required role, physical fit, budget, or explicit non-brand attribute to preserve the brand preference.
- When multiple assets share a category, pick the best style match at a moderate price; reach for premium versions only after every furnishing role the room needs is covered
- Treat every optional piece as a design decision: include it only when it serves a clear requested function or improves the composition in a specific way. {optional_piece_guidance}
- If the requested set is below the density floor, prefer one or two substantial, useful additions over several small filler pieces. State the specific purpose of each addition.
- Small room → prioritize essentials; do not add optional extras beyond the room-scale count caps
- Duplicates are allowed only when the requested or justified count calls for multiples (e.g. 2x accent_chair_5 for a requested pair). Seating multiples must repeat the same UID as a matched set unless the user explicitly asks for variety; for other categories only mix different UIDs if their style, color, and shape are clearly compatible.
- Honor user-stated counts and non-optional inferred count limits. Optional inferred counts are suggestions, not user requests. Never duplicate a category or add a new category purely to burn budget.

PROCESS:
0. Before selecting, estimate total footprint and cost from the catalog CSV. {selection_size_guidance} while staying under the budget cap. Do not drop coherent requested furniture solely to satisfy budget.
1. Select the {anchor_name} first, then build remaining selection around its style
2. Before every response, audit the proposed exact UID set against every user-stated exact count and every required style, material, color, and coordination constraint. Brand constraints are advisory and must not make constraint_audit fail. For every other required intent-packet asset_attribute_constraint, add one attribute_constraint_check using its exact source_label. Infer the semantic target from that source_label and the requested-item descriptors, then name every selected UID in that target: for example, "main seating" covers every selected primary and accent seat, and unrelated rugs, lamps, or decor cannot satisfy it. A category=null constraint is not automatically universal; use its natural-language scope. Replace any violating asset before validation, then include the completed constraint_audit with the response. When passed=true, both violation arrays and every unsatisfied_uids array must be empty; put success evidence in checked_constraints and attribute-check reasons, never in a violation array. Include fit_satisfaction when selected UIDs satisfy requested categories through compact substitutes, catalog-label mismatches, or an accepted fit decision.
3. {invalid_fix_guidance}
4. Include the complete final payload in every response. When validation succeeds, that accepted response ends selection; there is no later formatting response.

OUTPUT JSON:
{{
  "selected_assets": [{{"uid": "...", "reason": "Metadata match: color=X matches Y, style=X matches Y. Purpose: ..."}}],
  "total_cost": number,
  "total_footprint_sqm": number,
  "fit_satisfaction": {{
    "selected_counts": [{{"category": "requested_category", "count": number, "selected_uids": ["..."], "reason": "Why these selected UIDs satisfy this requested category"}}],
    "substitutions": [{{"requested_category": "requested_category", "selected_uid": "...", "reason": "Why this UID is an acceptable substitute"}}]
  }},
  "selection_strategy": {{
    "summary": "Overall approach and rationale for the selection",
    "higher_budget_additions": "Suggested additions if user has more budget (e.g., upgrade sofa, add accent chairs, premium lighting)",
    "lower_budget_savings": "What to remove or swap for cheaper alternatives if user needs to save money",
    "gaps": "List each selected item where brand/color/style/shape does NOT match user intent, with reason why (e.g. 'rug_15: another brand because the preferred brand has no fitting rug' or 'rug_15: grey instead of green - no green rugs available'). Use an empty string only if ALL selected items match the intent",
    "conflict": "If the user's request contains contradicting goals (e.g. 'more open' but also 'add more furniture'), describe the conflict and what tradeoff was made. Use an empty string if there is no conflict."
  }}
}}"""
    gaps = _gap_slots(shared["slots"])
    if gaps:
        prompt += (
            "\n\nCATALOG GAPS (no eligible product; these requested items are not required):\n"
            + "\n".join(f'- {slot["label"]} ({slot["category"]})' for slot in gaps)
            + "\nName each one in selection_strategy.gaps."
        )
    previous = state.get("selection_validation")
    if previous:
        prompt += (
            f"\n\nPREVIOUS SELECTION (turn {state.get('selection_turns', 0)}) FAILED VALIDATION:\n"
            f"{json.dumps([asset['uid'] for asset in state['selection']['selected_assets']])}\n"
            f"VALIDATION RESULT:\n{json.dumps(previous, default=str)}\n"
            "Fix every error and return the complete corrected selection."
        )
    return prompt


def _next_fit_step(state: VariantState) -> str | None:
    """Fit step for this turn: compact from the start when the room fit estimate warns,
    compact after the first fit failure, capped counts after the next."""
    step = state.get("fit_step")
    previous = state.get("selection_validation")
    if previous is None:
        return "compact" if state["shared"]["room"]["fit_warning"] else None
    if previous.get("fit_failed"):
        return "capped" if step else "compact"
    return step


async def select(state: VariantState, ctx: StageContext) -> VariantState:
    """Run one selection turn: one model call, then the ported selection validator.

    Reads: `shared`, `direction`, `selection_turns` (turns already made),
    `selection` and `selection_validation` from the previous turn as feedback.
    Returns: `selection` (the model's `selected_assets`, one entry per unit, with
    gaps in `selection_strategy.gaps`), `selection_validation` (the legacy
    validator report: `valid: bool`, `errors: list[str]`, ...), `instances`, and
    `fit_step`. Notes the fit step applied with `ctx.run.note(...)`.
    """
    shared = state["shared"]
    fit_step = _next_fit_step(state)
    turn = state.get("selection_turns", 0) + 1
    if fit_step != state.get("fit_step"):
        ctx.run.note(f"selection turn {turn}: fit step {fit_step}", ctx.variant_index)
    intent = _selection_intent(shared["intent"], shared["slots"])
    response = await ctx.generate(Selection, _selection_prompt(state, intent, fit_step))

    candidates = _candidates(shared["pool"])
    by_id = {str(record["asset_id"]): record for record in candidates}
    items = [
        {"asset": by_id.get(asset.uid.strip()) or {"asset_id": asset.uid.strip()}, "quantity": 1,
         "reason": asset.reason, "functional_group": asset.functional_group}
        for asset in response.selected_assets
    ]
    gaps = _gap_slots(shared["slots"])
    validation = validate_selection(
        items=items,
        intent=intent,
        room=shared["room"],
        budget=shared["request"].budget,
        candidates=candidates,
        gaps=[slot["category"] for slot in gaps],
        fit_step=fit_step,
        fit_satisfaction=response.fit_satisfaction.model_dump() if response.fit_satisfaction else None,
        constraint_audit=response.constraint_audit.model_dump(),
    )
    instances = validation.pop("instances")
    validation.pop("feedback")
    selection = response.model_dump()
    gap_text = "; ".join(f"{slot['label']}: no eligible catalog product" for slot in gaps)
    strategy = selection["selection_strategy"]
    strategy["gaps"] = "; ".join(text for text in (strategy["gaps"].strip(), gap_text) if text)
    return {
        "selection": {**selection, "fit_step": fit_step},
        "selection_validation": validation,
        "instances": instances,
        "fit_step": fit_step,
    }


# --- place and correct -------------------------------------------------------


class Placement(BaseModel):
    uid: str
    category: str = ""
    position: list[float]
    rotation: list[float]
    on_top_of: str = Field(
        default="",
        description="UID of the parent asset this sits on. Empty string or omitted for floor/wall/ceiling items.",
    )


class InitialLayout(BaseModel):
    layout_summary: str
    layout: list[Placement]


class Pose(BaseModel):
    uid: str
    x: float
    y: float
    rotation_z: float
    on_top_of: str | None = Field(
        default=None,
        description=(
            "Supporting parent UID. Omit this field to preserve the current support relationship; "
            "use an empty string only to remove it."
        ),
    )


class Correction(BaseModel):
    poses: list[Pose]


def _key(asset: Record) -> str:
    return str(asset.get("instance_key") or asset.get("uid") or "")


def _opening_lines(room: Record) -> list[str]:
    room_area = tuple(room["room_area"])
    lines = [
        format_door_for_prompt(index, door, boundary=room["room_vertices"], room_area=room_area)
        for index, door in enumerate(room["room_doors"])
    ]
    lines += [
        f"- window-{index}: center={window.get('center')}, width={float(window.get('width') or 0):.2f}m, "
        f"depth={float(window.get('depth') or 0):.2f}m"
        for index, window in enumerate(room["room_windows"])
    ]
    return lines


def _path_fit_facts(state: VariantState) -> str:
    preflight = state["selection_validation"]["metrics"].get("layout_preflight") or {}
    return json.dumps({
        "protected_path_zones": preflight.get("protected_path_zones", []),
        "clusters": preflight.get("clusters", []),
    })


def _placement_prompt(state: VariantState) -> str:
    """Legacy initial-layout prompt for a fresh design, without the seed image and asset-feedback decision."""
    shared = state["shared"]
    request, intent, room = shared["request"], shared["intent"], shared["room"]
    room_width, room_depth = request.room_area
    room_type = room["room_type"]
    assets = state["instances"]
    studio = room_type == "studio"
    protected_path_lines = [
        f"- {path.get('id')}: {path.get('axis')}-axis route between "
        f"{' and '.join(path.get('wall_pair') or [])}, "
        f"center={path.get('center')}, width={float(path.get('width') or 0):.2f}m, "
        f"depth={float(path.get('depth') or 0):.2f}m"
        for path in room["protected_paths"]
    ]
    asset_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')}), W×D×H={a.get('width', 0):.2f}×{a.get('depth', 0):.2f}×{a.get('height', 0):.2f}m"
        f", mount_type={a.get('mount_type') or 'unknown'}, features={json.dumps(a.get('features') or [])}"
        for a in assets
    ]
    wall_aligned_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')})"
        for a in assets
        if is_wall_aligned_asset(a.get("category", ""), _key(a)) and not (studio and is_freestanding_studio_media_support(a))
    ]
    wall_mounted_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')})"
        for a in assets
        if placement_mode_for_asset(a) == "wall_mounted" or is_wall_mounted_asset(a.get("category", ""), _key(a))
    ]
    ceiling_mounted_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')})"
        for a in assets
        if placement_mode_for_asset(a) == "ceiling_mounted" or is_ceiling_mounted_asset(a.get("category", ""), _key(a))
    ]
    floor_lamp_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')}): place beside a reading or primary seat"
        for a in assets
        if is_floor_lamp_asset(a.get("category", ""), _key(a))
    ]
    table_lamp_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')}): set on_top_of a side table, nightstand, desk, or console"
        for a in assets
        if is_table_lamp_asset(a.get("category", ""), _key(a))
    ]
    tabletop_display_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')}): set on_top_of a table, shelf, console, or other valid support"
        for a in assets
        if placement_mode_for_asset(a) == "tabletop" and not is_table_lamp_asset(a.get("category", ""), _key(a))
    ]
    opening_lines = _opening_lines(room)
    example_uid = _key(assets[0])
    example_cat = assets[0].get("category", "furniture")
    none = "None"
    return f"""Generate a 2D furniture layout for this room.

INTENT: {intent_prompt_text(intent, request.user_intent)}
{format_intent_packet_for_prompt(intent)}
{format_room_facts_for_prompt(room["facts"])}
{format_feasibility_digest_for_prompt(room["digest"])}

{coordinate_system_block(room_width, room_depth)}

USABLE FLOOR BOUNDARY (all furniture footprints must stay inside this polygon):
{json.dumps(room["room_vertices"])}

{build_rules_block(room_type=room_type)}

DINING PLACEMENT FACTS (table-local axes; chair fronts face the occupied edge):
{json.dumps(dining_placement_facts(assets))}
Center offsets include half the chair depth, not just half the table. Reserve the full
table/chairs/pull-out envelope before choosing the table center. Front/back edges run
along table width; left/right edges run along table depth. Rotate these local axes with
the table. These are coarse seating options, not proof of clearance from other groups.
For a compact Studio, establish dining pull-out and bed access together before placing
the sofa and media. Try adjacent dining edges when opposing seats would consume a bed
or sofa service band. Do not use required pull-out space as the media-console zone.

MEDIA COMFORT FACTS (product heuristics; plan each requested viewer independently):
{json.dumps(comfort_placement_facts(assets, room_type))}

OPENINGS (doors / windows — respect doorway clear zone):
{chr(10).join(opening_lines) if opening_lines else none}

WALL-MOUNT SPANS AT MOUNTING HEIGHT (actual segments, after openings; empty center intervals cannot fit this item):
{json.dumps(wall_mount_placement_facts({}, assets, room["room_vertices"], room["room_doors"], room["room_windows"], (room_width, room_depth)))}
Place wall artwork only within a fitting center interval. These spans do not certify collisions with other assets.

PROTECTED PATHS (keep clear except rugs/runners):
{chr(10).join(protected_path_lines) if protected_path_lines else none}

SELECTION-STAGE PHYSICAL FIT FACTS:
{_path_fit_facts(state)}
These describe feasible footprints, not chosen poses. For a living-seating cluster, `as_dimensioned` places its listed width across the zone and depth along it; `quarter_turn` swaps those axes. Plan the complete group in a fitting orientation before calculating coordinates, while also preserving its media axis and all openings.

ASSETS ({len(assets)} items, ALL must be placed):
{chr(10).join(asset_lines)}

WALL-ALIGNED in this set (flush to a wall, never diagonal):
{chr(10).join(wall_aligned_lines) if wall_aligned_lines else none}

WALL-MOUNTED in this set:
{chr(10).join(wall_mounted_lines) if wall_mounted_lines else none}

CEILING-MOUNTED in this set:
{chr(10).join(ceiling_mounted_lines) if ceiling_mounted_lines else none}

FLOOR LAMPS in this set:
{chr(10).join(floor_lamp_lines) if floor_lamp_lines else none}

TABLE LAMPS in this set:
{chr(10).join(table_lamp_lines) if table_lamp_lines else none}

TABLETOP DISPLAY OBJECTS in this set:
{chr(10).join(tabletop_display_lines) if tabletop_display_lines else none}

NODE-LOCAL:
- Respect mount_type: wall_secured items stand on the floor against a wall; wall_mounted and ceiling_mounted items need the stated mounting surface. Unknown mount_type provides no additional constraint.
- Use each uid EXACTLY as provided.
- Plan circulation before placing the seating group: connect each usable doorway to the room's functional zones with the required clear walking width. Clearing only the door swing is insufficient; do not park a chair or cabinet just beyond it across the entry route, even when PROTECTED PATHS is empty.
- If a TV and sofa are selected, plan their shared viewing axis together, facing each other across usable space. Do not assign them independently to convenient perpendicular walls, and do not place the TV across a window to solve another clearance problem.
- If a sofa and coffee table are selected, place them as one reachable group. Calculate their facing edge-to-edge gap from the rotated footprints, not center distance; target the middle of the specified usable range, then place secondary seating outside both that gap and the entry route.
- Before returning, check the complete group for overlaps and walking access. If a seat blocks entry, reposition that seat within the group instead of separating the sofa and coffee table to create a passage between them.

OUTPUT JSON:
{{
  "layout_summary": "Briefly identify the clear entry route and, where applicable, the shared sofa/TV viewing axis and measured sofa/table edge gap in meters. Describe the returned poses, not the intended design.",
  "layout": [{{"uid": "{example_uid}", "category": "{example_cat}", "position": [x, y, z], "rotation": [0, 0, radians], "on_top_of": ""}}]
}}

The layout array must contain EXACTLY {len(assets)} items — one per asset, no exceptions.
Order bottom-to-top: rugs → floor furniture → stacked items → wall-mounted."""


def _measure(state: VariantState, layout: Record) -> VariantState:
    """Normalize and analyze a layout the way legacy fresh designs do."""
    room = state["shared"]["room"]
    assets = state["instances"]
    layout = normalize_layout(layout, assets, room["room_vertices"], room_windows=None, rehome_service_items=False)
    issues = analyze_layout(
        layout,
        assets,
        room["room_vertices"],
        room["room_doors"],
        tuple(room["room_area"]),
        room_windows=room["room_windows"],
        protected_paths=room["protected_paths"],
        room_type=room["room_type"],
    )
    levels = findings_by_level(issues)

    def flatten(names: tuple[str, ...]) -> list[Record]:
        return [{"level": name, "issue": key, "finding": finding}
                for name in names for key, found in levels[name].items() for finding in found]

    return {
        "layout": layout,
        "issues": issues,
        "findings": flatten(("P0", "P1", "P2")),
        "blocking_findings": flatten(("P0", "P1", "critical_p2")),
    }


async def place(state: VariantState, ctx: StageContext) -> VariantState:
    """Place every selected instance with one model call, then normalize and analyze.

    Reads: `shared`, `selection`.
    Returns: `layout`, `findings`, and `blocking_findings` (P0, P1, and critical P2).
    A response that does not place every instance exactly once with finite vectors
    gets one retry with the error; a second malformed response fails the variant.
    """
    prompt = _placement_prompt(state)
    room_area = tuple(state["shared"]["room"]["room_area"])

    async def propose(prompt: str) -> Record:
        response = await ctx.generate(InitialLayout, prompt, system=LAYOUT_SYSTEM_INSTRUCTION)
        return layout_from_placements([p.model_dump() for p in response.layout], state["instances"], room_area)

    try:
        layout = await propose(prompt)
    except ValueError as exc:
        layout = await propose(f"{prompt}\n\nYOUR PREVIOUS LAYOUT WAS REJECTED: {exc}. Return the complete layout again.")
    return _measure(state, layout)


def _correction_prompt(state: VariantState) -> str:
    """Legacy layout_fix prompt for a fresh design, answered with poses instead of edit tools."""
    shared = state["shared"]
    request, room = shared["request"], shared["room"]
    room_width, room_depth = request.room_area
    room_area = (room_width, room_depth)
    room_type = room["room_type"]
    layout, assets, issues = state["layout"], state["instances"], state["issues"]
    tiers = tuple(tier for tier, keys in ISSUE_KEYS_BY_TIER.items() if any(issues.get(key) for key in keys)) or ("P0", "P1", "P2")
    rows = [
        {"uid": uid, "x": layout[uid]["position"][0], "y": layout[uid]["position"][1],
         "rotation_z": layout[uid]["rotation"][2], "on_top_of": str(layout[uid].get("on_top_of") or "")}
        for uid in sorted(layout)
    ]
    asset_lines = [
        f"- {_key(a)} ({a.get('category', 'unknown')}), W×D×H={float(a.get('width') or 0):.2f}×"
        f"{float(a.get('depth') or 0):.2f}×{float(a.get('height') or 0):.2f}m"
        f", mount_type={a.get('mount_type') or 'unknown'}, features={json.dumps(a.get('features') or [])}"
        for a in assets
    ]
    proposal = state.get("correction_proposals", 0) + 1
    return f"""Solve the measurable placement violations by reasoning about the complete room layout.

This is not an iteration edit.
ORIGINAL DESIGN REQUEST:
{request.user_intent}

Preserve the requested functions throughout repair. When changing a group's
zone or facing axis, choose the TV/support poses with its intended viewers (sitting,
bed or both). Do not require a bed-only TV to face the sofa. Keep the console front accessible and chair backs outside the
viewing line; collision-free footprints alone do not prove either condition.

{coordinate_system_block(room_width, room_depth)}

ROOM BOUNDARY VERTICES:
{json.dumps(room["room_vertices"])}

{build_rules_block(tiers=tiers, include_definitions=True, room_type=room_type)}

DOORS:
{chr(10).join(format_door_for_prompt(i, door, boundary=room["room_vertices"], room_area=room_area) for i, door in enumerate(room["room_doors"])) or "None"}

OPENINGS:
{chr(10).join(_opening_lines(room)) or "None"}

WINDOW GEOMETRY:
{json.dumps(room["room_windows"]) if room["room_windows"] else "None"}

PROTECTED CIRCULATION PATHS:
{json.dumps(room["protected_paths"]) if room["protected_paths"] else "None"}

SELECTION-STAGE PHYSICAL FIT FACTS:
{_path_fit_facts(state)}
These are exact feasibility facts, not a design recommendation. For a living-seating cluster, `as_dimensioned` means the listed arrangement width runs across the named zone and its depth runs along that zone; `quarter_turn` swaps those axes. Use these facts to size a complete sofa/table/accent-seat group within a zone. Choose its facing axis together with the selected TV/support at a usable wall or intentional Studio divider serving its requested viewers; a feasible seating footprint alone does not establish a usable media arrangement.

ACTIVE ASSETS ({len(layout)}):
{chr(10).join(asset_lines) or "None"}

CURRENT NORMALIZED FULL-CANDIDATE ROWS:
{json.dumps(rows)}

CURRENT ASSET BOUNDS:
{format_layout(layout, assets)}

MEDIA COMFORT FACTS (product heuristics; preserve every requested viewing target):
{json.dumps(comfort_placement_facts(assets, room_type))}

DINING PLACEMENT FACTS (table-local axes; chair fronts face the occupied edge):
{json.dumps(dining_placement_facts(assets))}
CURRENT CHAIR/TABLE GEOMETRY (negative signed gap means penetration):
{json.dumps(dining_chair_table_facts(layout, assets))}
These translations hold the table and chair yaw fixed; they solve only the edge gap.
Check tabletop aim, occupied edge width and all pull-out/room constraints before choosing them.
Center offsets include half the chair depth. Front/back edges run along table width;
left/right edges run along table depth. Rotate these local axes with the table.
When separating overlapping chairs from a table, reserve their full pull-out envelope
at the same time. If it hits a wall, doorway, bed-access strip or furniture, move/rotate
the complete editable dining group into a usable span. Do not trade overlaps for blocked
chair pull-out or move frozen furniture. Envelopes alone do not certify scene clearance.
Chair findings identify their table/group_members, blockers and clearance_shortfall_m.
Repair coupled conflicts together: check both chair backs after moving a table or console,
and use adjacent occupied edges for two diners when opposing edges cannot clear.
Avoid alternating a chair between the same two known blockers. For equal issue counts,
the solver retains lower overlap penetration, then lower total access shortfall and worst/total viewer-distance
excess; partial progress does not make a hard failure valid.
Bed/dining access findings include center_bounds_for_room_bbox: allowed center
intervals for the item AND its service band at its current yaw, using
the room bounding box. A center outside those intervals cannot have usable access.
Irregular boundaries, doors and other groups still need their separate checks.
For no_usable_side, inspect both side records: a tiny headboard overrun can invalidate
both full side strips even when there is no furniture blocker. Correct that boundary
offset before rearranging other groups; only one usable bed side is required.

EXACT VALIDATION FINDINGS:
{format_issues(issues)}

EXACT OVERLAP SEPARATIONS (world XY; each option separates only the named pair):
{json.dumps(overlap_separation_facts(layout, assets, issues.get("overlaps", [])))}
Choose an editable side and direction that also preserves all service regions.

WALL-MOUNT SPANS AT CURRENT HEIGHT (actual wall segments; empty center intervals cannot fit this item):
{json.dumps(wall_mount_placement_facts(layout, assets, room["room_vertices"], room["room_doors"], room["room_windows"], room_area))}
Choose a fitting interval on another wall when the current span is narrower than the item.
Keep the asset's full height inside the room; these spans do not certify furniture/artwork collisions.

CONTROLLED-EDIT PROTOCOL:
- Preserve mounting requirements: wall_secured items remain floor-standing against a wall; wall_mounted and ceiling_mounted items remain on their mounting surface. Unknown mount_type provides no additional constraint.
- This is repair proposal {proposal}. Return the poses of every asset that must move as one proposal. Every pose must include uid, x, y, rotation_z, and on_top_of. Do not retranscribe unchanged assets.
- A relational gap correction must also keep each resulting footprint inside the
  room and preserve its valid wall gap. If moving the anchor would cross its wall,
  move the editable neighboring table instead; do not fix one gap by creating a
  boundary violation.
- The server applies the poses to the normalized current draft. Every unmentioned asset keeps its exact pose and support relationship.
- Before the first candidate, make a relational zone plan: use protected paths and openings to divide the usable floor into connected zones, assign each complete functional group to one zone, choose its anchor/facing axis, and only then calculate coordinates.
- A protected path that spans the room is a divider, not spare floor. Do not split a conversation, dining, or work group across it. A media focal wall may be across the path from seating when every physical asset stays outside the path; never put a coffee table or chair in the path merely to align seating with media. If an inseparable group does not fit on one side in its current orientation, rotate or re-anchor the whole group.
- The server includes supported children in their parent's translation, canonicalizes rotations, support relationships, rugs, wall mounts, and z values, but does not silently relocate floor furniture for window or service-item preferences.
- Apply world-axis translation intervals exactly; do not re-estimate clearances from nominal width/depth after rotation.
- When a protected-path blocker belongs to a functional group, move the sofa, table, and conversation chairs together, or rotate/re-anchor that group before clearing the path. Moving only the blocker is acceptable only when it preserves every measured relationship.
- A separate best candidate is retained server-side, so explore coordinated/global corrections when a local nudge cannot satisfy competing constraints.
- You may reposition or rotate any number of assets and may make moves larger than 1m when the whole-room solution requires it.
- Optimize lexicographically: hard geometry first, then critical functional issues, then remaining functional issues.
- Solving terminates when P0, P1, and critical P2 findings are clear. Noncritical P2 findings are optimization guidance and must not trigger unrelated movement after the blocking findings are clear.
- Use numeric issue measurements and normalized coordinates as authoritative facts.
- Prioritize the exact remaining blocking findings. When a measured translation or gap correction can resolve a blocker without creating another, apply that correction before changing the group's zone or orientation. Re-anchor the group only when local corrections conflict with other blocking findings.
- Poses describe the chosen edit only.

Submit the controlled edit now as JSON: {{"poses": [{{"uid": "...", "x": number, "y": number, "rotation_z": number, "on_top_of": ""}}]}}"""


async def correct(state: VariantState, ctx: StageContext) -> VariantState:
    """Make one correction proposal: send findings and poses, apply, normalize, analyze.

    Reads: `shared`, `selection`, `layout`, `findings`, `blocking_findings`,
    `correction_proposals` (proposals already made).
    Returns: `layout`, `findings`, and `blocking_findings` for the best candidate
    so far (fewest physical findings, then critical function, then the rest).
    """
    response = await ctx.generate(Correction, _correction_prompt(state), system=LAYOUT_SYSTEM_INSTRUCTION)
    layout, assets = state["layout"], state["instances"]
    for pose in response.poses:
        if pose.uid not in layout or not all(math.isfinite(value) for value in (pose.x, pose.y, pose.rotation_z)):
            continue
        layout = move_asset_with_supports(layout, pose.uid, pose.x, pose.y, assets)
        placement = {**layout[pose.uid], "rotation": [0.0, 0.0, pose.rotation_z]}
        if pose.on_top_of == "":
            placement.pop("on_top_of", None)
        elif pose.on_top_of in layout and pose.on_top_of != pose.uid:
            placement["on_top_of"] = pose.on_top_of
        layout = {**layout, pose.uid: placement}
    candidate = _measure(state, layout)
    if layout_issue_score(candidate["issues"]) < layout_issue_score(state["issues"]):
        return candidate
    return {}


# --- validate ----------------------------------------------------------------


async def validate(state: VariantState, ctx: StageContext) -> VariantState:
    """Final check: each selected instance placed exactly once, no blocking finding.

    Reads: `shared`, `selection`, `layout`, `blocking_findings`.
    Returns on pass: `render_manifest` (`contracts.render_manifest(request, instances)`),
    `selected_assets` (`contracts.selected_asset` per instance), and `total_cost`
    (purchasable prices only). Returns on fail: `validation_errors` and no
    `render_manifest`.
    """
    shared = state["shared"]
    room, layout, assets = shared["room"], state["layout"], state["instances"]
    check = final_layout_check(
        layout,
        assets,
        room_type=room["room_type"],
        room_area=room["room_area"],
        room_vertices=room["room_vertices"],
        room_doors=room["room_doors"],
        room_windows=room["room_windows"],
        protected_paths=room["protected_paths"],
    )
    if not check["valid"]:
        return {"validation_errors": check["errors"]}
    instances: list[contracts.Instance] = [
        {
            "instance_key": _key(asset),
            "category": normalize_category(asset.get("category")),
            "position": layout[_key(asset)]["position"],
            "rotation": layout[_key(asset)]["rotation"],
            "placement_mode": placement_mode_for_asset(asset),
            "asset": asset,
        }
        for asset in assets
    ]
    return {
        "render_manifest": contracts.render_manifest(shared["request"], instances),
        "selected_assets": [contracts.selected_asset(instance) for instance in instances],
        # Validator total: purchasable prices only, TVs excluded.
        "total_cost": float(state["selection_validation"]["metrics"]["total_cost"]),
    }
