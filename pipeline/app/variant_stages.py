"""Variant stages: run once per variant inside the variant subgraph.

Each stage is `async def stage(state: VariantState, ctx: StageContext) -> VariantState`.
It reads variant-state keys and returns a partial update with only the keys it
owns. `state["shared"]` holds the request, intent, room, and slots; it is shared
by all variants, so copy a record before changing it. `state["pool"]` holds this
variant's candidates per slot id, dealt by the shared `rank` stage defined here.
`state["variant_index"]` and `state["direction"]` identify the variant.

The graph owns the loops and their bounds (app.graph): it counts
`selection_turns`, `correction_proposals`, and `reselections`. It repeats select
until `selection_validation["valid"]` or 4 turns in all; repeats correct while
`blocking_findings` is non-empty and the last proposal improved (no
`correction_stalled`), up to 3 proposals per layout; runs refine only when no
blocking finding remains; and after a failed validate goes back to select once.
A stage that raises fails only its own variant (reason `model_call_failed` for
ModelCallError, otherwise `variant_error`). Correct and refine catch their own
ModelCallError instead: a layout already exists, so they keep it and go on.

`ctx` is the same as for shared stages (see app.shared_stages); model calls made
through `ctx.generate` and Jev calls made through `ctx.ask` are recorded under
this stage and variant.

Prompts port the legacy fresh-design text (livinit_pipeline
src/nodes/asset_selection/agent.py, src/nodes/layout_generation/initial_flow.py,
src/nodes/layout_fix.py, src/nodes/layout_generation/refine_flow.py). Tool-call
wording becomes one JSON response, and the revision, seed-image, preview-image,
asset-feedback, and owned-asset branches are dropped.
"""

from __future__ import annotations

import asyncio
import itertools
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
from app.rules.geometry.generation import generate_deterministic_layout_with_report
from app.rules.layout.analysis import analyze_layout, final_layout_check, findings_by_level
from app.rules.layout.bedroom import bedroom_layout_measurements
from app.rules.layout.cleanup import (
    clear_protected_paths,
    run_deterministic_living_dining_cleanup,
    run_deterministic_p0_cleanup,
    satisfy_sofa_table_gaps,
)
from app.rules.layout.comfort import comfort_layout_measurements, comfort_placement_facts
from app.rules.layout.constants import ISSUE_KEYS_BY_TIER
from app.rules.layout.dining import dining_chair_table_facts, dining_layout_measurements, dining_placement_facts
from app.rules.layout.formatting import format_issues, format_layout
from app.rules.layout.metrics import layout_issue_score
from app.rules.layout.normalization import layout_from_placements, move_asset_with_supports, normalize_layout
from app.rules.layout.solver import solve_layout
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
from app.rules.planner.seed_guidance import build_seed_guidance
from app.rules.planner.taxonomy import normalize_category
from app.rules.selection.catalog import _asset_matches_brand_preferences, _price_constraint_category_matches, catalog_asset
from app.rules.selection.constants import BUDGET_EXCLUDED_CATEGORIES, BUDGET_FLEX_PCT, FIT_ANCHOR_SEATING
from app.rules.selection.fit import fit_step_guidance
from app.rules.selection.validation import is_decor_plant, requires_spacious_perimeter_storage, validate_selection
from app.run import ModelCallError, StageContext, describe
from app.shared_stages import KEEP, LIMIT_KEYS, RANKED_KEEP

if TYPE_CHECKING:
    from app.graph import PipelineState, Shared, VariantState

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


# --- rank --------------------------------------------------------------------

# A slot needs this many eligible products per variant to be dealt; with fewer, the variants share it.
MIN_DEAL = 2
_RANK_QUESTION = "Is this product a good choice for the slot, given the brief and the variant direction? Product: {}"


def _brief(shared: Shared | PipelineState, variant_direction: str) -> Record:
    """What Jev reads about the request and one variant."""
    intent = shared["intent"]
    return {
        "request": intent.get("normalized_prompt") or shared["request"].user_intent,
        "style_hints": intent.get("style_hints") or [],
        "room_type": shared["room"]["room_type"],
        "variant_direction": variant_direction.strip() or "none",
    }


def _product_text(record: Record) -> str:
    """A product's prepared fields as one line for a Jev question."""
    asset = catalog_asset(record)
    parts = [f"{record.get('title')} ({normalize_category(record.get('category'))})"]
    parts += [
        f"{field}: {', '.join(value) if isinstance(value, list) else value}"
        for field in ("colors", "materials", "styles")
        if (value := record.get(field))
    ]
    parts.append(f"size: {asset['width']:.2f} x {asset['depth']:.2f} x {asset['height']:.2f} m")
    if record.get("price") is not None:
        parts.append(f"price: ${float(record['price']):.0f}")
    if record.get("description"):
        parts.append(f"description: {str(record['description'])[:300]}")
    return "; ".join(parts)


async def rank(state: PipelineState, ctx: StageContext) -> PipelineState:
    """Rank each slot's candidates for every variant direction, then deal them so the variants get mostly different products.

    A shared stage (see app.shared_stages) that runs after retrieve; it lives here
    beside the variant brief and directions.
    Reads: `request`, `intent`, `room`, `slots`, `pool`.
    Returns: `pools`, each variant's candidates per slot id, by variant index; the
    graph sends each variant its own as `pool`. Each record gets `shared`: true
    when another variant's pools also hold it, except products from slots too
    small to deal. With Jev rank on, one Jev request per slot and direction asks
    one yes/no question per candidate, recorded under that direction's variant
    index; the direction's order puts preferred brands first, then yes
    probability, and its list size is RANKED_KEEP. With rank off, or when a
    request fails, the direction keeps the shared order and KEEP. Slots are dealt
    in plan order (`_deal`) with `ctx.run.product_reuse_rate`. A slot with fewer
    than MIN_DEAL products per variant is not dealt: each variant gets its own
    list, and the run notes it.
    """
    slots, pool = state["slots"], state["pool"]
    count = ctx.run.variant_count
    briefs = [_brief(state, direction(index, state["request"].room_type)) for index in range(count)]

    async def rank_slot(slot: Record, index: int) -> list[Record] | None:
        """This direction's order of the slot's candidates, or None without Jev answers."""
        rows = pool[slot["id"]]
        if not rows or "rank" not in ctx.run.jev_uses:
            return None
        questions = {f"product_{n}": _RANK_QUESTION.format(_product_text(row)) for n, row in enumerate(rows)}
        answers = await StageContext(ctx.run, ctx.stage, index).ask("rank", {**briefs[index], "slot": slot["label"]}, questions)
        if answers is None:
            return None
        brands = slot["preferred_brands"]
        order = sorted(
            range(len(rows)),
            key=lambda n: (bool(brands) and not _asset_matches_brand_preferences(rows[n], brands), -answers[f"product_{n}"]),
        )
        return [rows[n] for n in order]

    ranked = await asyncio.gather(*(rank_slot(slot, index) for slot in slots for index in range(count)))
    pools: list[dict[str, list[Record]]] = [{} for _ in range(count)]
    holders: dict[str, set[int]] = {}  # asset id -> the variants whose pools hold it, across slots
    exclusive: dict[str, int] = {}  # asset id -> the one variant whose pools may hold it
    exempt: set[str] = set()  # asset ids from slots too small to deal
    for n, slot in enumerate(slots):
        rows = pool[slot["id"]]
        orders = ranked[n * count : (n + 1) * count]
        rankings = [rows if order is None else order for order in orders]
        sizes = [(KEEP if order is None else RANKED_KEEP)[slot["kind"]] for order in orders]
        if len(rows) < MIN_DEAL * count:
            hands = [ranking[:size] for ranking, size in zip(rankings, sizes, strict=True)]
            exempt |= {str(row["asset_id"]) for row in rows}
            if rows:
                ctx.run.note(f"slot {slot['id']} shared across variants: only {len(rows)} eligible products")
        else:
            hands = _deal(rankings, sizes, ctx.run.product_reuse_rate, holders, exclusive)
        for index, hand in enumerate(hands):
            pools[index][slot["id"]] = hand
            for row in hand:
                holders.setdefault(str(row["asset_id"]), set()).add(index)

    def mark(row: Record) -> Record:
        uid = str(row["asset_id"])
        return {**row, "shared": len(holders[uid]) > 1 and uid not in exempt}

    return {"pools": [{slot_id: [mark(row) for row in rows] for slot_id, rows in hands.items()} for hands in pools]}


def _deal(
    rankings: list[list[Record]], sizes: list[int], rate: float, holders: dict[str, set[int]], exclusive: dict[str, int]
) -> list[list[Record]]:
    """Deal one slot's ranked candidates to the variants. Updates `holders` and `exclusive`.

    First, in rounds, each variant takes its highest-ranked candidate that no other
    variant holds, as its exclusive, until it has ceil(size * (1 - rate)) of them;
    round r starts at variant r. Then each variant fills up to its size with its
    highest-ranked remaining candidates that are not another variant's exclusive;
    these may be shared.
    """
    count = len(rankings)
    hands: list[list[Record]] = [[] for _ in rankings]

    def take(index: int, own: bool) -> bool:
        held = {str(row["asset_id"]) for row in hands[index]}
        for row in rankings[index]:
            uid = str(row["asset_id"])
            if uid not in held and (holders.get(uid, set()) <= {index} if own else exclusive.get(uid, index) == index):
                hands[index].append(row)
                holders.setdefault(uid, set()).add(index)
                if own:
                    exclusive[uid] = index
                return True
        return False

    own_sizes = [math.ceil(round(size * (1 - rate), 9)) for size in sizes]
    for first in itertools.count():
        dealt = False
        for index in ((first + turn) % count for turn in range(count)):
            if len(hands[index]) < own_sizes[index] and take(index, own=True):
                dealt = True
        if not dealt:
            break
    for index in range(count):
        while len(hands[index]) < sizes[index] and take(index, own=False):
            pass
    return hands


# --- select ------------------------------------------------------------------

# Legacy validate_selection tool arguments (src/nodes/asset_selection/contracts.py)
# as one JSON response, trimmed to the selection and its gap notes. Code derives
# fit_satisfaction, and code and Jev build the constraint audit.


class SelectedAsset(BaseModel):
    uid: str
    functional_group: Literal["sleeping", "sitting", "dining"] | None = Field(
        default=None,
        description="For studio rugs, side tables and ceiling lights, the functional group this accessory serves. Bedside tables serve sleeping.",
    )


class Selection(BaseModel):
    selected_assets: list[SelectedAsset]
    gaps: str


_CSV_HEADER = "uid,category,name,price,width,depth,height,brand,color,style,material,asset_description,mount_type,features,is_decor_item,is_placeholder,shared"


def _csv_text(value: Any, max_len: int | None = None) -> str:
    text = ", ".join(value) if isinstance(value, list) else str(value or "")
    return f'"{text[:max_len].replace(chr(34), "")}"'


def _csv_row(asset: Record) -> str:
    """Legacy _assets_to_csv row from a prepared record, plus rank's `shared` mark. Prepared records have no shape or popularity."""
    return (
        f"{asset['uid']},{normalize_category(asset.get('category'))},{_csv_text(asset.get('title'))},"
        f"{asset['price']:.2f},{asset['width']:.3f},{asset['depth']:.3f},{asset['height']:.3f},"
        f"{_csv_text(asset.get('brand'))},{_csv_text(asset.get('colors'))},{_csv_text(asset.get('styles'))},"
        f"{_csv_text(asset.get('materials'))},{_csv_text(asset.get('description'), 120)},"
        f"{_csv_text(asset.get('mount_type'))},{_csv_text(asset.get('features'))},"
        f"{'true' if asset['is_decor_item'] else 'false'},false,{'yes' if asset.get('shared') else 'no'}"
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


def _selection_prompt(
    state: VariantState, intent: Record, fit_step: str | None, placement_feedback: str | None, reuse_rate: float
) -> str:
    """Legacy _build_selection_prompt for a fresh design, plus the reuse limit, slot gaps,
    placement feedback after a failed layout, and the previous turn's feedback."""
    shared = state["shared"]
    request, room = shared["request"], shared["room"]
    room_width, room_depth = request.room_area
    budget = request.budget
    room_type = room["room_type"]
    digest = room["digest"]
    furniture_area = float(room["furniture_area_sqm"])
    candidates = [{**catalog_asset(record), "uid": str(record["asset_id"])} for record in _candidates(state["pool"])]
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
    reuse_guidance = (
        f"- shared=yes marks products the other variants may also use. At most {reuse_rate:.0%} of your distinct products may be shared ones.\n"
        if reuse_rate < 1 else ""
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
{_catalog_block(shared["slots"], state["pool"])}

STUDIO TV SIZE / VIEWING RANGE FACTS (metres; choose for the requested bed/sofa viewers):
{json.dumps(media_selection_facts) if media_selection_facts else "Not applicable"}
{f"For bed/shared viewing across opposing walls, the shorter room span minus a 0.60m combined eye/screen inset gives an approximate {opposed_wall_reference:.2f}m planning reference. Catalog screens whose preferred range reaches it: {wall_viewing_candidates}. Prefer these with a fitting console for a wall-backed arrangement; smaller screens need an intentionally closer arrangement. This is guidance, not a pose or feasibility guarantee." if studio and media_selection_facts else ""}

CONSTRAINTS:
{reuse_guidance}- Use features as supported product capabilities. mount_type=wall_secured requires floor placement against a wall; wall_mounted and ceiling_mounted require those mounting surfaces. An empty mount_type is unknown.
- {required_role_guidance}
- {rug_guidance}
{plant_guidance}- Budget is a spending cap, not a target: total cost must stay at or under ${budget * (1 + BUDGET_FLEX_PCT):.2f} (${budget:.2f} + 10% flex). There is no minimum spend — never inflate item prices to use the budget up.
- Furnish the room: total footprint (width x depth x 2 per asset, rugs excluded) must be at least {footprint_floor:.2f} sqm and at most {furniture_area:.2f} sqm. The comfortable load of {comfortable_load:.2f} sqm is an upper reference, not a target. Once the requested functions and density floor are satisfied, do not add pieces merely to approach it.
{perimeter_storage_guidance}- Physical layout preflight must pass: rugs must fit the room; every tabletop asset must fit an eligible selected support surface; each selected TV must fit a selected media support; desk/dining clusters must fit with their chairs; and tiny rooms must not receive extra seating or floor lamps.
- {count_guidance_constraint}
- Assets with is_decor_item=true are non-shoppable: their zero budget contribution is bookkeeping, not a retail price. is_placeholder=true identifies temporary furniture for layout evaluation; a verified wardrobe placeholder can satisfy clothing storage, but cannot be purchased or count as spending more budget. Other non-shoppable finishing accents remain optional apart from any fresh-design plant requirement stated above. Never describe these items as free products.
- TVs are normal catalog assets. Treat a TV and its media support as one pair: select neither, or select both a TV and a tv_stand/media_unit/media_console (a console_table is also a valid support). The TV must fit: width <= support width - 0.07m and depth <= max(0.08m, support depth - 0.07m). A TV without a fitting support, or a media support without a TV, fails validation; the server will not add, remove, or replace either asset.
- TVs are non-sellable: exclude TV prices from every budget total while still including the TV UID in selected_assets and the layout.
- You MUST return the exact UID list as selected_assets (one entry per unit); the accepted response is the final result

STRATEGY:
- Functional completeness over spend: satisfy the user's requested functions with a coherent set, then stop. Unspent budget is acceptable; never add an item solely because budget remains.
- The {anchor_name}/anchor piece sets the style direction for all other picks
- Primary goal: aesthetic coherence — select assets whose brand, color, style, shape, and description match the intent
- Treat intent-packet brand constraints as strong preferences, not hard requirements. For each needed category, use the preferred brand when a catalog option also satisfies category, fit, budget, and every required attribute. If none does, select the best eligible option from another brand and identify that category-level fallback in gaps. Never sacrifice a required role, physical fit, budget, or explicit non-brand attribute to preserve the brand preference.
- When multiple assets share a category, pick the best style match at a moderate price; reach for premium versions only after every furnishing role the room needs is covered
- Treat every optional piece as a design decision: include it only when it serves a clear requested function or improves the composition in a specific way. {optional_piece_guidance}
- If the requested set is below the density floor, prefer one or two substantial, useful additions over several small filler pieces.
- Small room → prioritize essentials; do not add optional extras beyond the room-scale count caps
- Duplicates are allowed only when the requested or justified count calls for multiples (e.g. 2x accent_chair_5 for a requested pair). Seating multiples must repeat the same UID as a matched set unless the user explicitly asks for variety; for other categories only mix different UIDs if their style, color, and shape are clearly compatible.
- Honor user-stated counts and non-optional inferred count limits. Optional inferred counts are suggestions, not user requests. Never duplicate a category or add a new category purely to burn budget.

PROCESS:
0. Before selecting, estimate total footprint and cost from the catalog CSV. {selection_size_guidance} while staying under the budget cap. Do not drop coherent requested furniture solely to satisfy budget.
1. Select the {anchor_name} first, then build remaining selection around its style
2. Before every response, check the proposed exact UID set against every user-stated exact count and every required style, material, color, coordination, and attribute constraint, and replace any violating asset. Brand constraints are advisory.
3. {invalid_fix_guidance}
4. Include the complete final payload in every response. When validation succeeds, that accepted response ends selection; there is no later formatting response.

OUTPUT JSON:
{{
  "selected_assets": [{{"uid": "..."}}],
  "gaps": "List each selected item where brand/color/style/shape does NOT match user intent, with reason why (e.g. 'rug_15: another brand because the preferred brand has no fitting rug' or 'rug_15: grey instead of green - no green rugs available'). Use an empty string only if ALL selected items match the intent"
}}"""
    gaps = _gap_slots(shared["slots"])
    if gaps:
        prompt += (
            "\n\nCATALOG GAPS (no eligible product; these requested items are not required):\n"
            + "\n".join(f'- {slot["label"]} ({slot["category"]})' for slot in gaps)
            + "\nName each one in gaps."
        )
    if placement_feedback:
        prompt += f"\n\n{placement_feedback}"
    previous = state.get("selection_validation")
    if previous and not previous.get("valid"):
        prompt += (
            f"\n\nPREVIOUS SELECTION (turn {state.get('selection_turns', 0)}) FAILED VALIDATION:\n"
            f"{json.dumps([asset['uid'] for asset in state['selection']['selected_assets']])}\n"
            f"VALIDATION RESULT:\n{json.dumps(previous, default=str)}\n"
            "Fix every error and return the complete corrected selection."
        )
    return prompt


def _named_items(findings: list[Record], keys: set[str]) -> list[str]:
    """The instance keys that these findings name anywhere in their values."""
    named: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str) and value in keys:
            named.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for finding in findings:
        walk(finding["finding"])
    return sorted(named)


def _placement_feedback(state: VariantState) -> str:
    """Reselection text after a failed layout: the placed selection, the final check
    errors, and the items the remaining blocking findings name."""
    instances = {_key(asset): asset for asset in state["instances"]}
    named = list(dict.fromkeys(f"{instances[key]['asset_id']} ({instances[key]['category']})"
                               for key in _named_items(state.get("blocking_findings", []), set(instances))))
    return (
        "PREVIOUS SELECTION PASSED VALIDATION, BUT ITS LAYOUT FAILED THE FINAL CHECK:\n"
        f"{json.dumps([asset['uid'] for asset in state['selection']['selected_assets']])}\n"
        f"LAYOUT ERRORS:\n{json.dumps(state.get('validation_errors', []))}\n"
        f"Items named in the remaining blocking findings: {', '.join(named) or 'none'}.\n"
        "Replace these items with smaller products, or select fewer items, so the room can be laid out. "
        "Return the complete corrected selection."
    )


def _next_fit_step(state: VariantState) -> str | None:
    """Fit step for this turn: compact from the start when the room fit estimate warns,
    compact after the first fit failure or failed layout, capped counts after the next."""
    step = state.get("fit_step")
    previous = state.get("selection_validation")
    if previous is None:
        return "compact" if state["shared"]["room"]["fit_warning"] else None
    if previous.get("fit_failed") or (state.get("validation_errors") and "placement_feedback" not in state):
        return "capped" if step else "compact"
    return step


def _fit_satisfaction(uids: list[str], slots: list[Record], pool: dict[str, list[Record]]) -> Record:
    """Selected counts per requested category, from slot membership: a product listed
    under a requested slot satisfies that slot's category."""
    category_of: dict[str, str] = {}
    for slot in slots:
        if slot["kind"] == "requested":
            for record in pool[slot["id"]]:
                category_of.setdefault(str(record["asset_id"]), slot["category"])
    selected: dict[str, list[str]] = {}
    for uid in uids:
        if uid in category_of:
            selected.setdefault(category_of[uid], []).append(uid)
    return {"selected_counts": [{"category": category, "count": len(found), "selected_uids": found}
                                for category, found in selected.items()]}


# Anchor categories in priority order, as the selection prompt's anchor rule names them.
_ANCHOR_CATEGORIES = {
    "bedroom": ({"bed"},),
    "dining_room": ({"dining_table"},),
    "studio": ({"bed"}, FIT_ANCHOR_SEATING, {"dining_table"}),
}
# Constraint attribute types a prepared vocabulary filters, and the requested-item field holding the filter.
_VOCABULARY_FIELDS = {"color": "colors", "style": "styles", "material": "materials", "fabric": "materials"}
# Starting thresholds, to tune on the benchmark.
STYLE_VIOLATION_BELOW = 0.3
ATTRIBUTE_UNSATISFIED_BELOW = 0.5
_STYLE_QUESTION = "Does this product match the anchor piece's style and palette? Product: {}"
_ATTRIBUTE_QUESTION = 'This product meets the requirement "{}", or the requirement does not apply to it. Product: {}'


def _jev_attribute_constraints(intent: Record) -> list[Record]:
    """Required non-brand attribute constraints that no prepared-vocabulary filter checks."""

    def filtered(constraint: Record) -> bool:
        field = _VOCABULARY_FIELDS.get(constraint.get("attribute_type"))
        return bool(field and constraint.get("category")) and any(
            item.get(field) and _price_constraint_category_matches(constraint["category"], item["canonical_category"])
            for item in intent.get("requested_items") or []
        )

    return [
        constraint for constraint in intent.get("asset_attribute_constraints") or []
        if constraint.get("required", True) and constraint.get("attribute_type") != "brand" and not filtered(constraint)
    ]


def _code_audit() -> Record:
    """The constraint audit with only the checks validate_selection runs itself."""
    return {
        "passed": True,
        "checked_constraints": ["counts, sizes, vocabulary attributes, brands, and prices (code)"],
        "exact_count_violations": [],
        "coordination_violations": [],
        "attribute_constraint_checks": [],
    }


async def _constraint_audit(state: VariantState, ctx: StageContext, intent: Record, products: dict[str, Record]) -> Record:
    """The constraint audit validate_selection reads, built by code and one Jev request.

    products: catalog_asset records of the selected products by uid. Counts, sizes,
    vocabulary attributes, brands, and prices are checked by validate_selection
    itself. With Jev check on, one request asks whether each other selected
    product, except TVs and decor plants, matches the anchor's style and palette
    (below STYLE_VIOLATION_BELOW is a coordination violation), and whether each
    product a `_jev_attribute_constraints` entry targets meets it (below
    ATTRIBUTE_UNSATISFIED_BELOW is unsatisfied). A constraint without a category
    targets every product. Notes a rejection. With check off or failed, the audit
    holds only code results.
    """
    audit = _code_audit()
    if "check" not in ctx.run.jev_uses:
        return audit
    categories = {uid: normalize_category(product.get("category")) for uid, product in products.items()}
    room_type = state["shared"]["room"]["room_type"]
    anchor = next((uid for group in _ANCHOR_CATEGORIES.get(room_type, (FIT_ANCHOR_SEATING,))
                   for uid in products if categories[uid] in group), None)
    styled = [uid for uid in products if anchor and uid != anchor
              and categories[uid] not in BUDGET_EXCLUDED_CATEGORIES and not is_decor_plant(products[uid])]
    constraints = [
        (str(constraint.get("source_label") or constraint.get("label") or constraint.get("value") or "").strip(),
         [uid for uid in products
          if not constraint.get("category") or _price_constraint_category_matches(constraint["category"], categories[uid])])
        for constraint in _jev_attribute_constraints(intent)
    ]
    questions = {f"style_{n}": _STYLE_QUESTION.format(_product_text(products[uid])) for n, uid in enumerate(styled)}
    for i, (label, targets) in enumerate(constraints):
        questions |= {f"attribute_{i}_{n}": _ATTRIBUTE_QUESTION.format(label, _product_text(products[uid]))
                      for n, uid in enumerate(targets)}
    if not questions:
        return audit
    answers = await ctx.ask("check", {**_brief(state["shared"], state["direction"]), "anchor": _product_text(products[anchor]) if anchor else "none"}, questions)
    if answers is None:
        return audit
    if anchor:
        audit["checked_constraints"].append(f"style and palette match to the anchor {anchor} (Jev)")
    mismatched = {uid: answers[f"style_{n}"] for n, uid in enumerate(styled) if answers[f"style_{n}"] < STYLE_VIOLATION_BELOW}
    audit["coordination_violations"] = [
        f"{uid} does not match the style and palette of the anchor {anchor} (Jev yes {yes:.2f}); "
        "choose a product that coordinates with it"
        for uid, yes in mismatched.items()
    ]
    for i, (label, targets) in enumerate(constraints):
        failing = [uid for n, uid in enumerate(targets) if answers[f"attribute_{i}_{n}"] < ATTRIBUTE_UNSATISFIED_BELOW]
        audit["checked_constraints"].append(f'attribute "{label}" (Jev)')
        audit["attribute_constraint_checks"].append({
            "source_label": label,
            "target_uids": targets,
            "satisfied_uids": [uid for uid in targets if uid not in failing],
            "unsatisfied_uids": failing,
        })
    unsatisfied = [f'"{check["source_label"]}" by {check["unsatisfied_uids"]}'
                   for check in audit["attribute_constraint_checks"] if check["unsatisfied_uids"]]
    audit["passed"] = not audit["coordination_violations"] and not unsatisfied
    if not audit["passed"]:
        ctx.run.note(f"jev check rejected: style {list(mismatched)}; attributes {'; '.join(unsatisfied) or 'none'}", ctx.variant_index)
    return audit


async def select(state: VariantState, ctx: StageContext) -> VariantState:
    """Run one selection turn: one model call, the Jev check, then the ported selection validator.

    Reads: `shared`, `pool` (this variant's candidates), `direction`,
    `selection_turns` (turns already made), `selection` and
    `selection_validation` from the previous turn as feedback, and after a
    failed layout `validation_errors`, `blocking_findings`, and `instances`.
    Returns: `selection` (the model's `selected_assets`, one entry per unit, and
    `gaps`), `selection_validation` (the legacy validator report: `valid: bool`,
    `errors: list[str]`, ...), `instances`, `fit_step`, and after a failed
    layout `placement_feedback`. `fit_satisfaction` comes from slot membership
    and the constraint audit from `_constraint_audit`. The selection also fails
    when more than `ctx.run.product_reuse_rate` of its distinct products are
    marked `shared`. Notes the fit step applied with `ctx.run.note(...)`.
    """
    shared = state["shared"]
    fit_step = _next_fit_step(state)
    turn = state.get("selection_turns", 0) + 1
    if fit_step != state.get("fit_step"):
        ctx.run.note(f"selection turn {turn}: fit step {fit_step}", ctx.variant_index)
    placement_feedback = state.get("placement_feedback")
    if placement_feedback is None and state.get("validation_errors"):
        placement_feedback = _placement_feedback(state)
    intent = _selection_intent(shared["intent"], shared["slots"])
    reuse_rate = ctx.run.product_reuse_rate
    response = await ctx.generate(Selection, _selection_prompt(state, intent, fit_step, placement_feedback, reuse_rate))

    selected = [asset.model_dump() for asset in response.selected_assets]
    audit = await _constraint_audit(state, ctx, intent, _products(state["pool"], selected))
    validation = _validated(state, selected, intent, fit_step, audit, reuse_rate)
    instances = validation.pop("instances")
    gap_text = "; ".join(f"{slot['label']}: no eligible catalog product" for slot in _gap_slots(shared["slots"]))
    selection = {
        "selected_assets": selected,
        "gaps": "; ".join(text for text in (response.gaps.strip(), gap_text) if text),
        "fit_step": fit_step,
    }
    update: VariantState = {
        "selection": selection,
        "selection_validation": validation,
        "instances": instances,
        "fit_step": fit_step,
    }
    if placement_feedback is not None:
        update["placement_feedback"] = placement_feedback
    return update


def _products(pool: dict[str, list[Record]], selected: list[Record]) -> dict[str, Record]:
    """catalog_asset records of the selected pool products by uid, with rank's `shared` mark."""
    by_id = {str(record["asset_id"]): record for record in _candidates(pool)}
    return {uid: {**catalog_asset(by_id[uid]), "uid": uid} for asset in selected if (uid := asset["uid"].strip()) in by_id}


def _validated(state: VariantState, selected: list[Record], intent: Record, fit_step: str | None, audit: Record,
               reuse_rate: float) -> Record:
    """validate_selection for `selected` ({uid, functional_group} per unit) from this variant's pool, plus the reuse limit.

    Returns the validator report with `instances`, without `feedback`. The
    selection also fails when more than `reuse_rate` of its distinct products
    are marked `shared`.
    """
    shared = state["shared"]
    candidates = _candidates(state["pool"])
    by_id = {str(record["asset_id"]): record for record in candidates}
    items = [
        {"asset": by_id.get(asset["uid"].strip()) or {"asset_id": asset["uid"].strip()}, "quantity": 1,
         "functional_group": asset.get("functional_group")}
        for asset in selected
    ]
    uids = [str(item["asset"]["asset_id"]) for item in items]
    validation = validate_selection(
        items=items,
        intent=intent,
        room=shared["room"],
        budget=shared["request"].budget,
        candidates=candidates,
        gaps=[slot["category"] for slot in _gap_slots(shared["slots"])],
        fit_step=fit_step,
        fit_satisfaction=_fit_satisfaction(uids, shared["slots"], state["pool"]),
        constraint_audit=audit,
    )
    validation.pop("feedback")
    products = _products(state["pool"], selected)
    shared_uids = [uid for uid in products if products[uid].get("shared")]
    limit = math.floor(round(reuse_rate * len(products), 9))
    if reuse_rate < 1 and len(shared_uids) > limit:
        validation["valid"] = False
        validation["errors"].append(
            f"REUSE LIMIT: {len(shared_uids)} of {len(products)} products are shared with other variants; "
            f"at most {limit} may be. Replace some with products not marked shared."
        )
    return validation


# --- place, repair, correct, and refine ----------------------------------------


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


def _pose_rows(layout: Record) -> list[Record]:
    return [
        {"uid": uid, "x": layout[uid]["position"][0], "y": layout[uid]["position"][1],
         "rotation_z": layout[uid]["rotation"][2], "on_top_of": str(layout[uid].get("on_top_of") or "")}
        for uid in sorted(layout)
    ]


def _apply_poses(layout: Record, poses: list[Pose], assets: list[Record]) -> Record:
    """Apply model poses to a layout. Supported items move with their support; unmentioned items keep their pose."""
    for pose in poses:
        if pose.uid not in layout or not all(math.isfinite(value) for value in (pose.x, pose.y, pose.rotation_z)):
            continue
        layout = move_asset_with_supports(layout, pose.uid, pose.x, pose.y, assets)
        placement = {**layout[pose.uid], "rotation": [0.0, 0.0, pose.rotation_z]}
        if pose.on_top_of == "":
            placement.pop("on_top_of", None)
        elif pose.on_top_of in layout and pose.on_top_of != pose.uid:
            placement["on_top_of"] = pose.on_top_of
        layout = {**layout, pose.uid: placement}
    return layout


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


def _placement_prompt(state: VariantState, seeded: Record, skipped: list[Record]) -> str:
    """Legacy initial-layout prompt for a fresh design, without the seed image and asset-feedback
    decision, answered with poses for moved and skipped items on the measured rule seed."""
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
    skipped_lines = [f"- {entry['uid']} ({entry.get('category', 'unknown')}): {entry.get('reason') or entry.get('status')}"
                     for entry in skipped]
    example_uid = skipped[0]["uid"] if skipped else _key(assets[0])
    none = "None"
    return f"""Edit the rule-based seed layout for this room.

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

ASSETS ({len(assets)} items, ALL must end up placed):
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

SEED LAYOUT (rule-based starting poses, normalized; anchors placed first):
{json.dumps(_pose_rows(seeded["layout"]))}

SEED ASSET BOUNDS:
{format_layout(seeded["layout"], assets) or none}

SEED FINDINGS (measured on the seed, which lacks the skipped items):
{format_issues(seeded["issues"]) or none}

SKIPPED BY THE SEED (not placed; return a pose for each):
{chr(10).join(skipped_lines) if skipped_lines else none}

NODE-LOCAL:
- Respect mount_type: wall_secured items stand on the floor against a wall; wall_mounted and ceiling_mounted items need the stated mounting surface. Unknown mount_type provides no additional constraint.
- Use each uid EXACTLY as provided.
- Plan circulation before placing the seating group: connect each usable doorway to the room's functional zones with the required clear walking width. Clearing only the door swing is insufficient; do not park a chair or cabinet just beyond it across the entry route, even when PROTECTED PATHS is empty.
- If a TV and sofa are selected, plan their shared viewing axis together, facing each other across usable space. Do not assign them independently to convenient perpendicular walls, and do not place the TV across a window to solve another clearance problem.
- If a sofa and coffee table are selected, place them as one reachable group. Calculate their facing edge-to-edge gap from the rotated footprints, not center distance; target the middle of the specified usable range, then place secondary seating outside both that gap and the entry route.
- Before returning, check the complete group for overlaps and walking access. If a seat blocks entry, reposition that seat within the group instead of separating the sofa and coffee table to create a passage between them.
- Move a seed item only to fix a seed finding, to make room for a skipped item, or to satisfy a rule above. The server applies your poses to the seed: every unmentioned item keeps its seed pose and support, and moving a support moves the items on it.

OUTPUT JSON:
{{"poses": [{{"uid": "{example_uid}", "x": number, "y": number, "rotation_z": radians, "on_top_of": ""}}]}}

Return a pose for every skipped item and for each seed item you move. Omit unchanged items."""


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

    critical = levels["critical_p2"]
    return {
        "layout": layout,
        "issues": issues,
        "findings": flatten(("P0", "P1", "P2")),
        "blocking_findings": flatten(("P0", "P1", "critical_p2")),
        "non_blocking_findings": [f for f in flatten(("P2",)) if f["finding"] not in critical.get(f["issue"], [])],
    }


MAX_SWAPS = 2
MAX_DROPS = 2


async def place(state: VariantState, ctx: StageContext) -> VariantState:
    """Place every selected instance: the code solver with PLACEMENT=solver, else the rule seed plus one model edit.

    Reads: `shared`, `instances`, `selection_validation`; for a swap also
    `selection`, `pool`, and `fit_step`.
    Returns: `layout`, `issues`, `findings`, `blocking_findings` (P0, P1, and
    critical P2), and `non_blocking_findings`; after a kept swap also
    `selection`, `selection_validation`, and `instances`.

    Solver (app.rules.layout.solver), no model call, in a worker thread: while
    the layout has unplaceable items or blocking findings, up to MAX_SWAPS
    times, `_swap` replaces the largest named floor item with the next smaller
    product of its slot that keeps the selection valid, and the solver runs
    again. Then, while those items include a dining table or chair, up to
    MAX_DROPS times, `_drop_chair` removes the last dining chair if the
    selection stays valid, and the solver runs again. A swap or drop is kept
    only when the layout scores better. Notes each solve, swap, and drop.

    Model: the seed (app.rules.geometry.generation) places anchors first and
    reports the items it skipped; the stage notes them. The model returns poses
    only for the items it moves and for every skipped item. A response that
    leaves a skipped item unplaced or has non-finite values gets one retry with
    the error; a second one fails the variant.
    """
    if ctx.run.placement == "solver":
        return await asyncio.to_thread(_solve, state, ctx)
    room, instances = state["shared"]["room"], state["instances"]
    room_area = tuple(room["room_area"])
    guidance = build_seed_guidance(
        room_facts=room["facts"], intent_packet=state["shared"]["intent"],
        feasibility_digest=room["digest"], selected_assets=instances,
    )
    seed, report = generate_deterministic_layout_with_report(
        instances, room_area, room_vertices=room["room_vertices"], room_doors=room["room_doors"],
        room_windows=room["room_windows"], planner_guidance=guidance, protected_paths=room["protected_paths"],
    )
    skipped = report["skipped"]
    if skipped:
        ctx.run.note("seed skipped " + ", ".join(f"{entry['uid']} ({entry['status']})" for entry in skipped), ctx.variant_index)
    seeded = _measure(state, seed)
    skipped_uids = {entry["uid"] for entry in skipped}
    prompt = _placement_prompt(state, seeded, skipped)

    async def propose(prompt: str) -> Record:
        response = await ctx.generate(Correction, prompt, system=LAYOUT_SYSTEM_INSTRUCTION)
        moved = _apply_poses(seeded["layout"], [pose for pose in response.poses if pose.uid not in skipped_uids], instances)
        placements = [{**placement, "uid": uid} for uid, placement in moved.items()]
        placements += [
            {"uid": pose.uid, "position": [pose.x, pose.y, 0.0], "rotation": [0.0, 0.0, pose.rotation_z],
             "on_top_of": pose.on_top_of or ""}
            for pose in response.poses if pose.uid in skipped_uids
        ]
        return layout_from_placements(placements, instances, room_area)

    try:
        layout = await propose(prompt)
    except ValueError as exc:
        layout = await propose(f"{prompt}\n\nYOUR PREVIOUS RESPONSE WAS REJECTED: {exc}. Return the poses again, with one pose for every skipped item.")
    return _measure(state, layout)


def _solve(state: VariantState, ctx: StageContext) -> VariantState:
    """The solver branch of `place`: solve, then swap products, then drop dining chairs, solving again while that helps."""
    room, intent = state["shared"]["room"], state["shared"]["intent"]

    def note(report: Record) -> str:
        unplaceable = f", unplaceable {', '.join(report['unplaceable'])}" if report["unplaceable"] else ""
        return (f"solver: score {report['score']}, {report['candidates']} candidates, {report['scored']} scored, "
                f"{report['elapsed']:.2f} s{unplaceable}")

    layout, report = solve_layout(state["instances"], room, intent)
    ctx.run.note(note(report), ctx.variant_index)
    update = _measure(state, layout)
    for change, limit in ((_swap, MAX_SWAPS), (_drop_chair, MAX_DROPS)):
        for _ in range(limit):
            current = {**state, **update}
            keys = report["unplaceable"] or _named_items(update["blocking_findings"], {_key(asset) for asset in current["instances"]})
            changed = change(current, keys, ctx.run.product_reuse_rate) if keys else None
            if changed is None:
                break
            selection, text = changed
            layout, trial_report = solve_layout(selection["instances"], room, intent)
            trial = _measure({**current, **selection}, layout)
            before, after = layout_issue_score(update["issues"]), layout_issue_score(trial["issues"])
            kept = after < before
            ctx.run.note(f"{text}: {'kept' if kept else 'reverted'}, score {list(before)} -> {list(after)}; "
                         f"{note(trial_report)}", ctx.variant_index)
            if not kept:
                break
            update, report = {**update, **selection, **trial}, trial_report
    return update


def _swap(state: VariantState, keys: list[str], reuse_rate: float) -> tuple[VariantState, str] | None:
    """Replace the largest floor item of `keys` with the next smaller product of its slot that keeps the selection valid.

    Every unit of the replaced product changes, so a matched set stays matched.
    The new selection passes validate_selection and the reuse limit again, with
    the code-only constraint audit. Returns the `selection`,
    `selection_validation`, and `instances` update with a description, or None.
    """
    shared = state["shared"]
    by_key = {_key(asset): asset for asset in state["instances"]}
    intent = _selection_intent(shared["intent"], shared["slots"])

    def area(asset: Record) -> float:
        return float(asset.get("width") or 0) * float(asset.get("depth") or 0)

    floor = [key for key in keys if key in by_key and placement_mode_for_asset(by_key[key]) == "floor"]
    for key in sorted(floor, key=lambda key: -area(by_key[key])):
        uid = str(by_key[key]["asset_id"])
        slot_id = next((slot_id for slot_id, rows in state["pool"].items() if any(str(r["asset_id"]) == uid for r in rows)), None)
        if slot_id is None:
            continue
        smaller = [record for record in state["pool"][slot_id] if area(catalog_asset(record)) < area(by_key[key]) - 1e-9]
        for record in sorted(smaller, key=lambda record: -area(catalog_asset(record))):
            replacement = str(record["asset_id"])
            selected = [{**asset, "uid": replacement} if asset["uid"].strip() == uid else asset
                        for asset in state["selection"]["selected_assets"]]
            validation = _validated(state, selected, intent, state.get("fit_step"), _code_audit(), reuse_rate)
            if validation["valid"]:
                instances = validation.pop("instances")
                update: VariantState = {"selection": {**state["selection"], "selected_assets": selected},
                                        "selection_validation": validation, "instances": instances}
                return update, f"swap {uid} -> {replacement} in slot {slot_id}"
    return None


def _drop_chair(state: VariantState, keys: list[str], reuse_rate: float) -> tuple[VariantState, str] | None:
    """Remove the last dining chair when `keys` name a dining table or chair, if the selection stays valid.

    The re-check is the swap's (validate_selection and the reuse limit), so an
    exact or minimum seat count is never broken. Returns the `selection`,
    `selection_validation`, and `instances` update with a description, or None.
    """
    shared = state["shared"]
    by_key = {_key(asset): asset for asset in state["instances"]}
    dining = [key for key in keys if key in by_key and normalize_category(by_key[key].get("category")) in {"dining_table", "dining_chair"}]
    chairs = [key for key, asset in by_key.items() if normalize_category(asset.get("category")) == "dining_chair"]
    if not dining or not chairs:
        return None
    uid = str(by_key[chairs[-1]]["asset_id"])  # instances follow the selection order, so this unit is placed last
    selected = list(state["selection"]["selected_assets"])
    del selected[max(n for n, asset in enumerate(selected) if asset["uid"].strip() == uid)]
    intent = _selection_intent(shared["intent"], shared["slots"])
    validation = _validated(state, selected, intent, state.get("fit_step"), _code_audit(), reuse_rate)
    if not validation["valid"]:
        return None
    instances = validation.pop("instances")
    update: VariantState = {"selection": {**state["selection"], "selected_assets": selected},
                            "selection_validation": validation, "instances": instances}
    return update, f"dropped dining chair {chairs[-1]} ({len(chairs)} -> {len(chairs) - 1} chairs)"


async def repair(state: VariantState, ctx: StageContext) -> VariantState:
    """Apply the ported code fixes in order, keeping each only if layout_issue_score improves. No model call.

    Reads: `shared`, `instances`, `layout`, `issues`.
    Returns: `layout`, `issues`, `findings`, `blocking_findings`, and
    `non_blocking_findings` after the kept fixes. The steps are the legacy P0
    cleanup, the living and dining cleanup, protected-path clearing, and the
    sofa-to-table gap fix. Notes each step's status.
    """
    room, instances = state["shared"]["room"], state["instances"]
    room_area, boundary = tuple(room["room_area"]), room["room_vertices"]
    openings = {"room_doors": room["room_doors"], "room_windows": room["room_windows"], "protected_paths": room["protected_paths"]}
    steps = (
        ("p0 cleanup", lambda layout: run_deterministic_p0_cleanup(layout, instances, room_area, boundary, **openings)),
        ("living/dining cleanup", lambda layout: run_deterministic_living_dining_cleanup(layout, instances, room_area, boundary, **openings)),
        ("path clearing", lambda layout: clear_protected_paths(layout, instances, room_area, boundary, **openings, room_type=room["room_type"])),
        ("sofa-table gap", lambda layout: satisfy_sofa_table_gaps(layout, instances, room_area, boundary, **openings, room_type=room["room_type"])),
    )
    current: VariantState = {key: state[key] for key in ("layout", "issues", "findings", "blocking_findings", "non_blocking_findings")}
    statuses = []
    for name, step in steps:
        layout, report = step(current["layout"])
        candidate = _measure(state, layout)
        kept = layout_issue_score(candidate["issues"]) < layout_issue_score(current["issues"])
        if kept:
            current = candidate
        statuses.append(f"{name} {report['status']}{', kept' if kept else ''}")
    ctx.run.note("repair: " + "; ".join(statuses), ctx.variant_index)
    return current


def _correction_prompt(state: VariantState) -> str:
    """Legacy layout_fix prompt for a fresh design, answered with poses instead of edit tools."""
    shared = state["shared"]
    request, room = shared["request"], shared["room"]
    room_width, room_depth = request.room_area
    room_area = (room_width, room_depth)
    room_type = room["room_type"]
    layout, assets, issues = state["layout"], state["instances"], state["issues"]
    tiers = tuple(tier for tier, keys in ISSUE_KEYS_BY_TIER.items() if any(issues.get(key) for key in keys)) or ("P0", "P1", "P2")
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
{json.dumps(_pose_rows(layout))}

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
    """Make one correction proposal for the findings left after repair: apply, normalize, analyze.

    Reads: `shared`, `selection`, `layout`, `issues`, `correction_proposals`
    (proposals already made).
    Returns the candidate's `layout` and findings when it improves the score
    (fewest physical findings, then critical function, then the rest). A proposal
    that does not improve, or a failed model call (noted, keeping the best layout),
    sets `correction_escalated` when LLM_STAGE_MODELS has a `correct_escalate`
    model and this layout has not escalated yet, so the next proposals use that
    model; otherwise it sets `correction_stalled`, which ends correction.
    """
    escalated = state.get("correction_escalated", False)
    try:
        response = await ctx.generate(Correction, _correction_prompt(state), system=LAYOUT_SYSTEM_INSTRUCTION,
                                      model_key="correct_escalate" if escalated else None)
    except ModelCallError as exc:
        ctx.run.note(f"correction call failed, kept the best layout: {describe(exc)}", ctx.variant_index)
        response = None
    if response is not None:
        candidate = _measure(state, _apply_poses(state["layout"], response.poses, state["instances"]))
        if layout_issue_score(candidate["issues"]) < layout_issue_score(state["issues"]):
            return candidate
    if not escalated and "correct_escalate" in ctx.run.model.stages:
        ctx.run.note(f"correction escalated to {ctx.run.model.stages['correct_escalate'][0]}", ctx.variant_index)
        return {"correction_escalated": True}
    return {"correction_stalled": True}


def _refine_prompt(state: VariantState) -> str:
    """Legacy refine prompt (src/nodes/layout_generation/refine_flow.py) as text only:
    no preview image or quality feedback, answered with poses."""
    shared = state["shared"]
    request, room = shared["request"], shared["room"]
    room_width, room_depth = request.room_area
    room_area = (room_width, room_depth)
    room_type = room["room_type"]
    boundary = room["room_vertices"]
    layout, assets, issues = state["layout"], state["instances"], state["issues"]
    tiers = tuple(tier for tier, keys in ISSUE_KEYS_BY_TIER.items() if any(issues.get(key) for key in keys)) or ("P2",)
    comfort = comfort_layout_measurements(layout, assets, room_type, boundary=boundary)
    studio = room_type == "studio"
    studio_access = {
        "bed_access": bedroom_layout_measurements(layout, assets, boundary, room_area, mixed_groups=True)["beds"],
        "dining_chair_pullout": dining_layout_measurements(layout, assets, boundary, room_area, mixed_groups=True)["chairs"],
    } if studio else {}
    viewing_goal = ""
    outside_viewers = [row for row in comfort["tv_viewing"] if not row["within_preferred_range"]]
    if studio and outside_viewers:
        viewing_goal = "REQUESTED VIEWING COMFORT NEEDS REFINEMENT:\n" + "\n".join(
            f"- {row['media']} viewed by {row['viewer']} ({row['viewer_group']}): "
            f"{row['estimated_view_distance_m']:.2f}m, preferred {row['preferred_distance_range_m']}m. "
            f"{'Increase' if row['estimated_view_distance_m'] < row['preferred_distance_range_m'][0] else 'Reduce'} "
            f"viewing distance by at least {row['distance_outside_preferred_range_m']:.2f}m."
            for row in outside_viewers
        ) + "\nThe layout is not already good for the requested TV use. Propose a feasible material improvement " \
            "to the worst viewer and then the combined excess. Translate the TV/support or viewers' complete " \
            "groups closer for an excessive distance, farther apart for a below-minimum distance. For shared viewing adjust " \
            "the lateral media center between the viewers as well as its distance from the wall; moving only " \
            "along the existing axis may still leave one viewer distant. Aim inside the preferred ranges " \
            "where feasible, not merely at a smaller violation. Wall backing is a lower " \
            "priority than comfortable viewing; an intentional divider may be appropriate. Preserve all access " \
            "bands and check every viewer. Do not return no changes merely because physical validation passes.\n"
    floating_supports = [row for row in comfort["media_placement"] if not row["wall_backed"]]
    if studio and floating_supports:
        viewing_goal += (
            "\nFLOATING MEDIA REVIEW: " + json.dumps(floating_supports) + "\n"
            "Evaluate a wall-backed arrangement of the whole viewing group before accepting a floating console. "
            "An empty strip behind the console is not a functional zone divider. Preserve comfortable viewing, "
            "bed headboard support and every service region; if another group obstructs the better arrangement, "
            "reposition that group's table AND chairs together. A deliberate divider remains acceptable when "
            "no wall-backed arrangement satisfies these relationships.\n"
        )
    return f"""Review this layout after collision fixing.

ORIGINAL DESIGN REQUEST:
{request.user_intent}

{viewing_goal}

{coordinate_system_block(room_width, room_depth)}

USABLE FLOOR BOUNDARY (all furniture footprints must stay inside this polygon):
{json.dumps(boundary)}

{build_rules_block(tiers=tiers, include_definitions=True, room_type=room_type)}

DOORS:
{chr(10).join(format_door_for_prompt(i, door, boundary=boundary, room_area=room_area) for i, door in enumerate(room["room_doors"])) or "None"}

WINDOW GEOMETRY:
{json.dumps(room["room_windows"]) if room["room_windows"] else "None"}

PROTECTED CIRCULATION PATHS:
{json.dumps(room["protected_paths"]) if room["protected_paths"] else "None"}

CURRENT LAYOUT:
{format_layout(layout, assets)}

CURRENT POSES:
{json.dumps(_pose_rows(layout))}

DINING PLACEMENT FACTS (table-local axes; chair fronts face the occupied edge):
{json.dumps(dining_placement_facts(assets))}
Reserve the full table/chairs/pull-out envelope together. Separating intersecting chairs
is not a repair if it blocks their pull-out; move the editable group when necessary.

CURRENT STUDIO SERVICE REGIONS (keep clear even when there are no existing violations):
{json.dumps(studio_access) if studio_access else "Not applicable"}
The regions and allowed center bounds are measured at current poses. Recalculate them
for any moved group; do not put the TV/support or another group into a clear service region.

VIOLATIONS:
{format_issues(issues)}

EXACT OVERLAP SEPARATIONS AND USABLE ARTWORK WALL SPANS:
{json.dumps(overlap_separation_facts(layout, assets, issues.get("overlaps") or []))}
{json.dumps(wall_mount_placement_facts(layout, assets, boundary, room["room_doors"], room["room_windows"], room_area))}

MEDIA AND RUG COMFORT MEASUREMENTS (advisories yield to access and frozen scope):
{json.dumps(comfort)}

RULES:
- Resolve any listed measurable violations first, then make only clear composition wins.
- Prefer NO changes if the layout is already good. Return {{"poses": []}} when no changes are needed.
- Output ONLY assets you want to change. Omit everything else.
- Preserve anchor relationships. Move dependents before anchors whenever possible.
- Prefer rotation tweaks or very small nudges when no functional relationship is broken and viewing distances are comfortable.
- In a Studio, reduce the worst intended viewer's distance outside its preferred range, then the combined distance excess. A small move that keeps the same warning can still improve comfort. Evaluate a closer wall-backed group first; use an intentional divider if no usable wall arrangement gives comfortable viewing. A sofa-wall or exposed-console advisory may trade against a clear viewing improvement, but access, support and physical geometry must not worsen. Shared viewing must improve both viewers, not just the sofa.
- When a listed violation breaks real use, move the needed functional group far enough to repair it; do not leave coffee tables, chairs, desks, media units, or lamps unusable just to keep a tiny move.
- Do not create new overlaps, doorway violations, or boundary violations.
- Keep a continuous usable entry route beyond the door swing, not just an empty swing rectangle. A chair or cabinet immediately beyond that rectangle can still block entry; repair its position without breaking the seating group's usable gaps.
- For sofa ↔ coffee table violations: first reason about a collision-free coffee-table position inside the measured gap range that preserves protected paths. Move only the coffee table when that is sufficient. Move other seating-group assets only when no safe table-only repair exists, and re-check the whole group for overlaps and door, window, and path clearance.
- Check the proposed sofa/table gap edge-to-edge using rotated footprints, and aim inside the measured range rather than at its rounding boundary. Do not accept a repair that clears circulation by moving the table out of reach.
- Chairs should face their intended surface or group: task chairs face desks, dining chairs face dining tables, and lounge chairs face the conversation or focal point.
- Floor lamps should sit beside the seat or surface they serve, not behind seating, in dead corners, or in circulation paths.
- Choose TV/media support and the requested sofa/bed viewers together on readable focal axes. Prefer a solid wall segment that clears windows and doors without leaving viewers too distant. A Studio divider needs a deliberate zone edge, a considered exposed back and clear routes around it. Keep the console front accessible and dining chair pull-out outside the complete media group. Do not require a bed-only TV to face the sofa.
- Wall and ceiling mounted heights are normalized automatically; focus on X/Y placement and yaw.
- If you move a parent asset, stacked children follow automatically.

OUTPUT (JSON):
{{"poses": [{{"uid": "...", "x": number, "y": number, "rotation_z": number}}]}}"""


_REFINE_QUESTION = (
    "Does this layout need composition fixes, such as chairs not facing their surface, lamps away from "
    "the seats they serve, a weak TV focal axis, or an uncomfortable viewing distance?"
)
REFINE_TRIGGER_AT = 0.5


async def refine(state: VariantState, ctx: StageContext) -> VariantState:
    """One text-only composition pass on a layout without blocking findings.

    Reads: `shared`, `instances`, `layout`, `issues`, `non_blocking_findings`.
    Runs when REFINEMENT is `always`, or `jev` and one Jev yes/no question (the
    brief and the non-blocking findings as text) says the layout needs
    composition fixes; a failed Jev call skips it. One model call returns poses
    for the items it changes. Returns the refined `layout` and findings, or {}
    when skipped, rolled back (the score got worse or a blocking finding
    appeared), or the model call failed. Notes the outcome.
    """
    if ctx.run.refinement == "off":
        return {}
    if ctx.run.refinement == "jev":
        findings = [f"{f['issue']}: {json.dumps(f['finding'], sort_keys=True)}" for f in state["non_blocking_findings"]]
        answers = await ctx.ask("refine", {**_brief(state["shared"], state["direction"]), "non_blocking_findings": findings or ["none"]}, {"refine": _REFINE_QUESTION})
        if answers is None:
            return {}
        if answers["refine"] < REFINE_TRIGGER_AT:
            ctx.run.note(f"refinement skipped: Jev yes {answers['refine']:.2f}", ctx.variant_index)
            return {}
    try:
        response = await ctx.generate(Correction, _refine_prompt(state), system=LAYOUT_SYSTEM_INSTRUCTION)
    except ModelCallError as exc:
        ctx.run.note(f"refinement call failed, kept the layout: {describe(exc)}", ctx.variant_index)
        return {}
    candidate = _measure(state, _apply_poses(state["layout"], response.poses, state["instances"]))
    before, after = layout_issue_score(state["issues"]), layout_issue_score(candidate["issues"])
    if candidate["blocking_findings"] or after > before:
        ctx.run.note(f"refinement rolled back: score {list(before)} -> {list(after)}", ctx.variant_index)
        return {}
    ctx.run.note(f"refinement applied: {len(response.poses)} poses, score {list(before)} -> {list(after)}", ctx.variant_index)
    return candidate


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
