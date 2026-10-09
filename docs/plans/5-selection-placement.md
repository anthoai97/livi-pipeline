# Phase 4: rebuild selection and placement

Issue: [#5](https://github.com/anthoai97/livi-pipeline/issues/5). Parent: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).
Status: stage 1 is implemented and benchmarked, and it misses the 60-second target. It is
checkpointed in draft PR [#9](https://github.com/anthoai97/livi-pipeline/pull/9), and its
results are in [phase 4 benchmark](../pipeline/phase-4-benchmark.md). Stage 2, the
[three-step flow](#stage-2-three-step-flow), is ready for implementation on branch
`issue-5-three-step-flow`. Phase 3 ([#4](https://github.com/anthoai97/livi-pipeline/issues/4)) is implemented.

Sections from "Objective" to "Deferred work" describe stage 1.

## Objective

Make each variant choose and place furniture with fewer and shorter model calls,
under the current rules, so that a request returns three valid layouts in under
60 seconds. Use Jev for fast bounded decisions, and record every option compared.

## Current behavior

Measured on the four phase 3 runs ([phase 3 local runs](../pipeline/phase-3-local-runs.md),
records in `pipeline/.data/runs/`):

- Full requests took 44 to 154 s. Interpretation (6.5 to 8.6 s) and retrieval
  (1.1 to 1.3 s) run before the variants. 11 of 12 variants were ready.
- The model writes about 130 output tokens per second.
- **Selection** (10 to 29 s per turn) reads about 18k input tokens and writes
  1.5k to 2k. Most of the output is per-item reasons, strategy text, and a
  self-audit (`Selection` in [variant_stages.py](../../pipeline/app/variant_stages.py)).
  Runs used at most 2 turns.
- **Placement** (5 to 8 s) places every item from scratch in one call, about 750
  output tokens.
- **Correction** is the tail. Calls take 3.6 to 54 s, and the slow ones think for
  up to 7k tokens. The failed studio variant used all 8 proposals. Six of them
  sent the same prompt, because a proposal that does not improve changes nothing.
- The validator's style and attribute self-audit comes from the model
  (`_selection_constraint_audit_errors` in [validation.py](../../pipeline/app/rules/selection/validation.py)).
  Code already checks sizes, vocabulary attributes, brands, prices, and counts.

Retrieval findings from phase 3 (issue comment):

- All three variants share one pool, ranked for a neutral query. Variant
  directions reach only the selection prompt.
- A slot with several categories leans to one category. Accent seating returned
  only swivel chairs, and lighting returned 5 floor lamps and 1 table lamp.
- Room-wide words reach only their own item. Optional and decor slots search
  with only the style hints.
- The design-only plant check runs after the 30-product fetch, so it could
  empty the slot.
- Selection sees ranked order only, not similarity scores.

Legacy code (`~/Code/freelance/livinit/codebase/livinit_pipeline`), all deterministic:

- `src/core/geometry/generation.py` builds a seed layout from
  `src/core/planner/seed_guidance.py`. It places anchors first: bed against a
  wall, nightstands at the bed head, coffee table in front of the sofa, TV on the
  wall opposite the sofa, chairs around the table, lamps beside seats. It
  reports items it skipped. Candidates assume a rectangular room and are only
  filtered by the room outline. The legacy fresh flow showed the seed to the
  model as a hint only (`src/nodes/layout_generation/initial_flow.py:213`).
- `src/core/layout/cleanup.py` moves P0 and path offenders to the nearest free
  point and arranges dining chairs. It has been unused since commit `f4d5d3a`.
- `src/nodes/layout_solver/operations.py:150` and `:199` clear protected paths
  and fix the sofa-to-table gap from validator vectors.
- Refinement (`src/nodes/layout_generation/refine_flow.py`) makes one model
  call with a preview image, two for studios, to fix composition. It took 2 to
  31 s per variant and is rolled back if findings get worse.

Already ported to `pipeline/app/rules`: geometry primitives, the living-group
poses, normalization (wall snap, rug fit, service-item slots), and validator
outputs that compute fixes (`overlap_separation_facts`, `translations_to_clear_m`,
sofa-table gap intervals, dining chair targets). Phase 3 puts these only in
prompts.

Jev (`typesafe-sdk`, [docs](https://docs.typesafe.ai/introduction)) answers
typed questions: choice, score, or yes/no with a probability. It takes text only,
answers in about 100 to 500 ms, and costs USD 0.042 per million input tokens.
Its docs list counting, arithmetic, and multi-step reasoning as weak points, and
a bias toward the first option.

## Scope

In scope:

- Per-category lookups and a design-only filter in retrieval.
- Jev ranking of each slot's candidates for each variant direction.
- Shorter selection output, with style and non-vocabulary attribute checks
  done by Jev and the rest done by code.
- Seed placement and model edits, code repair, capped correction, one
  reselection per variant, and a Jev-gated refinement pass.
- New limits, a benchmark script, and a results page that compares each option.

Non-goals:

- Interpretation speed. It is a shared stage and stays as is (about 8 s).
- Moving intent fields to Jev, and Jev decisions on which optional slots to include.
  Both are recorded options for later.
- Previews, images, the web app connection, and further variant distinctness
  (phase 5).
- A wider prompt set and replacing production (phase 6).

## Options and choices

Each decision lists every option considered. "User" means the user chose it.
"Planner" means the user delegated the choice. The benchmark switches let later
runs compare the options.

| # | Decision | Options | Chosen | By | Compare later |
| --- | --- | --- | --- | --- | --- |
| 1 | Placement seed | (a) Seed as a hint, model places all. (b) Seed is the starting layout, model returns only changed poses and poses for skipped items. (c) Seed is the layout, model places only skipped items. | (b) | User | Run (c) against (b) on the benchmark: time, valid layouts, non-blocking findings. |
| 2 | Repair | (a) Code fixes first, then capped model correction. (b) Model only, with tighter limits. | (a) | User | Compare with code repair turned off. |
| 3 | Selection output | (a) Model returns IDs, functional groups, and gap notes. Checks move to code and Jev. (b) Keep the legacy response. | (a) | User | Not switchable; compare with phase 3 run records. |
| 4 | Jev uses | (a) Rank slot candidates. (b) Style fit to the anchor piece. (c) Refinement trigger. (d) Attributes outside the prepared vocabulary. (e) Which optional slots to include. (f) Intent fields. | (a), (b), (c), (d) | User: a to c. Planner: d | Each chosen use on and off through `JEV_USES` and `REFINEMENT`. (e) and (f) deferred. |
| 5 | Retrieval fixes | (a) Search text per variant direction. (b) Search each category of a slot separately. (c) Add room-wide colors and materials to every slot. (d) Design-only filter in the query. (e) Pass similarity scores to selection. (f) Jev ranks a shared fetch toward each direction. | (b), (d), (f) | Planner | If Jev ranking loses, try (a) and (c) with Jev off. |
| 6 | Speed target | (a) Under 60 s with three valid layouts on the benchmark. (b) Report gains only and leave the gate to phase 6. | (a) | Planner | None. |
| 7 | Refinement | (a) Leave it out. (b) Always run. (c) Run when Jev says the layout needs it. (d) Run when code finds non-blocking composition findings. | (c) built. Default `off` after the benchmark | Planner | `REFINEMENT=off`, `always`, or `jev`. `jev` gave the fewest non-blocking findings, but the target failed with every option. |
| 8 | Correction thinking | (a) Keep `LOW`. (b) `MINIMAL` for placement edits, correction, and refinement. (c) A thinking budget (0 or 512 tokens). | (a), after (b) and (c) failed | Planner | Implementation check: `gemini-3.8-flash` rejects `MINIMAL`. Budgets 0 and 512 did not cut thinking (3 runs each: 1,015 to 1,972 thought tokens against 1,031 to 1,214 for `LOW`). Retry if a later model supports a lower level. |
| 9 | Correction stop | (a) Up to 3 proposals, stop at the first that does not improve. (b) Up to 3, stop after 2 that do not improve. | (a) | Planner | Count failures that (b) might have fixed. |
| 10 | Different products per variant | (a) Deal each slot's ranked candidates before selection: an exclusive share per variant plus shared fill, with a selection check on the shared share. (b) Variants select one after another, each excluding earlier picks. (c) Reselect when variants overlap. | (a), with `PRODUCT_REUSE_RATE` default 0.5 | User: distinct products, at most 50% reuse, configurable. Planner: (a) | (b) serializes selection, and (c) adds turns. (a) adds no model call; a limit violation costs one selection turn. |

Planner rationale:

- **4d.** Trimming the selection output removes the model's audit of attributes
  such as "pet-friendly" or "washable". Code cannot check these, and a yes/no
  question on each selected item fits Jev.
- **4e.** Ranking already shortens each slot's list. Dropping whole optional
  slots risks removing items the model would use. Defer it until the
  benchmark shows the prompt is still too long.
- **5b.** Every category lookup reuses the slot's query vector, so it adds no
  embedding call.
- **5d.** A design-only plant slot must never be emptied by purchasable results.
- **5f.** Jev ranks against the full brief and the direction, which already
  carries the room-wide words (5c) and the variant direction (5a). It needs no
  extra embeddings and is the comparison issue #1 asks for. With Jev off, the
  pool is phase 3's. 5e is superseded, because selection sees Jev's order.
- **6.** Phase 4 owns the variant time, which is where phase 3 lost it. The
  estimated critical path is in [Time budget](#time-budget).
- **7c.** The issue picked the Jev trigger. Refinement stays enabled only if it
  reduces non-blocking findings and the target holds. Otherwise the default is
  `off`.
- **8 and 9.** The tail came from long thinking and from repeating unchanged
  prompts. Code repair now handles the measurable fixes first. Lower thinking
  was not available (decision 8), so the proposal cap and stop rule bound the tail.

## Deliberate rule changes

Recorded for #1:

- The selection self-audit is no longer produced by the model. Code builds it from
  count and attribute checks. Jev adds style fit to the anchor and non-vocabulary
  attributes. This brings back the style check that phase 3 dropped.
- Selection no longer writes per-item reasons or strategy text
  (`summary`, `higher_budget_additions`, `lower_budget_savings`, `conflict`).
  Gap notes stay.
- Code derives `fit_satisfaction` from slot membership: a product taken from a
  requested slot satisfies that slot's category.
- With Jev ranking, each list keeps 6 products per requested or required
  slot, 4 per optional slot, and 3 per decor slot (phase 3: 10, 6, 4).
- Limits: 4 selection turns per variant (from 8), counted across reselection.
  3 correction proposals (from 8). 1 reselection.
- Placement starts from the rule seed. Refinement is text only, because no
  stage has preview images until phase 5.
- Final validation (`final_layout_check`) does not change.

## Design

### Flow

```text
START - interpret - room - retrieve - rank - Send x3 - variant - END

variant:
  select (loop, up to 4 turns) - place - repair - correct (loop, up to 3)
       - refine (when triggered) - validate
  validate fails and no reselection yet -> select (with placement feedback)
```

### Retrieval

- `search_assets` gets two options. `per_category=True` returns up to `limit`
  products per category in one query, ranked by distance within each category.
  `design_only=True` keeps only non-purchasable products.
- `retrieve` makes one embedding per slot as today. It runs one per-category
  lookup per slot and runs the design-only plant slot with `design_only`. It
  merges categories round-robin and applies the existing checks after the
  search. The pool keeps up to 30 eligible products per slot, unranked, in
  `shared["pool"]`.
- Gaps, relaxed required slots, and the run record behave as in phase 3.

### Rank (Jev, shared stage)

- Rank runs once per request, after retrieval, for all three directions. One
  Jev request per slot and direction. The state holds the brief
  (normalized prompt, style hints, room type), the direction, and the slot
  label. The request asks one yes/no question per candidate, with the
  candidate's prepared fields as text: title, category, colors, materials,
  styles, description, and price.
- Candidates are sorted by probability for each direction. `PRODUCT_REUSE_RATE`
  (default 0.5) is the largest share of a variant's distinct products that other
  variants may also use. Each slot of list size K is dealt in two steps:
  1. Exclusive: each variant takes ceil(K x (1 - rate)) products in turn, its
     highest-ranked product that no one holds yet, with the first pick rotating
     each round. An exclusive product never appears in another variant's pool.
  2. Fill: each variant tops up to K with its highest-ranked remaining products.
     These may overlap and are marked shared.
- If a slot has fewer than 6 eligible products, every variant gets that slot's
  full ranked list. The run record notes it, and these products do not count
  toward the limit.
- Each variant's `pool` replaces the shared pool in its selection prompt. The
  prompt marks shared products and states the limit. Select rejects a selection
  whose shared distinct products exceed floor(rate x distinct products), with a
  named error, and the model retries. Rate 0 gives fully distinct pools; rate 1
  gives the old overlapping lists and no check.
- When Jev is off or fails, the deal uses embedding order and phase 3 sizes. A
  failure is noted in the run record.
- Requests: about 15 slots for each of 3 variants, so about 45 at once, under Jev's
  80 requests per second limit.

### Select

- `Selection` becomes `selected_assets` (uid, functional_group) and `gaps`
  (string). The prompt drops the reason, audit, `fit_satisfaction`, and strategy
  instructions, including the guidance at `selection/fit.py:175`.
- After the model call, code builds `fit_satisfaction`. Jev checks the
  selection in one request:
  - Style fit. When an anchor is selected (sofa, bed, or dining table, as the
    prompt's anchor rule defines), ask for each other selected item: "does this
    match the anchor's style and palette?" Below 0.3 is a coordination violation.
  - Attributes. For each required `asset_attribute_constraints` entry that is not
    a brand and has no prepared-vocabulary filter, ask for each targeted item
    whether it satisfies the attribute. Below 0.5 is unsatisfied.
  - Thresholds are starting values to tune on the benchmark.
- `validate_selection` takes this audit in the shape it already reads, and
  still rejects on violations. With checks off, the audit holds only code
  results.
- On reselection, the prompt lists the items named in the remaining blocking
  findings and asks for smaller replacements or fewer items. The fit step moves
  to the next step.

### Place, repair, correct, refine

- **Place.** The ported seed (`build_seed_layout_plan`, `generation.py`,
  `placement.py`, the rest of `candidates.py`) builds the starting layout and the
  skipped list. The model gets the seed poses, the measured findings, and the
  phase 3 placement rules. It returns `Pose` entries only for items it moves and
  for skipped items. A response that leaves a skipped
  item unplaced gets one retry, as today. A second failure fails the variant with `variant_error`.
- **Repair.** Code applies the ported cleanup (`run_deterministic_p0_cleanup`,
  `run_deterministic_living_dining_cleanup`), then path clearing and the
  sofa-to-table gap fix from validator vectors. Each step is kept only if
  `layout_issue_score` improves. Repair makes no model call.
- **Correct.** Only the findings left after repair go to the model. Up to 3 proposals, and the loop stops at the first proposal that does
  not improve the score.
- **Refine.** It runs only when the layout has no blocking findings and
  `REFINEMENT` allows it. With `jev`, one question decides: does the layout need
  composition fixes, given the brief and the non-blocking findings as text? The
  legacy composition prompt (chairs facing their surface, lamps beside seats, TV
  focal axis, viewing distance) runs without an image and returns changed
  poses. It is rolled back if the score gets worse or a blocking finding
  appears.
- **Validate.** Unchanged. On failure with no reselection yet, the variant goes
  back to select. Otherwise it fails with `layout_validation_failed`.

### Jev client

- `pipeline/app/jev.py` wraps `AsyncTypeSafeClient`. It needs `JEV_API_KEY`
  and `JEV_MODEL` (pinned, not `latest`). It uses a 5-second timeout and 2
  attempts, and records latency, tokens, and cost per call in the run record.
- `JEV_USES` (comma list: `rank`, `check`) and `REFINEMENT` (`off`, `always`,
  `jev`) select the options. The run record stores the values used.
- A failed Jev call never fails a variant. That use falls back to its
  Jev-off behavior, and the run record notes it.

### Time budget

Estimated critical path with the chosen defaults: interpretation 8 s, retrieval
1.5 s, rank 0.5 s, selection 4 to 6 s per turn, selection check 0.5 s, seed
under 1 s, placement edit 2 to 4 s, repair under 1 s, correction 0 to 15 s,
refinement 0 to 5 s. One variant without retries takes about 20 to 40 s. One
reselection adds about 10 to 15 s. These are estimates until the benchmark runs.

## Implementation phases

1. **Retrieval.** Add `per_category` and `design_only` to `search_assets`, and
   use them in `retrieve`. Check: slot search tests and the retrieval report.
   Accent seating returns several categories, lighting returns both lamp
   types, and DB time stays under 500 ms.
2. **Jev client and rank.** Add `jev.py`, the switches, run-record entries, and
   the `rank` stage. Check: stage tests with a fake Jev client for sorting, sizes,
   and fallback.
3. **Selection.** Trim the schema and prompt, add the code-built
   `fit_satisfaction` and the Jev check, and adapt the validator input. Check:
   validator tests for style, attribute, and count failures.
4. **Seed and placement edits.** Port the seed modules, then change `place`.
   Check: parity tests against legacy seed output on the legacy fixtures.
5. **Repair, correction limits, reselection, refinement.** Port cleanup, add
   `repair` and `refine`, and set the new limits and graph edges. Check: graph
   tests for the stop rule, one reselection, and refinement rollback.
6. **Benchmark.** Run the four phase 3 requests 3 times each with the chosen
   defaults, then once per compared option. Write the results page and set
   the defaults from it.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | Add `rank`, `repair`, and `refine`. Trim `Selection` and its prompt. Build `fit_satisfaction` and the audit with the Jev check. Change `place` to seed plus edits. Correct with the findings that remain after repair. Add reselection feedback. | Owns all variant stages. |
| [pipeline/app/graph.py](../../pipeline/app/graph.py) | Edit | New variant edges and stop rules. `MAX_SELECTION_TURNS=4`, `MAX_CORRECTION_PROPOSALS=3`, `MAX_RESELECTIONS=1`. Add `rank`, `repair`, and `refine` to `Stages`. Add a per-variant `pool` to the state. | Owns the topology and bounds. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | Per-category lookups with round-robin merge, the design-only query for the plant slot, and an unranked pool of up to 30. Keep sizes move to `rank`. | Retrieval fixes 5b and 5d. |
| [product-data/src/search_assets.py](../../product-data/src/search_assets.py) | Edit | `per_category` and `design_only` options, plus CLI flags. | One owner for search filters. |
| `pipeline/app/jev.py` (proposed) | Create | Async Jev client wrapper, timeout and attempts, usage recording, switches, and fallback signal. | No module owns Jev. `llm.py` owns only Gemini generation. |
| [pipeline/app/run.py](../../pipeline/app/run.py) | Edit | Add the Jev client to the context, and Jev calls and switch values to the run record. | Run records drive the comparisons. |
| `pipeline/app/rules/planner/seed_guidance.py` (proposed) | Create | Port the legacy file. | Seed plan for placement. No ported file holds it. |
| [pipeline/app/rules/geometry/candidates.py](../../pipeline/app/rules/geometry/candidates.py) | Edit | Port the remaining candidate generators (wall, grid, centered, guided, preferred walls). | The seed needs them. The file already holds the support helpers. |
| `pipeline/app/rules/geometry/placement.py` (proposed) | Create | Port the legacy file. | Candidate scoring and search for the seed. |
| `pipeline/app/rules/geometry/generation.py` (proposed) | Create | Port `_generate_deterministic_layout` and its wrapper. | Builds the seed layout and skipped list. |
| `pipeline/app/rules/layout/cleanup.py` (proposed) | Create | Port `run_deterministic_p0_cleanup` and `run_deterministic_living_dining_cleanup`. Add path clearing and the sofa-to-table gap move from `operations.py:150` and `:199`. | Code repair before correction. Normalization covers different fixes. |
| [pipeline/app/rules/selection/validation.py](../../pipeline/app/rules/selection/validation.py) | Edit | `_selection_constraint_audit_errors` reads the code-and-Jev audit. Update its docstring. | The model no longer writes the audit. |
| [pipeline/app/rules/selection/fit.py](../../pipeline/app/rules/selection/fit.py) | Edit | Remove the `fit_satisfaction` response instructions. | Code derives it. |
| [pipeline/requirements.txt](../../pipeline/requirements.txt) | Edit | Add `typesafe-sdk>=0.7.3`. | Jev client. |
| `pipeline/scripts/benchmark.py` (proposed) | Create | Replay the benchmark requests N times against a running service, and summarize each run record: time, valid layouts, findings, cost, and switches. | Repeatable comparisons of each option. |
| `pipeline/scripts/benchmark_requests.json` (proposed) | Create | The four phase 3 requests, taken from their run records. | The run records are gitignored. |
| [pipeline/README.md](../../pipeline/README.md) | Edit | New environment variables and the benchmark command. | Setup. |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | Describe the new variant flow. | Keep the walkthrough current. |
| `docs/pipeline/phase-4-benchmark.md` (proposed) | Create | Results for the defaults and each compared option, with the chosen defaults. | Records the comparisons the user asked for. |
| [product-data/tests/test_search_assets.py](../../product-data/tests/test_search_assets.py), `pipeline/tests/` | Edit | Tests listed in the implementation phases. | Verification. |

## Failure paths

- Jev timeout or error: fall back for that use and note it. The variant continues.
- The seed skips items and the model does not place them after one retry: the
  variant fails with `variant_error`, as phase 3 does for malformed placements.
- Repair makes the layout worse: the step is discarded.
- Reselection finds no valid selection within the remaining turns: the variant
  fails with today's selection failure.
- The run deadline (300 s) and cancellation are unchanged.

## Acceptance criteria and verification

| Criterion | Verification |
| --- | --- |
| Every benchmark run (four phase 3 requests, 3 runs each, chosen defaults) returns three valid layouts in under 60 s. | Benchmark script and results page. |
| Final validation and the ported rules are unchanged. | Existing parity and stage tests pass. New parity tests cover the seed and cleanup ports. |
| A multi-category slot returns products from several categories, the plant slot is never emptied by purchasable results, and retrieval DB time stays under 500 ms. | Slot search tests and the retrieval report. |
| With Jev ranking on, the warm and cool variants get different top candidates for the same slot. |
| At most `PRODUCT_REUSE_RATE` (default 50%) of a variant's distinct products also appear in another variant, not counting slots with fewer than 6 eligible products. Rate 0 gives fully distinct variants. | Rank and select tests with fixed rankings, and the benchmark run records. | Benchmark run record of the living room request. |
| A style mismatch with the anchor, or a failed non-vocabulary attribute, rejects the selection with a named error. | Validator tests with a fake Jev client. |
| Correction makes at most 3 proposals and stops at the first that does not improve. A failed layout triggers one reselection before the variant fails. | Graph tests with fake stages. |
| Each chosen Jev use and refinement mode has measured time, valid layouts, findings, and cost on and off. | Results page. |
| A Jev failure never fails a variant. | Stage test with a failing fake client. |

## Dependencies and risks

- A Jev API key (`JEV_API_KEY`) is needed. Access is confirmed in #1.
- Jev's first-option bias and literal reading. Each yes/no question names one
  candidate, so option order does not apply. Thresholds are tuned on the benchmark.
- The seed assumes rectangular rooms. Non-rectangular rooms rely on skipped
  items and model edits. The benchmark requests are rectangular, so phase 6
  must include an L-shaped room.
- Thinking stays at `LOW` (decision 8), so a long-thinking correction call can still take up to about 50 s. The cap of 3 proposals and the stop rule bound it.
- Four requests are a small sample. Phase 6 verifies a wider set.
- Phase 5 (#6) lists a per-variant slot search text. Rank (5f) covers it,
  so #6 can drop that item when it is planned.

## Deferred work

- Jev decisions on which optional slots to include (4e) and on intent fields (4f).
- Per-variant search text (5a) and room-wide words in every slot (5c). Run these
  only if Jev ranking loses on the benchmark.
- Image-based refinement, after phase 5 adds previews.

## Stage 2: three-step flow

### Objective

The job is three steps: understand the prompt, pick products, and put them in
the room in a layout that makes sense. Do each step once, with the tool that
fits it, and stop asking the language model to do geometry.

### Why

Stage 1 per-step timing (12 runs, lite placement and correction, escalating to
`gemini-3.8-flash`) on the critical path, per run:

| Step | Seconds | Share |
| --- | ---: | ---: |
| correct (mostly escalated) | 42.3 | 54% |
| select | 20.8 | 26% |
| interpret | 10.3 | 13% |
| place, retrieve, rank, code | 5.5 | 7% |

Correction escalates on bedroom and studio relationships: bed access,
nightstands, TV viewing, and the media group. The lite model cannot fix them,
and `gemini-3.8-flash` thinks for 20 to 50 s to do it. The checker
(`analyze_layout`) takes 5.5 ms per layout, and the rule seed 146 ms. So code
can score about 150 candidate layouts in under a second.

### Flow

```text
1. understand  interpret (one model call)
2. pick        retrieve -> rank + deal (Jev) -> select (one model call)
3. place       solve (code) -> [fallback: swap, then model correction] -> validate
```

### Decisions

| # | Decision | Options | Chosen | Why |
| --- | --- | --- | --- | --- |
| 11 | Who places furniture | (a) Code solver scored by the checker, with model correction only as a fallback. (b) Keep seed, model edit, and model correction. | (a) | Geometry is arithmetic, and the checker already defines a sensible layout. |
| 12 | Unplaceable item | (a) Code swaps it for the next smaller product in its slot that stays within budget, and solves again. (b) Reselect with the model. (c) After swaps, drop one dining chair at a time. | (a), up to 2 swaps, then (c), up to 2 drops | Saves a selection turn of 5 to 25 s. The user approved (c); each drop passes the selection validator, so exact counts such as "seating for six" are never reduced. A swap or drop is kept only if the layout scores better. |
| 13 | Slow model calls | (a) Send a duplicate call when one is slow and keep the first answer. (b) Wait. | (a), after 8 s | 70 to 80% of calls answer in about 5 s, so the duplicate usually wins. |
| 14 | Switching over | (a) `PLACEMENT=solver` or `model`, compared on the benchmark, then the losing path is deleted. (b) Replace at once. | (a) | Keeps a comparison point, as the user asked. |

### Solver

- **Groups.** Seed guidance already assigns each item a role, an anchor, and a
  placement mode. A group is an anchor plus the items tied to it:
  - bed, nightstands, and bedside lamps
  - sofa, coffee table, accent chairs, side tables, TV, and media unit
  - dining table and chairs
  - desk and office chair
  - each storage piece on its own

  Each group gets a template of relative poses. These come from the existing
  living-group poses, the dining chair targets, and the seed's guided
  candidates.
- **Candidates.** Wall-aligned groups go against each wall segment, at steps
  along the wall, facing into the room. Floating groups, such as a dining table,
  go on a grid. Both orientations are tried.
- **Search.** Place the largest groups first. Keep the best few partial layouts,
  using cheap footprint, door, and path checks. Score each complete candidate
  with `analyze_layout` and `layout_issue_score`, and keep the best.
- **Accessories.** Rugs, lamps, plants, decor, and wall items are placed after
  the groups, with the existing service-slot, rug, and wall-mount
  normalization. The ported cleanups then run once.
- **Result.** The best layout, its findings, and the items the solver could not
  place. Rules and `final_layout_check` do not change.

### Fallbacks

1. The solver cannot place an item: swap it in code for a smaller product, then drop dining chairs one at a time if needed (decision 12), and solve again.
2. Blocking findings remain: run the stage 1 correction loop from the solver's
   layout, with lite correction escalating to `gemini-3.8-flash` when configured.
3. Validation fails: reselect once, as in stage 1.

### Implementation phases

1. **Duplicate slow calls.** `llm.py` sends a second identical call when the
   first has not answered after `MODEL_HEDGE_AFTER_S` (8 s). It keeps the first
   answer, cancels the other, and records both calls. Check: test with a slow
   fake transport.
2. **Solver.** Build the groups, templates, candidates, and search, and score
   with the checker. Check: on the parity fixtures and the benchmark rooms, the
   solver returns layouts with no blocking findings for living, dining, bedroom,
   and studio sets, in under 2 s.
3. **Wire it in.** `place` uses the solver when `PLACEMENT=solver`, with swap and
   correction fallbacks. Check: stage and graph tests with fakes.
4. **Benchmark.** 12 runs with `PLACEMENT=solver` against stage 1.
5. **Delete the losing path.** If the solver wins, remove the seed-plus-edit
   placement, the refinement stage and its Jev trigger, and the Jev selection
   check. Keep correction only as the fallback. Update docs.

### File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| `pipeline/app/rules/layout/solver.py` (proposed) | Create | Groups, templates, candidates, search, scoring, and swap support. | New code-based placement. No existing module searches whole layouts. |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `place` calls the solver and code swap under `PLACEMENT=solver`. Phase 5 removes the losing path and the refine and check code. | Stage wiring. |
| [pipeline/app/graph.py](../../pipeline/app/graph.py) | Edit | Routing for the swap and fallback. Phase 5 removes the refine node. | Topology. |
| [pipeline/app/llm.py](../../pipeline/app/llm.py) | Edit | Duplicate slow calls. | Caps thinking tails. |
| [pipeline/app/jev.py](../../pipeline/app/jev.py) | Edit | Read `PLACEMENT` with the other switches. Phase 5 drops `check` and `REFINEMENT`. | Switches. |
| [pipeline/app/rules/planner/seed_guidance.py](../../pipeline/app/rules/planner/seed_guidance.py), [pipeline/app/rules/geometry/candidates.py](../../pipeline/app/rules/geometry/candidates.py) | Edit if needed | Expose group roles and candidate generators to the solver. | Reuse the seed rules. |
| [pipeline/README.md](../../pipeline/README.md), [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md), [docs/pipeline/phase-4-benchmark.md](../pipeline/phase-4-benchmark.md) | Edit | New flow, switches, and results. | Docs. |

### Acceptance criteria

| Criterion | Verification |
| --- | --- |
| At least 11 of 12 benchmark runs return three valid layouts in under 60 s, with a median at or under 30 s. | Benchmark with `PLACEMENT=solver`. |
| Valid layouts are at least stage 1's 34 of 36. Non-blocking findings per run are reported against stage 1. | Same benchmark. |
| The solver alone clears blocking findings in most variants. The run record notes when a swap or the correction fallback ran. | Run-record counts. |
| A slow model call is capped by its duplicate. | `llm.py` test, and select call times in the benchmark. |
| Rules and final validation are unchanged. | Existing parity tests pass. |

## Unresolved questions

None for stage 2. Deferred: a shorter interpretation output, and Jev choosing
products per slot instead of the selection model call.
