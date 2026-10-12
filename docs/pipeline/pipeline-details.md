# Pipeline details

The full reference for the pipeline (`pipeline/`) as of 2026-10-11: each step in
order, what it reads, what it produces, and the exact rules. For the short
version with the flow diagram, see the [pipeline overview](pipeline-overview.md).
For measured speed and quality, see the [2026-10-11 report](report-2026-10-11.md).
For the older production pipeline, see the
[legacy pipeline overview](legacy-pipeline-overview.md).

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
       b. select    model picks products, code adds or swaps the TV, Jev and
                    code check them, code swaps products that fail on style,
                    size or budget (up to 4 turns)
       c. place     code solver places every item; each variant takes a
                    different one of its tied best layouts
       d. finish    model reviews and adjusts the layout; the final check
                    removes failing items; build the render manifest
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
   non-blocking findings, preview render results, and browser timing entries.

Every stage sends `node_start` and `node_complete` events with its elapsed time.
Each `node_complete` also carries `data` for browser progress: interpreted
categories, room facts, retrieved products, and counts or validation results
from the variant stages. These facts describe work already done. They do not
change selection or placement. See the [delivery reference](../../pipeline/README.md#browser-delivery)
for the fields.

## 1. Interpret the prompt

`shared_stages.interpret` makes one model call (`gemini-3.8-flash` by default;
see [Models](#models)) that turns the prompt into an intent packet.

| Field | Example |
|---|---|
| Requested items | sofa x1 (cream), coffee table x1 (wooden), floor lamp x1 (reading), TV x1 |
| Per-item limits | sizes the user states, and exact colors, styles, and materials from the catalog vocabulary, such as `colors=[cream]` |
| Excluded and avoided categories | "no rug" -> `excluded_categories=[rug]` |
| Price and brand constraints | "sofa under USD 1,500"; brand is always a preference |
| Style and function hints | `style_hints=[cozy, modern]`, `functional_hints=[tv_focused]` |
| Fit flexibility | `strict_counts`, `space_first`, `flexible`, or `balanced` |

The legacy cleanup code (`coerce_intent_packet`) then normalizes categories and
counts. It also drops a TV stand or media unit listed as a substitute for a TV:
a stand is never a TV, and select adds a missing TV (4b). If this call fails,
the whole run fails with an `error` event.

## 2. Read the room

`shared_stages.room` builds the room context from the geometry and the intent.
It makes no model call and takes about 10 ms.

It computes:

- Walls, doors, windows, and blocked zones near openings.
- Protected walking paths that furniture must keep clear.
- A size class (compact, medium, or spacious) by floor area per room type:
  living room below 18 and 30 m², bedroom 11 and 16 m², dining room 10 and
  16 m², studio 25 and 40 m². A 15 m² bedroom is medium.
- A density budget: how much of the usable floor (doors excluded) floor
  furniture may cover, by room type and size class. Footprint is width x depth
  of each floor piece; rugs, wall- and ceiling-mounted pieces, tabletop items
  and a TV on its stand do not count. Each room gets a comfortable target and a
  maximum (`comfortable_load_sqm`, `overcrowded_load_sqm`); a living room or
  bedroom also gets a sparse floor (`sparse_below_load_sqm`):

  | Room | Compact | Medium | Spacious |
  |---|---|---|---|
  | Living room | 22% / 30% | 27% / 35% | 30% / 38% |
  | Bedroom | 42% / 50% | 35% / 43% | 30% / 38% |
  | Dining room | 32% / 40% | 30% / 38% | 28% / 36% |
  | Studio | 35% / 43% | 32% / 40% | 30% / 38% |

  For example, a 22 m² living room with a 0.81 m² door zone has 21.19 m²
  usable: a 5.72 m² target and a 7.42 m² maximum. The sparse floor is 15%
  (compact), 16% (medium) or 12.5% (spacious).
- Maximum counts per category for this room size.
- A fit estimate: the requested items' real footprint, from default sizes per
  role, against the same target and maximum. Above the target the room "may
  feel tight", above the maximum "may feel overcrowded". When a requested
  count does not fit, it sets `fit_warning`, and selection starts with smaller
  products. Clearances are left to the layout checks, so a bed or sofa counts
  at its own size.

## 3. Find candidate products

`shared_stages.retrieve` plans search slots, then runs every slot search at the
same time.

### Plan the slots

One slot is one kind of item the room may need:

| Slot kind | Source | Example | Kept after Jev rank | Kept without it |
|---|---|---|---:|---:|
| Requested | each requested item | sofa, coffee table, floor lamp, TV | 6 | 10 |
| Required | room rules not covered by a requested item (see below) | lamp, nightstand, sideboard, design-only plant | 6 | 10 |
| Optional | room roles the model may use, and the other categories of a required role a request covers | accent seating, media unit, storage; a pendant or chandelier in a dining room or studio; side tables beside a requested coffee table | 4 | 6 |
| Decor | fixed list | planter, sculpture, floor mirror, wall mirror, wall art | 4 | 6 |

The required items per room (`room_requirements`):

| Room | Core | Completeness pieces |
|---|---|---|
| Living room | sofa; coffee or side table (a desk counts only when work is requested); rug; design-only plant; perimeter storage when spacious | a lamp; one accent chair when spacious |
| Bedroom | one bed; rug; design-only plant | a nightstand and a lamp; a dresser or wardrobe from medium size |
| Dining room | one table and its chairs (4, or 6 when spacious; 4 to 6 pass); design-only plant | a sideboard or cabinet and a pendant or chandelier from medium size |
| Studio | bed; sofa or loveseat; a table with 2 chairs when the brief asks for one or the studio is 25 m² or more | a nightstand and a lamp; a dresser or wardrobe from medium size |

A completeness piece is not required when the brief excludes its kind, and the
accent chairs, clothes storage, and dining pieces are not required for a minimal
or space-first brief.

Rank (4a) deals each variant up to these sizes from each slot.

Each slot carries:

- **Search text:** the item label, then its descriptors, then the room style
  hints. Example: `cream sofa, cozy, modern`.
- **Exact filters:** prepared categories, the price limit (the item limit or
  110% of the budget, whichever is lower), the item's size limits, and its exact
  colors, styles, and materials.

TV categories skip the price filters, because TV prices never count toward the
budget. TVs search apart from their stands, so each keeps its own candidates.

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
   allowed in this room type. In a living room, coffee tables outside 0.35 to
   0.50 m high and side tables outside 0.45 to 0.65 m high are dropped, the
   same limits the selection size check applies.
5. Moves preferred brands to the front and keeps up to 30 products.

If a required slot finds nothing within the user's limits, the stage searches
again without them and marks the slot as relaxed. A requested item or a
completeness piece with no match is reported as a gap and is not required.
Gaps do not fail the variants.

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
   The prompt also asks the model to:
   - furnish the room to its size: a side surface and a light by each seat, a
     complete seating group, long walls used in a spacious room, while
     minimal briefs stay minimal;
   - spend 85 to 95% of the budget, at least 80% and at most 110%, on better
     versions of the main pieces first, then on useful missing pieces, never
     on filler;
   - add design-only decor (plant, sculpture, floor or wall mirror) once the
     room is complete, whatever budget remains, as many as the room size and
     brief suit. It costs nothing toward the budget;
   - add up to 2 retail decor pieces (sculpture, plant, floor mirror) when the
     total is under the budget. The design-only plant does not count toward
     these.
2. The model returns one product ID per unit and a note on gaps. It no longer
   writes reasons, a strategy, or a self-audit.
3. Code fixes the TV before any check. When the brief asks for a TV, or a stand
   is selected, and there is no TV, it adds one from the variant's candidates,
   preferring one that fits a selected stand. A selected TV that fits no
   selected stand is swapped for the first candidate that does. A TV that
   still fits no stand hangs on the wall (4c). The run record notes
   `added TV` or `replaced TV`.
4. Code counts each product under the requested slot it was listed in, so a
   loveseat from the sofa slot satisfies the sofa request.
5. One Jev request checks the selection:
   - **Style.** For each other selected product (not TVs or decor plants): does
     it match the anchor's style and palette? The anchor is the sofa, bed, or
     dining table. Below 0.3 rejects the product.
   - **Attributes.** For each required attribute that the catalog vocabulary
     cannot filter, such as "pet-friendly" or "stain resistant": does each
     targeted product meet it? Below 0.5 rejects the product. An attribute
     with no category, such as "wool and boucle textures", skips TVs and decor
     plants.
6. The legacy selection validator checks the result:
   - total cost is at most 110% of the budget, with TVs and design-only decor
     excluded
   - counts, required items, and requested attributes, plus the Jev results.
     A missing completeness piece fails the turn unless a fit step was
     applied or the variant's candidates hold no product for it. A bed 1.4 m
     or wider in a spacious bedroom needs a nightstand on each side, unless
     the brief states a nightstand count
   - piece sizes: a living-room coffee table 0.35 to 0.50 m high and a side
     table 0.45 to 0.65 m high; a dining light 0.15 m inside each table edge;
     a bedroom rug that is a runner (at most 0.90 m wide) or reaches 0.45 m
     past each side of the bed. A piece within 5 cm of a limit passes,
     because catalog sizes come from 3D bounding boxes. A failing piece is an
     error only when the candidates hold one that passes, else a warning. A
     coffee table 0.5 to 0.75 x the sofa width is only a warning
   - footprint reaches the density floor and stays at or under the maximum.
     Over the maximum is an `OVERCROWDED` error, for example `OVERCROWDED:
     floor furniture covers 7.67m² of 21.19m² usable; at most 7.42m² (target
     5.72m²) for a medium living room. Drop optional pieces or choose smaller
     ones.` It is a warning after a fit step, or when the pieces filling the
     required roles alone go over the maximum. The prompt asks the model to
     stay at or under the target
   - tabletop items have a support (a TV no longer needs a stand)
   - at most `PRODUCT_REUSE_RATE` of the distinct selected products are marked
     shared (`REUSE LIMIT` error)

A selection that passes these checks goes to the code solver (4c) once. The
solver decides fit; the validator does not compare piece sizes with the room
or their supports. If the solver leaves items out, the selection fails
with one `LAYOUT FIT` error that names each item left out, with its size, and
asks for products from the same slots that fit together. Blocking findings on
placed items do not fail the selection; the final review (4d) handles them.
The validator still checks dining table edge length (a table under 0.75 m
deep seats one long side only), dining light clearance, and tiny-room counts.

Code then swaps products that fail on their own, without another model turn
(`_swap_repair`). It changes every unit of a product and swaps only within the
same category and the same slot of this variant's pool, in ranked order:

1. **Style and size.** A product Jev rejects for style, or a `SIZE` error
   whose candidates hold a passing product, is swapped first.
2. **Over the allowance** (`OVER BUDGET`, above 110%): the purchasable
   products from most to least expensive, skipping the anchor (sofa, bed, or
   dining table), are swapped for cheaper ones until the total is within the
   allowance. A walk that cannot reach the allowance swaps nothing.
3. **Under the floor** (a passing selection under 80% of the budget): the
   products from most to least expensive, anchors included, are swapped for
   pricier ones that keep the total at most the budget, until it reaches 90%.
   It upgrades pieces; it never adds any.

A replacement must pass before it is used: not selected, purchasable when the
product is, within the reuse limit, and no `SIZE` error beside the other
pieces. A product Jev rejects for style gets every such candidate in its slot;
any other product gets up to 3. One request asks Jev the style question for
all of them, against the anchor, and a product takes its first candidate in
ranked order with yes at or above 0.3. With the Jev check off or failed,
candidates pass on the code checks alone, and the note says
`code checks only`. For example, a sideboard Jev rejects is swapped for the
first sideboard in its slot that Jev accepts, even the fifth, and an
over-budget chair set skips a cheaper chair Jev would reject and takes the
next one.

When Jev rejects every candidate in the slot, or the slot has none, the
product stays and its style mismatch is no longer an error: the validation
report gets a `STYLE KEPT` warning, and the run record notes
`style kept: <uid> (no candidate in the slot matches the anchor)`. The
selection is checked again without another Jev request. The product stays
accepted for the rest of the design's turns (`style_kept` in the variant
state).

The swapped selection goes through the same checks, including the Jev check
and the solver check above. A round is kept when it passes or leaves fewer
errors, and a second round starts from it, so a style fix stays even while
the budget is still off. The run record notes `swap repair: <old> -> <new>
(style|size|budget), ...`, or `swap repair rejected: <swaps>; <errors>`. Each
turn that ends failing adds `selection turn <n> failed: <errors>` to the run
record.

If validation still fails, the next turn repeats with the previous selection's
product IDs and its errors, one per line, not the whole validation report,
then every product the Jev check rejected for style or an attribute in this
design's turns, except the kept ones, as `REJECTED BY THE STYLE AND ATTRIBUTE
CHECK (do not pick again): <uid>, ...`. These products stay in the catalog
block, so the prompt cache still covers it. The
prompt puts the parts all three designs share first (room, budget, intent,
room facts, digest, selection guidance), then the design's catalog, then what
changes per design and turn, so the model's prompt cache covers the longest
prefix. The model is asked again only for errors the repair cannot fix, such
as a missing required item, counts, layout fit, unknown IDs, an
under-furnished or overcrowded room. An over-budget turn also tells the model
to keep every requested item and its count and to replace expensive picks with
cheaper products from the same slots. When products do not fit, the turns step
down:

1. First fit failure: switch to smaller products (`compact`).
2. Next fit failure: reduce counts to what fits, keeping required items
   (`capped`). An item the user asked for that is not optional keeps its
   requested count; only optional items are reduced.

A fit failure is a turn where the solver leaves out a floor item. A left-out
tabletop item, such as a lamp too large for a small nightstand, fails the turn
but does not step down.

After 4 failed turns in all, the last selection loses the products that fail
it, and the variant goes on to placement. Code removes one product a round,
every unit of it, while that leaves fewer errors (then less cost over the
budget allowance). It never removes the anchor piece (sofa, bed, or dining
table). For example, a coffee table that does not match the sofa's style, or a
lamp with nothing to stand on, is removed instead of failing the design. Errors
that removal cannot fix, such as a missing required item, stay in the run
record, and the design is still delivered. The removal rounds cannot see a
`LAYOUT FIT` failure, so a selection that fails it goes to placement as it is.
Finish then removes the items the final check fails on.

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
the sofa stands 0.08 m off its wall, the coffee table 0.39 or 0.44 m in front of
it (the check allows 0.38 to 0.50 m), and accent chairs beside the table or
across from the sofa.
[Adding a product category](adding-a-product-category.md) explains how a
category's role decides its group, and when a new category needs its own.

The solver tries each group against every wall, facing into the room, and the
dining table on a grid of positions. It drops a position that overlaps placed
furniture, blocks a door or a protected path, or takes the space a bed side, a
chair pull-out, or a cabinet front needs: 0.60 m beside a bed and past its foot
(0.91 m when the foot strip is the way to a door), 0.46 m behind a dining chair,
0.60 m behind a desk chair, 0.76 m in front of storage, and 0.56 m in front of a
sofa. A door's clear zone is as deep as the door is wide. It also keeps the
whole dining group, chairs included, 0.76 m from the sofa group. When a TV faces the sofa, the sofa can
also move forward off its wall, so that the viewing distance fits the TV size.

A TV that fits no selected stand hangs on the wall that faces its viewer, with
its screen center at 1.1 m (seated eye level), or 1.3 m when it serves a bed,
and above any stand under it.
Its viewing distance is still measured and costed. A TV that fits a stand
stands on it, as before.

In a studio, the TV stand can also stand free as a divider, on the sofa's axis
and facing it, with the screen near the middle of its viewing range (1.5 to 2.8
times the TV width). For example, a 1.2 m TV that the far wall puts 5.3 m from
the sofa stands 2.6 m from it instead. The checker notes a free-standing stand,
so a wall-backed TV within its range still wins. A floor lamp goes beside the
sofa or an accent chair; dining chairs count as seats only in a room without
one.

Groups go in order, largest first. After each group, the best partial layouts,
measured with the same checker as validation, go on to the next group. The kept
layouts differ in their zone plan first: the wall the bed, sofa, TV, or desk
backs onto (or a divider TV) and the room quarter that holds the dining table.
So the final layouts are different rooms, not copies that differ in one shelf.
When the zone plans do not fill the beam, the kept layouts then differ in where
the wall pieces stand: a wardrobe or dresser on another wall instead of the same
one slid along it.

A dining table in a dining room prefers the room center. In a studio or living room it prefers a wall (0.1 per metre away),
so it takes its own part of the room instead of the middle.

Lamps, tabletop items, and wall art are added last with the seed rules, and the
complete layout with the fewest findings wins. A solve takes 0.1 to 1.5 s on the
test rooms (up to 15 items).

Place makes no model call. An item the solver cannot place keeps its
least-violating pose, and findings go to the final review (4d).

The solver ends with up to 4 complete layouts, each with its plan: the zone
plan, then the wall each wardrobe, dresser, bookcase, or other wall piece backs
onto. Place keeps the result and the others that tie its issue score, with no
more unplaceable items. Each design solves its own products, so the designs take
layouts first come, first served: the best tied layout whose zone plan no other
design of the run has taken, else the best whose full plan none has taken, else
the best. So the designs differ in where the bed, sofa, or dining table stands,
and when the main piece fits only one wall, in where the storage stands. A
design never gets a worse layout to be different: every tied layout has the same
score. When a design repeats another's zone plan, the run record notes
`repeats the main layout of variant <n>`.
The record keeps each ready design's `plan`, and `scripts/benchmark.py` prints
how many distinct plans each run produced.

The layout is then normalized: wall items snap to walls, rugs fit the room, and
displays sit on their supports. It is then analyzed into findings by severity:

- **P0:** physical problems such as overlaps, items outside the room, and
  blocked doors.
- **P1:** placement problems such as wall items off the wall and blocked
  walking paths.
- **Critical P2:** function problems that block a layout, such as a broken
  seating group.
- **Other P2:** quality notes that do not block, such as a piece taller than
  the sill in front of a window, or a dining light more than 0.90 m above the
  table.

### 4d. Review, check, and send

`variant_stages.finish` gives every design a final review, then checks and
delivers it:

Comfort and taste live in `DESIGN_RULES` as plain text, so a new rule is one
line and needs no code. Code keeps only the hard geometry checks (overlaps,
inside the room, doors, walkways, TV and stand), and rejects any adjustment that
breaks them.

1. **Review.** One model call (stage `finish`, low thinking unless
   `LLM_STAGE_MODELS` sets it) gets the request, the requested items and style
   hints, the layout rules for the room type with their definitions, the
   coordinate system, the room boundary, doors, windows, and protected paths,
   each piece's key, category, size, and mount type, and the design's layout
   drawn top-down with its poses and measured facts: the
   findings the checks still report (issue, items, gaps and limits), and
   edge-to-edge gaps from sofa and lounge chairs to the coffee table, floor
   lamps to their seat, and the TV viewing distance. Acting as an interior
   designer, the model reviews the layout as a room against the
   design rules (`DESIGN_RULES` in `app/rules/layout_rules.py`: shared rules such
   as clear zones, a TV whose back does not face another zone, nothing tall in
   front of a window, a floor lamp at the seat it lights, plus rules per room
   type, such as a bed seen from the door but not in line with it, and a
   studio's bed in the corner farthest from the door, its sofa back or a shelf
   between sitting and sleeping, and dining by the entry wall), writes two or three sentences on
   what is wrong with it, and returns new poses that fix those problems. It
   does not choose among layouts: place already dealt each design its own.
2. **Adjust.** Code rejects poses for unknown items, with non-finite values, or
   with a rotation that is neither a whole number of quarter turns from the
   current one nor aligned with the room axes. Moves of any length
   are allowed. The rest are applied together, moving supported items with
   their supports, and kept if the issue score does not get worse. Otherwise
   each move is applied in the model's order on top of the moves kept, and kept
   if the score does not get worse. A failed call keeps the design's solver
   layout. The run record notes the review and the adjustments kept, reverted,
   or rejected. The `node_complete` data carries `review`.
3. **Check.** The final layout check runs: every selected unit is placed
   exactly once and no blocking finding remains. When it fails, code removes the
   items it fails on, one named item a round, choosing the removal that leaves
   the fewest findings. Items resting on a removed item go with it. It never
   removes the anchor (bed, sofa, or dining table): findings that name only the
   anchor stay, and the design is delivered with them. For example, a chair
   stuck inside the dining table is removed and the room keeps three chairs.
4. **Send.** Finish builds the render manifest (model references and
   placements), the selected-asset list, and the total cost. The graph then
   renders a top-down preview in a worker thread and sends `variant_ready` with
   its `preview_url`.

Failed checks and a failed review never fail a variant. A variant sends
`variant_failed` only when a stage raises, such as a selection model call that
used all its attempts.

The viewer can show each variant as soon as its `variant_ready` event arrives.
The preview uses product footprints without downloading images. The service
saves it beside the run record and serves it through
`GET /runs/{run_id}/previews/{index}.png`. Rendering and image write failures
leave the design ready with a null URL. The record keeps the render time and error.
Benchmark reviews use the same renderer in `app/preview.py`.

### Models

Three stages call a language model: interpret, select (each turn), and finish.

- **Default:** `gemini-3.8-flash` at low thinking for every stage
  (`LLM_DESIGN_MODEL`). `LLM_STAGE_MODELS` sets a model and level per stage, for
  example `select=gemini-3.5-flash-lite:minimal`.
- **Other models:** a model whose name does not start with `gemini`, such as
  `claude-haiku-5-5`, is called through the OpenAI-compatible AI proxy
  (`AI_PROXY_BASE_URL`, `AI_PROXY_API_KEY`). The stage level is sent as
  `reasoning_effort`. These calls record tokens but no cost. Haiku was tested on
  2026-10-11 and is about twice as slow (see the
  [report](report-2026-10-11.md)), so Gemini stays the default.
- **Retries and duplicates:** each call has a 60 s timeout and up to 3 attempts.
  A call still running after `MODEL_HEDGE_AFTER_S` (default 12 s; 0 turns it off)
  gets a duplicate, and the first answer wins. Turn it off for slower models
  such as Haiku.

Every setting is listed in `.env.example`.

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

The browser posts when each shown design arrived, finished loading models, and
drew its first frame after loading to `POST /runs/{run_id}/client-timing`.
Times are milliseconds since submission. Model counts, failed model counts,
and the screen name give context. The record keeps these in `client_timing`,
whether they arrive during generation or after it finishes.

Continuing with a ready design disconnects the stream. The service cancels the
unfinished variants and saves a `cancelled` record with ready variants and
browser timing retained. Record writes and timing submissions share a lock so
completion or cancellation cannot overwrite an accepted timing entry.

## Limits

| Limit | Value |
|---|---|
| Variants | 3 |
| Selection turns per variant | 4 |
| Final review calls per variant | 1 |
| Model call timeout | 60 s, up to 3 attempts |
| Jev call timeout | 5 s, up to 2 attempts |
| Run deadline | 300 s |
| Budget target | 85 to 95% of the stated budget; code fills a passing selection under 80% toward 90% |
| Budget allowance | 110% of the stated budget |
| Design-only decor | as many as the room size and brief suit |
| Retail decor with leftover budget | up to 2 pieces per design |
| Search fetch per slot | 30 per category, 30 kept |

## Not in this phase

- Saving designs, design-create payloads, and the chat reply. The browser still uses its local save mock.
- Supabase upload and billing fields.
