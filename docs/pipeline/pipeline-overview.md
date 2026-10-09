# From a prompt to a furnished room

The pipeline turns a prompt, room geometry, and budget into a selection of
catalog furniture and decor, with a position for each item. The browser loads
their existing 3D models to display the room.

This document covers new designs and full-room redesigns. Individual edits
run a smaller set of stages. Settings and behavior were checked on October 7, 2026.

## Generation flow

Generation follows this sequence:

1. Interpret the prompt and load the room's dimensions, boundary, doors, and windows.
2. Build a catalog of candidates that match the request and room.
3. Generate three variants in parallel, each with its own selection and layout.
4. Emit `variant_ready` as each variant passes validation. The user can choose
   one while the others continue.
5. Save the chosen design or apply it to the existing design. The browser uses
   its render manifest, which contains model references and placements.

Each variant runs these stages in order:

| Stage | Work |
| --- | --- |
| Selection | The model chooses furniture, decor, and quantities, then submits them for validation. |
| Preview download | Fetch missing top-down images for layout previews. |
| Initial placement | Give the model a rough layout seed. It returns each item's position, rotation, and supporting item, if any. |
| Repair | Measure violations and request corrections, with up to eight proposals. |
| Refinement | Request aesthetic changes and check them against the repaired layout. |
| Final validation | Check the final layout and build its render manifest. |

In the reviewed local runs, selection, initial placement, and repeated repairs
took most of the time. Preview downloads occasionally delayed placement.

## Selection controls

The selector chooses items from a filtered catalog. Candidate limits determine
its choices. Validation determines whether a proposed selection can proceed.

### Available candidates

[Catalog filtering](../livinit_pipeline/src/nodes/rag_scope_assets.py) retrieves
up to 1,000 similarity matches and builds a pool with a base limit of 20 items
per category. It also fills gaps from the catalog and reserves space for brand
preferences and popular products, so the final pool can exceed that base limit.

Retrieval reserves space for up to four decor candidates per category. This
controls available choices, not how many decor items the model must select.

### Selection checks and preferences

The [selection validator](../livinit_pipeline/src/nodes/asset_selection/validation.py)
checks budget, counts, required items, and fit. The
[selection agent](../livinit_pipeline/src/nodes/asset_selection/agent.py)
adds prompt guidance and model audits.

| Control | Current behavior |
| --- | --- |
| Budget | Cap spending at 110% of the stated budget. There is no minimum spend. Decor marked non-shoppable contributes zero, and TV prices are excluded. |
| Counts | Enforce requested counts and room-dependent category caps. Explicit user counts can override the caps, but must still pass fit checks. |
| Density | Estimate load as width × depth × 2 per item, excluding rugs. Reject load above the available furnishing area. Fresh living rooms and bedrooms also have a minimum load unless a fit decision was accepted. |
| Physical fit | Check rug dimensions, supporting surfaces, TV and stand compatibility, furniture groups, and restrictions for small rooms. |
| Required items | Apply room-specific requirements, such as one complete bed in a bedroom or a table and chairs in a dining room. |
| Attributes | Audit required style, material, color, and coordination constraints. An independent model audit checks style coherence once and can request a correction. |
| Preferences | Treat brands as preferences. Favor popular products when other requirements are equally satisfied. |
| Variant direction | Give variants different instructions, such as warm, rounded furniture or compact furniture with a cooler palette. |

Each selection turn submits an exact item list to `validate_selection`.
Failures return to the model for correction. Selection allows up to eight
turns, separate from the eight-proposal layout repair limit. Passing selection
does not guarantee that the resulting layout will pass.

### Settings and their sources

Most controls are code constants:

| Source | Controls |
| --- | --- |
| [rag_scope_assets.py](../livinit_pipeline/src/nodes/rag_scope_assets.py) | `TOP_K=1000`, `MAX_ASSETS_PER_CATEGORY=20`, `_DECOR_ITEMS_PER_CATEGORY=4` |
| [Selection constants](../livinit_pipeline/src/nodes/asset_selection/constants.py) | `BUDGET_FLEX_PCT=0.10` and required furniture roles |
| [fit_policy.py](../livinit_pipeline/src/core/planner/fit_policy.py) | Density thresholds and count caps by room size |
| [Selection contracts](../livinit_pipeline/src/nodes/asset_selection/contracts.py) | `MAX_TURNS=8` and thinking level |

[Model configuration](../livinit_pipeline/src/config.py) reads `LLM_DESIGN_MODEL`
first, then `LLM_PIPELINE_MODEL`, then the default, `gemini-3.8-flash`.
The inspected local environment overrides this with `gemini-3-flash-preview`.
The selector uses medium thinking for that model and low thinking for other
design models. The model name is an environment setting; this thinking-level
mapping is defined in code.

## How decor enters the room

The selector picks decor alongside retail furniture. Decor follows the same
placement, repair, validation, and rendering stages.

The catalog includes existing models from `pipeline.decor_items`, marked
`is_decor_item=true`. These items are non-shoppable. Their zero budget
contribution does not mean they are free products. Filtering makes plants,
sculptures, floor mirrors, and wall mirrors available unless excluded.

Fresh designs require at least one non-shoppable plant unless the room type is
`studio` or the interpreted request excludes planters. A retail planter does
not satisfy this rule. Missing the plant fails selection validation.

Other decor is optional unless requested. There is no single decor-quantity
setting: user instructions, category caps, space, available candidates, and
the model's choices determine the amount.

The [decor registry mapping](../backend/supabase/migrations/20260824000100_decor_soft_exclusion.sql)
assigns `wall_mounted` to wall mirrors and `floor` to everything else. Small
imported sculptures therefore do not automatically get tabletop placement.
Retail bowls and vases can receive `tabletop` classification and an `on_top_of`
relationship to selected furniture.

## How layout and furniture rules are enforced

The model proposes placements. Python code adjusts some placements, measures
violations, and decides whether a variant can finish.

### Rules supplied to the model

[layout_rules.py](../livinit_pipeline/src/core/layout_rules.py) supplies prompt
rules for coordinates, orientation, boundaries, access, and furniture
relationships. The model also receives dimensions, usable wall spans, and
seating-capacity measurements.

Room-specific rules take precedence. Bedrooms need bed access. Dining rooms
need chair pull-out space. Studios need sleeping, sitting, and dining groups
to fit together.

### Measured checks and blocking rules

[analyze_layout](../livinit_pipeline/src/core/layout/analysis.py) checks
coordinates against furniture dimensions, rotated footprints, boundaries,
and openings. Clearance measurements generally use gaps between item edges.

The checks use three priority levels:

| Level | Examples | Blocks a finished variant? |
| --- | --- | --- |
| P0: physical constraints | Overlaps, items outside the room, blocked doors, invalid wall or ceiling mounting | Yes |
| P1: alignment and protected paths | Invalid wall alignment or furniture inside a protected route | Yes |
| P2: function and composition | Bed access, chair access, sofa and coffee table spacing, rug placement, visual alignment | Only findings classified as critical |

[Layout constants](../livinit_pipeline/src/core/layout/constants.py) define
the levels and critical P2 findings. Inadequate dining access blocks completion.
A rug composition warning can remain. Prompt guidance also includes preferences,
such as visual balance, that have no measured acceptance check.

Examples of current measured requirements:

| Relationship | Requirement |
| --- | --- |
| Sofa and its coffee table | A gap of 0.46 to 0.61 m |
| Dining table and living-area seating or coffee tables | At least 0.76 m clearance |
| Dining chair access in dining rooms and studios | At least 0.56 m behind the full chair footprint |
| Bed access in bedrooms and studios | At least 0.56 m at the foot and along one side |
| Selected furniture | Every selected instance must appear exactly once in the final layout |

### Placement adjustments, repair, and final acceptance

[normalize_layout](../livinit_pipeline/src/core/layout/normalization.py)
can snap furniture to walls, fit rugs within the room, align displays with
supports, and calculate heights. Its adjustments depend on the stage and
options. During edits, it restores placements marked as frozen.

Repair gives the model item IDs, measured violations, and correction geometry.
It retains the best candidate, prioritizing physical constraints, critical
function, then other issues. Repair stops when blocking findings are clear or
the proposal limit is reached. Reaching the limit does not make a layout valid.

Refinement keeps the repaired layout if its proposed changes fail the checks.
[render_scene.py](../livinit_pipeline/src/nodes/render_scene.py) then checks
that every selected instance is present and revalidates fresh layouts.
Any remaining P0, P1, or critical P2 finding rejects the variant.

For example, one recorded layout left 0.484 m between a dining table and sofa,
below the 0.76 m requirement. Eight repair attempts did not resolve it.
Final validation rejected that variant while the other variants passed.

## Recorded run timings

These measurements cover the three most recent generation requests in
`livinit_pipeline/runs/`, all from October 6, 2026. All durations are seconds.
Run labels use request start time in Asia/Ho_Chi_Minh (UTC+7).

The generation totals include server work through request completion. They
exclude browser model downloads and display time. Variants run concurrently,
so their durations must not be added together to estimate user wait time.

### Request totals and shared stages

| Stage or milestone | Oct 06 20:33 | Oct 06 21:00 | Oct 06 21:14 |
| --- | ---: | ---: | ---: |
| Request setup | 2.35 | 3.68 | 3.90 |
| Room loading | 0.00 | 0.00 | 0.00 |
| Intent interpretation | 7.62 | 8.49 | 8.83 |
| Catalog phase | 18.77 | 6.15 | 4.64 |
| All variants, elapsed time | 307.00 | 138.92 | 144.46 |
| Response synthesis | 2.74 | 2.37 | 2.18 |
| Finalization | 0.00 | 0.00 | 0.00 |
| Completion payload | 0.02 | 0.02 | 0.03 |
| Run-event recording | 0.00 | 0.00 | 0.00 |
| Other overhead (remainder) | 0.03 | 0.07 | 0.03 |
| **Full request** | **338.53** | **159.71** | **164.07** |
| First variant ready, from request start | 220.93 | 123.20 | 118.47 |

Request setup includes authentication, saved design and history loading when
needed, and route planning. Catalog phase includes `rag_scope_assets` and its
orchestration overhead. Other overhead is the full request time minus the
listed phase durations. First-variant readiness is a milestone within the
request, not an additional duration. Values round to two decimals. `0.00`
can represent a measured duration below 0.005 seconds.

### Stages within each variant

Variant numbers use the log indices, 0 through 2. Variant elapsed time starts
after the shared catalog phase and includes small orchestration costs beyond
the listed nodes. Preview rows cover explicit preview nodes; previews created
inside selection, placement, or repair remain included in those stages.

#### Oct 06 20:33

Sources: [stage timings](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_203355_626411/meta/run_meta.json),
[request timing](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_203355_626411/meta/chat_end_to_end_timing.json).

| Stage | Variant 0 | Variant 1 | Variant 2 |
| --- | ---: | ---: | ---: |
| Furniture and decor selection | 120.87 | 42.65 | 47.04 |
| Preview-image download | 1.03 | 18.74 | 6.05 |
| Initial placement | 38.52 | 84.59 | 62.78 |
| Initial preview | 0.30 | 0.20 | 0.24 |
| Layout repair | 0.28 | 149.77 | 77.45 |
| Preview after repair | 0.22 | 0.22 | 0.19 |
| Refinement | 30.66 | 10.58 | 8.52 |
| Preview after refinement | 0.24 | 0.22 | 0.19 |
| Final validation and manifest | 0.01 | Not recorded | 0.01 |
| **Variant elapsed time** | **192.19** | **307.00** | **202.51** |
| Outcome | Ready | Failed | Ready |

#### Oct 06 21:00

Sources: [stage timings](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_210041_960509/meta/run_meta.json),
[request timing](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_210041_960509/meta/chat_end_to_end_timing.json).

| Stage | Variant 0 | Variant 1 | Variant 2 |
| --- | ---: | ---: | ---: |
| Furniture and decor selection | 44.76 | 45.04 | 38.45 |
| Preview-image download | 0.59 | 0.00 | 1.02 |
| Initial placement | 49.90 | 39.06 | 57.15 |
| Initial preview | 0.24 | 0.22 | 0.20 |
| Layout repair | 0.23 | 22.75 | 0.21 |
| Preview after repair | 0.18 | 0.18 | 0.18 |
| Refinement | 25.01 | 31.42 | 7.40 |
| Preview after refinement | 0.17 | 0.18 | 0.19 |
| Final validation and manifest | 0.01 | 0.01 | 0.01 |
| **Variant elapsed time** | **121.12** | **138.92** | **104.85** |
| Outcome | Ready | Ready | Ready |

#### Oct 06 21:14

Sources: [stage timings](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_211421_165029/meta/run_meta.json),
[request timing](../livinit_pipeline/runs/af2e3dc6-5d39-4ac5-b5e9-de29c8f1975e_20261006_211421_165029/meta/chat_end_to_end_timing.json).

| Stage | Variant 0 | Variant 1 | Variant 2 |
| --- | ---: | ---: | ---: |
| Furniture and decor selection | 32.86 | 33.48 | 33.05 |
| Preview-image download | 61.15 | 38.41 | 0.00 |
| Initial placement | 45.84 | 34.01 | 64.94 |
| Initial preview | 0.23 | 0.20 | 0.20 |
| Layout repair | 0.21 | 0.21 | 0.22 |
| Preview after repair | 0.19 | 0.18 | 0.19 |
| Refinement | 3.73 | 3.35 | 2.24 |
| Preview after refinement | 0.19 | 0.24 | 0.18 |
| Final validation and manifest | 0.01 | 0.01 | 0.01 |
| **Variant elapsed time** | **144.47** | **110.13** | **101.10** |
| Outcome | Ready | Ready | Ready |

The failed variant on October 6 at 20:33 reached final validation with an
unresolved clearance violation between living and dining furniture. Its `render_scene` duration was
not recorded as a completed node. The 307.00-second variant total includes
the failed attempt. The missing node duration is not treated as zero.

The slowest recorded stages were selection at 120.87 seconds, repair at
149.77 seconds, and preview-image download at 61.15 seconds. The first two
occurred in the 20:33 run; the download delay occurred in the 21:14 run.
This sample does not include production timing data.

### Phase 3 runtime, local runs

Four requests to the phase 3 service (`pipeline/`) on October 9, 2026, one per
room type, with `gemini-3.8-flash` and the local prepared catalog. Labels use
request start time in Asia/Ho_Chi_Minh (UTC+7). Run records are in
`pipeline/.data/runs/<run_id>.json`, which is gitignored. There is no request
setup, preview download, refinement, or response synthesis in this runtime. The
web app rejects these variants until phase 5, so the runs were sent to the API
directly.

| Stage or milestone | Living room 16:49 | Bedroom 16:50 | Dining room 16:51 | Studio 16:52 |
| --- | ---: | ---: | ---: | ---: |
| Intent interpretation | 8.60 | 8.00 | 6.54 | 8.31 |
| Room context | 0.01 | 0.00 | 0.00 | 0.01 |
| Retrieval by slot | 1.33 | 1.10 | 1.13 | 1.34 |
| First variant ready | 39.37 | 28.57 | 24.09 | 61.05 |
| **Full request** | **67.44** | **44.37** | **88.47** | **154.28** |
| Variants ready | 3 of 3 | 3 of 3 | 3 of 3 | 2 of 3 |
| Model calls | 10 | 8 | 10 | 18 |
| Model cost (USD) | 0.137 | 0.112 | 0.128 | 0.260 |
| Run ID | `b5b6ce6e…` | `af3cb293…` | `56cb72b9…` | `b1005cb4…` |

Within each variant, selection and correction show seconds, with the number of
turns or proposals in parentheses. Variant elapsed time starts when retrieval
ends.

| Run | Variant | Selection | Placement | Correction | Final validation | Variant elapsed | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Living room | 0 | 28.67 (2) | 6.68 | 3.60 (1) | 0.01 | 38.98 | Ready |
| Living room | 1 | 20.96 (1) | 8.40 | 0 | 0.01 | 29.39 | Ready |
| Living room | 2 | 14.64 (1) | 6.08 | 36.71 (1) | 0.01 | 57.47 | Ready |
| Bedroom | 0 | 23.46 (2) | 4.95 | 0 | 0.01 | 28.44 | Ready |
| Bedroom | 1 | 28.94 (1) | 6.27 | 0 | 0.01 | 35.25 | Ready |
| Bedroom | 2 | 13.25 (1) | 6.17 | 0 | 0.01 | 19.45 | Ready |
| Dining room | 0 | 10.94 (1) | 5.44 | 0 | 0.01 | 16.39 | Ready |
| Dining room | 1 | 14.86 (1) | 5.45 | 0 | 0.01 | 20.33 | Ready |
| Dining room | 2 | 11.43 (1) | 5.93 | 63.37 (3) | 0.01 | 80.77 | Ready |
| Studio | 0 | 14.39 (1) | 7.36 | 122.80 (8) | 0.01 | 144.60 | Failed: `layout_validation_failed` |
| Studio | 1 | 14.83 (1) | 6.17 | 53.54 (1) | 0.01 | 74.57 | Ready |
| Studio | 2 | 13.67 (1) | 6.44 | 31.22 (2) | 0.01 | 51.37 | Ready |

The failed studio variant used all 8 correction proposals and still had one
overlap and one media-group finding. In bedroom variant 0, the first selection
overcrowded the room, so the second turn used capped counts and dropped the
nightstands and lamps. The slowest model calls were corrections of 36 to 54
seconds, when thinking grew to several thousand tokens. These four runs are a
first check of the new runtime, not a speed or quality benchmark.
