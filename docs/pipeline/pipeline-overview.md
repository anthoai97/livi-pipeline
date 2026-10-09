# How the pipeline builds a room

The phase 3 pipeline (`pipeline/`) turns a prompt, room geometry, and budget into
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
  -> 4. three variants in parallel, each:
       a. select    model picks products, rules check them (up to 8 turns)
       b. place     model places every item (one call)
       c. correct   model fixes layout problems (up to 8 proposals)
       d. validate  rules check the final layout, build the render manifest
  -> complete event with every ready variant
```

Steps 1 to 3 run once per request. All three variants share their results.

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
   stage timings, model calls, tokens, cost, slot results, notes, and variant
   outcomes.

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

| Slot kind | Source | Example | Products kept |
|---|---|---|---:|
| Requested | each requested item | sofa, coffee table, floor lamp, TV | 10 |
| Required | room rules not covered by a requested item | storage, design-only plant | 10 |
| Optional | room roles the model may use | accent seating, media unit, storage | 6 |
| Decor | fixed list | planter, sculpture, floor mirror, wall mirror | 4 |

Each slot carries:

- **Search text:** the item label, then its descriptors, then the room style
  hints. Example: `cream sofa, cozy, modern`.
- **Exact filters:** prepared categories, the price limit (the item limit or
  110% of the budget, whichever is lower), the item's size limits, and its exact
  colors, styles, and materials.

TV slots have no price filter, because TV prices never count toward the budget.

### Search each slot

For each slot, the stage:

1. Embeds the search text with `gemini-embedding-2` (`embed_query`).
2. Calls `search_assets`, which ranks products by cosine similarity and keeps
   only placeable products that pass every filter. It fetches up to 30.
3. Applies checks that need the room: the product must fit the floor in either
   orientation, meet the requested minimum sizes, match a strict brand, be
   design-only in a design-only slot, and be allowed in this room type.
4. Moves preferred brands to the front and keeps the slot's limit (10, 6, or 4).

If a required slot finds nothing within the user's limits, the stage searches
again without them and marks the slot as relaxed. A requested item with no
match is reported as a gap. Gaps do not fail the variants.

The result is `slots` (the plan) and `pool` (kept products per slot). The
example run kept 115 products across 15 slots in about 1.3 seconds.

## 4. Run three variants

The graph sends the shared results to three variants that run in parallel. Each
variant has its own state. A failed variant does not stop the others.

| Variant | Direction added to the selection prompt |
|---|---|
| 0 | None |
| 1 | Soft, rounded anchor; warm textiles and wood; cream, sand, terracotta |
| 2 | Compact, structured anchor; cool greys, charcoal, deep blue or green |

Bedrooms, dining rooms, and studios use room-specific versions of directions 1
and 2.

### 4a. Select products

`variant_stages.select` runs one selection turn:

1. The model receives the room, budget, intent, room limits, the variant
   direction, and the candidate pool as CSV grouped by slot. Each row has the
   price, size, brand, colors, styles, materials, a 120-character description,
   mount type, and features.
2. The model returns one product ID per unit, a reason for each, a constraint
   audit, and a selection strategy that names the gaps.
3. The legacy selection validator checks the result:
   - total cost is at most 110% of the budget, with TVs and design-only decor
     excluded
   - counts, required items, and requested attributes
   - footprint is within the density budget
   - tabletop items have a support, each TV has a media support it fits, and
     rugs fit the room

If validation fails, the next turn repeats with the errors. When products do
not fit, the turns step down:

1. First fit failure: switch to smaller products (`compact`).
2. Next fit failure: reduce counts to what fits, keeping required items
   (`capped`).

After 8 failed turns, the variant fails with `asset_selection_failed`.

### 4b. Place products

`variant_stages.place` makes one model call. The model receives each selected
item with its size, mount type, and features, plus the room's openings and
protected paths. It returns a position, rotation, and optional support item for
every unit.

If the response does not place every unit exactly once, the stage retries once
with the error. The layout is then normalized: wall items snap to walls, rugs
fit the room, and displays sit on their supports. It is then analyzed into
findings by severity:

- **P0:** physical problems such as overlaps, items outside the room, and
  blocked doors.
- **P1:** placement problems such as wall items off the wall and blocked
  walking paths.
- **Critical P2:** function problems that block a layout, such as a broken
  seating group.
- **Other P2:** quality notes that do not block.

### 4c. Correct the layout

While blocking findings (P0, P1, or critical P2) remain, `variant_stages.correct`
asks the model for new poses for the items involved. It applies them, moving
supported items with their supports, and analyzes the result again.

A proposal replaces the current layout only if it scores better. After 8
proposals, the variant moves on to validation with its best layout.

### 4d. Validate and send

`variant_stages.validate` runs the final layout check: every selected unit is
placed exactly once and no blocking finding remains.

- **Pass:** it builds the render manifest (model references and placements),
  the selected-asset list, and the total cost, then sends `variant_ready`.
- **Fail:** it sends `variant_failed` with reason `layout_validation_failed`
  and the errors.

The viewer can show each variant as soon as its `variant_ready` event arrives.

## 5. Finish the run

After all three variants finish, the service sends a `complete` event with every
ready variant and writes the run record.

## Limits

| Limit | Value |
|---|---|
| Variants | 3 |
| Selection turns per variant | 8 |
| Correction proposals per variant | 8 |
| Model call timeout | 60 s, up to 3 attempts |
| Run deadline | 300 s |
| Budget allowance | 110% of the stated budget |
| Search fetch per slot | 30 |

## Not in this phase

- Saving designs, design-create payloads, and the chat reply (phase 5).
- Faster selection, placement, and correction, and the refinement pass (phase 4).
- Supabase upload and billing fields.
