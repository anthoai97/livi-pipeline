"""Shared stages: run once per request, before the variants fan out.

Each stage is `async def stage(state: PipelineState, ctx: StageContext) -> PipelineState`.
It reads parent-state keys and returns a partial update with only the keys it
owns. The graph wraps every call in the stage timer (node events, timing, and
outcome); a stage that raises ends the run with an `error` event.

From `ctx` (see app.run.StageContext):
- `await ctx.generate(Schema, prompt, system=...)` makes one structured model call,
  bounded and recorded under this stage. It raises ModelCallError on failure.
- `ctx.run.connection` is the request's sync psycopg connection with dict rows.
  Pass it to `search_assets` inside `asyncio.to_thread`. Retrieval reads only the
  v2 tables through product-data/src/search_assets.py.
- `ctx.run.model.client` is the genai client for `search_assets.embed_query`.
- `ctx.run.record_slot(...)` and `ctx.run.note(...)` add run-record entries.
"""

from __future__ import annotations

import asyncio
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Literal

from prepare_assets import Color, Material, Style
from pydantic import BaseModel
from search_assets import embed_query, search_assets

from app.rules.planner.intent_packet import coerce_intent_packet
from app.rules.planner.room_facts import build_room_context
from app.rules.planner.taxonomy import CANONICAL_CATEGORIES, CATEGORY_ALIASES, normalize_category, sibling_categories
from app.rules.room_policy import (
    BEDROOM_FURNISHING_GUIDANCE,
    DINING_FURNISHING_GUIDANCE,
    STUDIO_FURNISHING_GUIDANCE,
    SUPPORTED_ROOM_TYPES,
    asset_is_eligible_for_room,
)
from app.rules.selection.catalog import (
    _asset_matches_brand_preferences,
    _price_constraint_category_matches,
    _recommendation_price_constraints,
    catalog_asset,
    filter_oversized_seating,
)
from app.rules.selection.constants import BUDGET_EXCLUDED_CATEGORIES, BUDGET_FLEX_PCT
from app.rules.selection.validation import _ORIENTATION_FREE_CATEGORIES, _size_limits_met, room_requirements

if TYPE_CHECKING:
    from app.graph import PipelineState
    from app.run import StageContext

Record = dict[str, Any]

# --- interpret -------------------------------------------------------------

# Schema of the legacy new-design intent call (core/planner/intent_understanding.py),
# without the edit-only, fit-confirmation, and chat-reply fields, plus per-item
# size limits and strict attributes in the prepared vocabularies.
Category = Literal[*CANONICAL_CATEGORIES]
RoomTypeName = Literal[*SUPPORTED_ROOM_TYPES]


class RequestedItem(BaseModel):
    label: str
    canonical_category: Category
    count: int
    exact: bool
    optional: bool
    acceptable_substitutes: list[Category]
    descriptors: list[str]
    relation: str | None = None
    min_width_m: float | None = None
    max_width_m: float | None = None
    min_depth_m: float | None = None
    max_depth_m: float | None = None
    min_height_m: float | None = None
    max_height_m: float | None = None
    colors: list[Color]
    styles: list[Style]
    materials: list[Material]


class PriceConstraint(BaseModel):
    label: str
    category: Category | None
    relation: Literal["under", "above", "between"]
    min_price: float | None
    max_price: float | None
    currency: str


class AttributeConstraint(BaseModel):
    category: Category | None
    attribute_type: Literal["brand", "color", "style", "material", "fabric", "shape", "size", "lifestyle", "purpose", "other"]
    value: str
    required: bool
    source_label: str


class IntentPacket(BaseModel):
    normalized_prompt: str
    room_type: RoomTypeName | None = None
    requested_categories: list[Category]
    excluded_categories: list[Category]
    avoid_categories: list[Category]
    requested_items: list[RequestedItem]
    price_constraints: list[PriceConstraint]
    asset_attribute_constraints: list[AttributeConstraint]
    style_hints: list[str]
    functional_hints: list[str]
    requires_straight_circulation_path: bool
    room_goal: str
    fit_flexibility: Literal["strict_counts", "balanced", "space_first", "flexible"]


def _intent_prompt(user_intent: str, room_type: str) -> str:
    return f"""Extract structured interior-design intent from this user request.

User request:
{user_intent.strip()}

Operation: NEW_DESIGN

Return JSON only. Use canonical categories from the schema.
Rules:
- The selected or saved room purpose is authoritative: room_type must be {room_type}.
- Studio furnishing policy:
  {STUDIO_FURNISHING_GUIDANCE if room_type == "studio" else "Not applicable to this room type."}
  For studio TV requests, use functional_hints `tv_from_bed` when only bed viewing is requested,
  `tv_shared_viewing` when viewing from both bed and sofa is requested, or `tv_from_sitting` when
  sofa/sitting viewing is explicitly requested. Unrelated edits preserve the saved viewing target;
  a new TV defaults to sitting. Do not fabricate user counts from studio furnishing defaults.
  A conflicting room name in the prose does not change this selected room purpose.
  Bed frames, bunk beds, separate mattresses and headboards are unsupported.
- This is a new design. requested_items describe the requested room contents.
  For a bedroom, plan one complete adult bed as the sleeping anchor.
  Other unrequested furniture is a design choice: express useful functions in
  functional_hints instead of inventing requested categories or item counts.
- requested_items.descriptors should preserve item-specific style, material,
  fabric, durability, purpose, or placement language in short phrases.
- count is the intended item count ("two armchairs" -> 2, "a pair of lamps" -> 2).
  Use 1 for singular or uncounted requested items such as "a sofa" or "a linen sofa".
- exact is true only for explicit exact language such as "exactly two".
- optional is true for phrases like "if it fits", "maybe", or "if needed".
- acceptable_substitutes should capture semantic equivalents, e.g. a storage piece
  can be satisfied by bookcase/cabinet/storage_unit, and a lamp can be satisfied
  by floor_lamp/table_lamp/lamp.
- requested_items size limits (min_width_m, max_width_m, min_depth_m, max_depth_m,
  min_height_m, max_height_m) hold only sizes the user states for that item, in
  metres ("no wider than 2.2 m" -> max_width_m=2.2, "at least 6 ft long" ->
  min_width_m=1.83). Width is the item's long front side, depth runs front to back.
  Leave them null when the user states no size.
- requested_items colors, styles and materials hold only the explicit attributes
  the user attaches to that item, using the schema vocabulary ("a cream boucle
  sofa" -> colors=[cream], materials=[boucle fabric]). They are exact catalog
  filters, so leave them empty for soft or uncertain preferences and when no
  vocabulary value matches. A style or mood for the whole room ("a Scandinavian
  bedroom") belongs in style_hints, not in each item's styles. Keep the item's
  words in descriptors.
- In a bedroom, TV/media and work areas are opt-in. Watching TV from bed requests
  a tv and its needed support, not a sofa or coffee table. A working setup or
  study/work-from-home area requests a desk and office_chair as a usable pair;
  preserve explicit desk-only, chair-only, exclusions and optional language.
  On existing-room turns, reuse suitable current inventory instead of requesting
  duplicate desks, chairs or media supports. Do not add these functions to a
  generic bedroom request.
- For a fresh bedroom, use this furnishing policy:
  {BEDROOM_FURNISHING_GUIDANCE if room_type == "bedroom" else "Not applicable to this room type."}
  For NEW_DESIGN bedroom TV/support and desk/office_chair requests, mark the group
  optional=true unless the user makes it mandatory (must-have, do not omit, or
  an explicit non-optional exact count). This permits dropping whole lower-priority
  groups for space. Do not apply this default optionality to existing-room edits.
  Wardrobes are also opt-in: extract a wardrobe request only for an explicit wardrobe/armoire
  or hanging-clothes storage request, never from a generic bedroom brief.
- For a dining room, use this furnishing policy:
  {DINING_FURNISHING_GUIDANCE if room_type == "dining_room" else "Not applicable to this room type."}
  Interpret "seating for six", "a table for six" and "six dining chairs" as dining_chair
  count=6, exact=true. Preserve other explicit seating quantities in the same way.
  A bare request for dining chairs without a stated quantity does not impose count=1;
  leave it optional=true so the fresh-design default applies. Do not fabricate a
  user-requested four-chair count when seating quantity is unspecified.
  Suggested dining storage, overhead lighting and wall art are conditional design
  choices, not requested_items or requested_categories unless the user asks for them.
  Preserve explicit minimal-furniture/essentials-only requests in functional_hints
  as minimal_furniture; a minimalist style alone does not mean an unfurnished room.
  Existing-room requests never reapply fresh-design suggestions or default counts.
  A dining table means a standalone table, not a coffee table, bar table or bundled set.
- price_constraints should capture per-item catalog price filters. Use max_price
  for under/below/less-than requests, min_price for above/over/more-than
  requests, and both min_price and max_price for between/range/from-to requests.
  Leave price_constraints empty for whole-room budgets.
- asset_attribute_constraints should capture concrete item attributes the catalog
  options must satisfy or prefer, such as an IKEA sofa, green chair, curved sofa,
  linen drapes, washable fabric, pet-friendly upholstery, or low-maintenance
  material. Prefer category scoping when the attribute applies to one item. Use
  attribute_type=brand for a retailer/manufacturer name. Brand is always a strong
  preference with cross-brand fallback, so set required=false even when explicitly
  named. For every other explicit attribute set required=true; use required=false
  for softer preferences.
- Do not include categories that the user explicitly asks not to add.
- Put excluded categories in excluded_categories.
- Put softer negative preferences in avoid_categories.
- room_goal should summarize the desired use case in one short phrase.
- fit_flexibility should be strict_counts for exact-count requests, space_first
  for small-room/circulation-first requests, flexible for vague requests, and
  balanced otherwise.
- functional_hints should be concise semantic goals such as small_space, storage,
  tv_focused, conversation, work_from_home, open_feel, or layered_lighting.
- requires_straight_circulation_path should be true only when the user explicitly
  asks for a straight/direct/through route to stay clear, describes the room as a
  pass-through, or names daily access between entries/doors/openings that must
  stay unobstructed. It should be false for generic open-feel or circulation
  preferences.
"""


async def interpret(state: PipelineState, ctx: StageContext) -> PipelineState:
    """Interpret the prompt with one model call.

    Reads: `request` (PipelineRequest).
    Returns: `intent`, the cleaned intent packet (requested, excluded, and avoided
    categories, counts, descriptors, price, size, and attribute constraints, style
    and function hints, fit flexibility, circulation-path flag).
    """
    request = state["request"]
    packet = await ctx.generate(IntentPacket, _intent_prompt(request.user_intent, request.room_type))
    intent = coerce_intent_packet(
        {**packet.model_dump(), "interpretation_source": "llm"},
        fallback_intent=request.user_intent,
        room_type=request.room_type,
    )
    return {"intent": intent}


# --- room ------------------------------------------------------------------


async def room(state: PipelineState, ctx: StageContext) -> PipelineState:
    """Build the room context from the request geometry and interpreted counts. No model call.

    Reads: `request`, `intent`.
    Returns: `room` (walls, openings, blocked zones, protected paths, density
    budget, category caps, room scale, and the fit estimate with counts that fit).
    """
    request = state["request"]
    context = build_room_context(
        room_type=request.room_type,
        room_area=request.room_area,
        room_vertices=[list(vertex) for vertex in request.room_vertices],
        wall_height=request.wall_height,
        room_doors=request.room_doors,
        room_windows=request.room_windows,
        intent=state["intent"],
    )
    return {"room": context}


# --- retrieve --------------------------------------------------------------

# Each slot's pool holds up to FETCH eligible products. Rank deals each variant up
# to KEEP of them in embedding order, or up to RANKED_KEEP in Jev's order.
FETCH = 30
KEEP = {"requested": 10, "required": 10, "optional": 6, "decor": 4}
RANKED_KEEP = {"requested": 6, "required": 6, "optional": 4, "decor": 3}
DECOR_CATEGORIES = ("planter", "sculpture", "floor_mirror", "wall_mirror")
_ATTRIBUTE_KEYS = ("colors", "styles", "materials")
# Requested-item limits a slot carries: sizes in metres, then strict prepared attributes.
LIMIT_KEYS = ("min_width_m", "max_width_m", "min_depth_m", "max_depth_m", "min_height_m", "max_height_m", *_ATTRIBUTE_KEYS)


def _prepared_categories(canonical: list[str]) -> list[str]:
    """Prepared category values the rules read as these canonical categories (normalize_category)."""
    wanted = set(canonical)
    return sorted(wanted | {alias for alias, target in CATEGORY_ALIASES.items() if target in wanted})


def _max_price(constraints: list[Record]) -> float | None:
    prices = [float(constraint["max_price"]) for constraint in constraints if constraint.get("max_price")]
    return min(prices) if prices else None


def _brands(intent: Record, canonical: list[str], *, required: bool) -> list[str]:
    return [
        constraint["value"]
        for constraint in intent.get("asset_attribute_constraints") or []
        if constraint.get("attribute_type") == "brand"
        and bool(constraint.get("required")) == required
        and (
            not constraint.get("category")
            or any(_price_constraint_category_matches(constraint["category"], category) for category in canonical)
        )
    ]


def _search_text(*parts: str) -> str:
    """Join phrases, skipping repeats and phrases the text already contains."""
    text: list[str] = []
    for part in parts:
        part = " ".join(str(part).split())
        if part and not any(part.lower() in existing.lower() for existing in text):
            text.append(part)
    return ", ".join(text)


def plan_slots(intent: Record, room: Record, budget: float) -> list[Record]:
    """Plan one search slot per requested item, required item, optional role, and decor category.

    A required role that a requested item covers is filled by that item's slot,
    and TVs search apart from their supports. Each slot has `id`, `kind`
    (requested, required, optional, decor), `label`, `category` (the requested
    canonical category, or None), `count`, `text`
    (search text), `categories` (prepared category values), `max_price`,
    `limits` (requested size limits and strict attributes), `strict_brands`,
    `preferred_brands`, `design_only`, `required`, `relaxed` (searched without
    the request's limits), and `gap`.
    """
    room_type = room["room_type"]
    width, depth = room["room_area"]
    allowance = round(float(budget) * (1 + BUDGET_FLEX_PCT), 2)
    style_hints = intent.get("style_hints") or []
    excluded = {normalize_category(category) for category in intent.get("excluded_categories") or []}
    caps = room["digest"].get("max_counts_by_category") or {}
    general_prices = [constraint for constraint in intent.get("price_constraints") or [] if not constraint.get("category")]
    requirements = room_requirements(room_type=room_type, intent=intent, digest=room["digest"])
    slots: list[Record] = []

    def add(kind: str, slot_id: str, canonical: set[str], *, label: str = "", item: Record | None = None,
            count: int = 0, required: bool = False, design_only: bool = False) -> None:
        kept = [asset["category"] for asset in filter_oversized_seating([{"category": c} for c in sorted(canonical)], width, depth)]
        if not kept:
            return
        item = item or {}
        prices = _recommendation_price_constraints(intent, item["canonical_category"]) if item else general_prices
        item_price = _max_price(prices)
        used, base, n = {slot["id"] for slot in slots}, slot_id, 1
        while slot_id in used:
            n += 1
            slot_id = f"{base}_{n}"
        label = label or " or ".join(category.replace("_", " ") for category in kept)
        slots.append({
            "id": slot_id,
            "kind": kind,
            "label": label,
            "category": item.get("canonical_category"),
            "count": count,
            "text": _search_text(label, *item.get("descriptors", []), *style_hints),
            "categories": _prepared_categories(kept),
            "max_price": min(item_price, allowance) if item_price is not None else allowance,
            "limits": {key: item[key] for key in LIMIT_KEYS if item.get(key)},
            "strict_brands": _brands(intent, kept, required=True),
            "preferred_brands": _brands(intent, kept, required=False),
            "design_only": design_only,
            "required": required,
            "relaxed": False,
            "gap": False,
        })

    # A requested item fills the required role it covers. The design-only plant
    # keeps its own slot, because only a design-only planter satisfies it.
    furniture_needs = [need for need in requirements["required"] if not need["decor_only"]]
    requested_categories: set[str] = set()
    for item in intent.get("requested_items") or []:
        canonical = {item["canonical_category"], *item.get("acceptable_substitutes", []), *sibling_categories(item["canonical_category"])}
        requested_categories |= canonical
        covers = any(set(need["categories"]) & canonical for need in furniture_needs)
        add("requested", item["canonical_category"], canonical, label=item["label"], item=item, count=item["count"], required=covers)
    for need in requirements["required"]:
        if need["decor_only"] or not set(need["categories"]) & requested_categories:
            add("required", need["role"], set(need["categories"]), count=need["count"], required=True, design_only=need["decor_only"])
    for role, categories in requirements["optional_roles"].items():
        categories = set(categories) - requested_categories
        add("optional", role, categories - BUDGET_EXCLUDED_CATEGORIES)
        # TVs search apart from their supports, so each keeps its own candidates.
        add("optional", "tv", categories & BUDGET_EXCLUDED_CATEGORIES)
    for category in DECOR_CATEGORIES:
        if category not in excluded | requested_categories and int(caps.get(category, 1)) > 0:
            add("decor", category, {category})
    return slots


def slot_filters(slot: Record) -> Record:
    """search_assets arguments for a slot.

    Each category fetches up to FETCH products, so one category cannot crowd out
    the others. A design-only slot searches only design-only products. Rug sizes
    follow the model axes, so they are checked after the search. TV categories are
    exempt from the price filters, because TV prices never count toward the budget;
    the slot's other categories keep them.
    """
    filters: Record = {
        "limit": FETCH, "categories": slot["categories"], "per_category": True, "design_only": slot["design_only"],
        "known_price": True, "max_price": slot["max_price"],
        "price_exempt": [category for category in slot["categories"] if normalize_category(category) in BUDGET_EXCLUDED_CATEGORIES],
    }
    limits = slot["limits"]
    if not _orientation_free(slot):
        filters.update({key: limits[key] for key in ("max_width_m", "max_depth_m", "max_height_m") if key in limits})
    filters.update({key: limits[key] for key in _ATTRIBUTE_KEYS if key in limits})
    return filters


def _orientation_free(slot: Record) -> bool:
    return any(normalize_category(category) in _ORIENTATION_FREE_CATEGORIES for category in slot["categories"])


def merge_categories(rows: list[Record]) -> list[Record]:
    """Interleave rows round-robin across canonical categories, each in distance order.

    The first category is the one with the nearest row.
    """
    groups: dict[str, list[Record]] = {}
    for row in rows:
        groups.setdefault(normalize_category(row["category"]), []).append(row)
    return [row for turn in zip_longest(*groups.values()) for row in turn if row is not None]


def keep_slot(slot: Record, rows: list[Record], room: Record) -> list[Record]:
    """Merge categories, apply the checks after the search, and keep up to FETCH.

    Drops products that fit the floor in neither orientation, products outside the
    requested sizes (rugs in either orientation), other brands when a brand is strict,
    and products the room type excludes. Preferred brands move to the front.
    """
    width, depth = room["room_area"]
    kept = []
    for row in merge_categories(rows):
        asset = catalog_asset(row)
        w, d = asset["width"], asset["depth"]
        if not ((w <= width and d <= depth) or (d <= width and w <= depth)):
            continue
        if slot["limits"] and not _size_limits_met(asset, slot["limits"]):
            continue
        if slot["strict_brands"] and row.get("is_purchasable") and not _asset_matches_brand_preferences(row, slot["strict_brands"]):
            continue
        if not asset_is_eligible_for_room(row, room["room_type"]):
            continue
        kept.append(row)
    if slot["preferred_brands"]:
        kept.sort(key=lambda row: not _asset_matches_brand_preferences(row, slot["preferred_brands"]))
    return kept[:FETCH]


async def retrieve(state: PipelineState, ctx: StageContext) -> PipelineState:
    """Plan the slots and run one embedding and one per-category `search_assets` call per slot, all at once.

    Reads: `request`, `intent`, `room`.
    Returns: `slots` (planned slots with id, role, count, descriptors, and a `gap`
    flag) and `pool` (up to FETCH eligible candidates per slot id, categories
    merged round-robin, preferred brands first, not yet cut to a keep size). Calls
    `ctx.run.record_slot(slot_id, candidates, gap)` for every slot.
    """
    request, intent, room_context = state["request"], state["intent"], state["room"]
    allowance = round(request.budget * (1 + BUDGET_FLEX_PCT), 2)
    client, connection = ctx.run.model.client, ctx.run.connection

    async def search(slot: Record) -> tuple[Record, list[Record], str | None]:
        vector = await asyncio.to_thread(embed_query, client, slot["text"])
        rows = await asyncio.to_thread(search_assets, connection, vector, **slot_filters(slot))
        kept = keep_slot(slot, rows, room_context)
        has_limits = slot["limits"] or slot["strict_brands"] or slot["max_price"] < allowance
        if kept or not slot["required"] or not has_limits:
            return slot, kept, None
        slot = {**slot, "limits": {}, "strict_brands": [], "max_price": allowance, "relaxed": True}
        rows = await asyncio.to_thread(search_assets, connection, vector, **slot_filters(slot))
        return slot, keep_slot(slot, rows, room_context), f"required {slot['label']} has no product within the request's limits; searched without them"

    slots, pool = [], {}
    for slot, kept, note in await asyncio.gather(*(search(slot) for slot in plan_slots(intent, room_context, request.budget))):
        # Only a requested item can be a gap; an empty required slot fails selection validation.
        gap = not kept and slot["kind"] == "requested"
        if gap:
            note = "; ".join(filter(None, [note, f"requested {slot['label']} has no eligible catalog product; reported as a gap"]))
        elif not kept and slot["required"]:
            note = "; ".join(filter(None, [note, f"required {slot['label']} has no eligible catalog product"]))
        if note:
            ctx.run.note(note)
        ctx.run.record_slot(slot["id"], len(kept), gap, note)
        slots.append({**slot, "gap": gap})
        pool[slot["id"]] = kept
    return {"slots": slots, "pool": pool}
