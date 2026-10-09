# How the pipeline builds a room

The pipeline (`pipeline/`, phase 4) turns a prompt, room geometry, and budget into
three furnished room designs. This page walks through each step in order and
says what the step does, what it reads, and what it produces.

For the older production pipeline, see the
[legacy pipeline overview](legacy-pipeline-overview.md). For measured timings,
see [phase 3 local runs](phase-3-local-runs.md).

## Overview

```text
POST /pipeline
  -> 1. interpret   one model call: prompt -> structured intent
  -> 2. room        no model call: geometry -> room facts and limits
  -> 3. retrieve    one vector search per slot -> candidate pool
  -> 4. three variants:
       a. rank      once for all three: Jev orders each slot's candidates per
                    direction, then deals them so variants get different products
     then in parallel, each:
       b. select    model picks products, code and Jev check them (up to 4 turns)
       c. place     code solver places every item (no model call)
       d. repair    code fixes layout problems (no model call)
       e. correct   fallback: model fixes blocking findings that remain
                    (up to 3 proposals)
       f. validate  rules check the final layout, build the render manifest
       a failed layout goes back to select once with the items to replace
  -> complete event with every ready variant
```

Steps 1 to 3 and rank (4a) run once per request. All three variants share their
results, and each variant gets its own dealt candidates.

Example request used below:

> A cozy modern living room with a cream sofa, a wooden coffee table, a floor
> lamp for reading, and a TV for movie nights.

Budget USD 5,000, room 5.0 m x 6.0 m, one door, one window.

## 0. Receive the request

`POST /pipeline` (`app/main.py`) accepts the geometry request: prompt, budget,
room type, room size, vertices, wall height, doors, and windows.

The service then:

1. Sends a `start` event with the run ID and stage names.
2. Opens one database connection for the request.
3. Runs the graph and streams its events as server-sent events.
4. Sends a `heartbeat` every 10 seconds while a stage is running.
5. Stops the run when the client disconnects or after 300 seconds. On the
   deadline, it sends every finished variant and marks the rest as failed with
   `timeout`.
6. Writes a run record to `pipeline/.data/runs/<run_id>.json`. The record has
   stage timings, model and Jev calls, tokens, cost, the `JEV_USES`
   and `PRODUCT_REUSE_RATE` switches, slot results, notes, and variant outcomes with their
   non-blocking findings.

Every stage sends `node_start` and `node_complete` events with its elapsed time.

## 1. Interpret the prompt

`shared_stages.interpret` makes one `gemini-3.8-flash` call that turns the prompt
into an intent packet.

| Field | Example |
|---|---|
| Requested items | sofa x1 (cream), coffee table x1 (wooden), floor lamp x1 (reading), TV x1 |
| Per-item limits | sizes the user states, and exact colors, styles, and materials from the catalog vocabulary, such as `colors=[cream]` |
| Excluded and avoided categories | "no rug" -> `excluded_categories=[rug]` |
| Price and brand constraints | "sofa under USD 1,500"; brand is always a preference |
| Style and function hints | `style_hints=[cozy, modern]`, `functional_hints=[tv_focused]` |
| Fit flexibility | `strict_counts`, `space_first`, `flexible`, or `balanced` |

The legacy cleanup code (`coerce_intent_packet`) then normalizes categories and
counts. If this call fails, the whole run fails with an `error` event.

## 2. Read the room

`shared_stages.room` builds the room context from the geometry and the intent.
It makes no model call and takes about 10 ms.

It computes:

- Walls, doors, windows, and blocked zones near openings.
- Protected walking paths that furniture must keep clear.
- A density budget: the minimum and maximum furniture footprint for the room.
- Maximum counts per category for this room size.
- A fit estimate. If the requested items will probably not fit, it sets
  `fit_warning`, and selection starts with smaller products.

## 3. Find candidate products

`shared_stages.retrieve` plans search slots, then runs every slot search at the
same time.

### Plan the slots

One slot is one kind of item the room may need:

| Slot kind | Source | Example | Kept after Jev rank | Kept without it |
|---|---|---|---:|---:|
| Requested | each requested item | sofa, coffee table, floor lamp, TV | 6 | 10 |
| Required | room rules not covered by a requested item | storage, design-only plant | 6 | 10 |
| Optional | room roles the model may use | accent seating, media unit, storage | 4 | 6 |
| Decor | fixed list | planter, sculpture, floor mirror, wall mirror | 3 | 4 |

Rank (4a) deals each variant up to these sizes from each slot.

Each slot carries:

- **Search text:** the item label, then its descriptors, then the room style
  hints. Example: `cream sofa, cozy, modern`.
- **Exact filters:** prepared categories, the price limit (the item limit or
  110% of the budget, whichever is lower), the item's size limits, and its exact
  colors, styles, and materials.

TV categories skip the price filters, because TV prices never count toward the
budget. In a slot that mixes TVs with other categories, such as a requested TV
that a TV stand may substitute, the other categories keep the price filters.

### Search each slot

For each slot, the stage:

1. Embeds the search text with `gemini-embedding-2` (`embed_query`).
2. Calls `search_assets` once, which ranks products by cosine similarity within
   each of the slot's categories and keeps only placeable products that pass
   every filter. It fetches up to 30 per category. The design-only plant slot
   searches only design-only products.
3. Merges the categories in turn, so one category cannot crowd out the others.
4. Applies checks that need the room: the product must fit the floor in either
   orientation, meet the requested minimum sizes, match a strict brand, and be
   allowed in this room type.
5. Moves preferred brands to the front and keeps up to 30 products.

If a required slot finds nothing within the user's limits, the stage searches
again without them and marks the slot as relaxed. A requested item with no
match is reported as a gap. Gaps do not fail the variants.

The result is `slots` (the plan) and `pool` (kept products per slot).

## 4. Run three variants

Rank runs once, then the graph sends the shared results and each variant's own
candidates to three variants that run in parallel. Each variant has its own
state. A failed variant does not stop the others.

| Variant | Direction added to the Jev rank request and the selection prompt |
|---|---|
| 0 | None |
| 1 | Soft, rounded anchor; warm textiles and wood; cream, sand, terracotta |
| 2 | Compact, structured anchor; cool greys, charcoal, deep blue or green |

Bedrooms, dining rooms, and studios use room-specific versions of directions 1
and 2.

### 4a. Rank and deal candidates

`variant_stages.rank` runs once, after retrieve, for all three directions. It
sends one Jev request per slot and direction, about 45 at once. The request
states the brief (prompt, style hints, room type), the variant direction, and
the slot, and asks one yes/no question per candidate: is this product a good
choice? Each question carries the candidate's title, category, colors,
materials, styles, size, price, and description.

For each direction, candidates are sorted by Jev's yes probability, with
preferred brands still first, and the list size is 6, 4, or 3. When Jev ranking
is off or a direction's request fails, that direction keeps the embedding order
and the larger sizes.

Then each slot is dealt to the variants, in plan order. `PRODUCT_REUSE_RATE`
(default 0.5) is the largest share of a variant's products that other variants
may also use:

1. In rounds, each variant takes its highest-ranked product that no other
   variant holds, until it has `ceil(size x (1 - rate))` of them. The first pick
   rotates each round. These products are exclusive: no other variant gets them,
   in any slot.
2. Each variant fills up to its size with its own highest-ranked remaining
   products that are not another variant's exclusive. Variants may share these.

Example with rate 0.5 and size 6: each variant gets 3 exclusive sofas, then 3
more that other variants may also see. Rate 0 gives fully separate pools; rate 1
gives each variant its own top 6.

A slot with fewer than 6 products is not dealt: every variant gets its own
ranked list, the run record notes the shared slot, and its products do not count
toward the reuse limit. Each variant's selection prompt lists its own pool.

### 4b. Select products

`variant_stages.select` runs one selection turn:

1. The model receives the room, budget, intent, room limits, the variant
   direction, and the variant's candidates as CSV grouped by slot. Each row has
   the price, size, brand, colors, styles, materials, a 120-character
   description, mount type, features, and `shared` (yes when another variant
   may also use the product). The prompt states the reuse limit.
2. The model returns one product ID per unit and a note on gaps. It no longer
   writes reasons, a strategy, or a self-audit.
3. Code counts each product under the requested slot it was listed in, so a
   loveseat from the sofa slot satisfies the sofa request.
4. One Jev request checks the selection:
   - **Style.** For each other selected product (not TVs or decor plants): does
     it match the anchor's style and palette? The anchor is the sofa, bed, or
     dining table. Below 0.3 rejects the product.
   - **Attributes.** For each required attribute that the catalog vocabulary
     cannot filter, such as "pet-friendly" or "stain resistant": does each
     targeted product meet it? Below 0.5 rejects the product.
5. The legacy selection validator checks the result:
   - total cost is at most 110% of the budget, with TVs and design-only decor
     excluded
   - counts, required items, and requested attributes, plus the Jev results
   - footprint is within the density budget
   - tabletop items have a support, each TV has a media support it fits, and
     rugs fit the room
   - at most `PRODUCT_REUSE_RATE` of the distinct selected products are marked
     shared (`REUSE LIMIT` error)

When a turn fails only on fit estimates, the code solver (4c) checks the
selection before the next turn. Fit estimates are the footprint estimate
(`OVER CROWDED`) and these layout preflight size checks: sofa, bed, rug, desk
cluster, and dining cluster against the room clear area, and tabletop items
against their supports. If the solver places every item with no blocking
findings, the selection passes, and the run record notes
`fit estimate overruled by the solver: <category counts>; overruled <errors>`.
All other checks still apply, including TV and media pairing, dining table edge
length, dining light clearance, and tiny-room counts.

When a turn is over the budget allowance (`OVER BUDGET`), code tries a budget
repair before the next turn. It takes the purchasable products from most to
least expensive, skipping the anchor (sofa, bed, or dining table), and swaps
each for a cheaper product of the same category from the same slot of this
variant's pool. It picks the next cheaper product in the variant's ranked
order, changes every unit of the product, and keeps the reuse limit. It stops
when the total is within the allowance. The repaired selection goes through the
same checks, including the Jev check and the solver check above. If it passes,
it replaces the model's selection without another model turn, and the run
record notes `budget repair: $<from> -> $<to> (<old> -> <new>, ...)`. If it
fails, the run record notes `budget repair rejected (<swaps>): <errors>`, and
the next turn uses the model's selection as usual. Each failed turn adds
`selection turn <n> failed: <errors>` to the run record.

If validation fails, the next turn repeats with the errors. An over-budget turn
also tells the model to keep every requested item and its count and to replace
expensive picks with cheaper products from the same slots. When products do
not fit, the turns step down:

1. First fit failure: switch to smaller products (`compact`).
2. Next fit failure: reduce counts to what fits, keeping required items
   (`capped`). An item the user asked for that is not optional keeps its
   requested count; only optional items are reduced.

A tabletop item too large for its supports, such as a lamp on a small
nightstand, does not step down. Its error asks for a smaller tabletop item or a
larger support from the same slots.

After 4 failed turns in all, the variant fails with `asset_selection_failed`.

### 4c. Place products

`variant_stages.place` lays out the selection with code, with no model call.
The solver (`app.rules.layout.solver`) splits the selection into groups:

- the bed with its nightstands
- the sofa with its coffee table, accent chairs, side tables, and rug
- the TV stand with its TV
- the dining table with its chairs and ceiling lights
- the desk with its chair
- each other piece alone

Each group has a few arrangements taken from the existing rules. For example,
the sofa stands 0.1 m off its wall, the coffee table 0.47 or 0.54 m in front of
it, and accent chairs beside the table or across from the sofa.

The solver tries each group against every wall, facing into the room, and the
dining table on a grid of positions. It drops a position that overlaps placed
furniture, blocks a door or a protected path, or takes the space a bed side, a
chair pull-out, or a cabinet front needs. When a TV faces the sofa, the sofa can
also move forward off its wall, so that the viewing distance fits the TV size.

Groups go in order, largest first. After each group, the best partial layouts,
measured with the same checker as validation, go on to the next group. Lamps,
tabletop items, and wall art are added last with the seed rules, and the
complete layout with the fewest findings wins. A solve takes 0.1 to 1.5 s on the
test rooms (up to 15 items).

If the solver cannot place an item, or blocking findings remain, the stage
swaps the largest item involved for the next smaller product in the same slot
that keeps the selection valid, and solves again, up to 2 swaps. If a dining
table or chair is still involved, it then removes one dining chair at a time,
up to 2, when the selection stays valid. A request for an exact number of
seats, or the room's minimum, keeps its chairs. Each swap or removal is kept
only if the layout gets better, and the run record notes it. Findings that
remain go to repair and correction.

The layout is then normalized: wall items snap to walls, rugs fit the room, and
displays sit on their supports. It is then analyzed into findings by severity:

- **P0:** physical problems such as overlaps, items outside the room, and
  blocked doors.
- **P1:** placement problems such as wall items off the wall and blocked
  walking paths.
- **Critical P2:** function problems that block a layout, such as a broken
  seating group.
- **Other P2:** quality notes that do not block.

### 4d. Repair the layout

`variant_stages.repair` runs four code fixes in order, with no model call:

1. Move overlapping and out-of-room items to the nearest free spot.
2. Arrange dining chairs and fix living and dining groups.
3. Move items out of protected walking paths.
4. Fix the gap between the sofa and the coffee table.

A fix is kept only if it lowers the layout's issue score.

### 4e. Correct the layout

Correction is the fallback when solving and repair leave a problem. While
blocking findings (P0, P1, or critical P2) remain, `variant_stages.correct`
sends the findings left after repair and asks the model for new poses for the
items involved. It applies them, moving supported items with their supports,
and analyzes the result again.

A proposal replaces the layout only if it scores better. Correction stops at the
first proposal that does not improve, after 3 proposals, or when a model call
fails. It then goes on to validation with the best layout so far.

With a `correct_escalate` entry in `LLM_STAGE_MODELS`, the first proposal that
does not improve, or a failed call, escalates instead of stopping: the
remaining proposals for that layout use the escalation model. For example,
correction can start on a lite model and switch to `gemini-3.8-flash` only for
hard layouts. The run record notes each escalation.

### 4f. Validate and send

`variant_stages.validate` runs the final layout check: every selected unit is
placed exactly once and no blocking finding remains.

- **Pass:** it builds the render manifest (model references and placements),
  the selected-asset list, and the total cost, then sends `variant_ready`.
- **Fail, first time:** the variant goes back to select once, if selection
  turns remain. The prompt names the items in the remaining blocking findings
  and asks for smaller products or fewer items. The fit step moves to the next
  step, and the new layout gets its own 3 correction proposals.
- **Fail again:** it sends `variant_failed` with reason
  `layout_validation_failed` and the errors.

The viewer can show each variant as soon as its `variant_ready` event arrives.

### Jev

Jev (`typesafe-sdk`, model `jev-1.13.0`) answers yes/no questions with a
probability in about 0.3 seconds. Each call has a 5-second timeout and up to 2
attempts, and the run record lists its time, tokens, and cost (USD 0.042 per
million input tokens). `JEV_USES` turns `rank` and `check` on or off. A failed
Jev call never fails a variant: that use falls back to its Jev-off behavior, and
the run record notes it.

## 5. Finish the run

After all three variants finish, the service sends a `complete` event with every
ready variant and writes the run record.

## Limits

| Limit | Value |
|---|---|
| Variants | 3 |
| Selection turns per variant, across reselection | 4 |
| Correction proposals per layout | 3 |
| Reselections per variant | 1 |
| Solver product swaps per layout | 2 |
| Solver dining chairs removed per layout | 2 |
| Model call timeout | 60 s, up to 3 attempts |
| Jev call timeout | 5 s, up to 2 attempts |
| Run deadline | 300 s |
| Budget allowance | 110% of the stated budget |
| Search fetch per slot | 30 per category, 30 kept |

## Not in this phase

- Saving designs, design-create payloads, and the chat reply (phase 5).
- Preview images (phase 5).
- Supabase upload and billing fields.
