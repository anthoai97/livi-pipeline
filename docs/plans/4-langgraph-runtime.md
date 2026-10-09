# Phase 3: build the LangGraph generation runtime

Issue: [#4](https://github.com/anthoai97/livi-pipeline/issues/4). Parent: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).
Status: ready for implementation. Depends on phase 2 ([#3](https://github.com/anthoai97/livi-pipeline/issues/3)):
embeddings and retrieval must exist first. Nothing in this plan has been built or run.

## Objective

Build a Python service in `pipeline/` that turns one `POST /pipeline` request
(prompt, budget, and room geometry) into three validated layouts through explicit
LangGraph stages. Shared work runs once, and each variant keeps its own state.
The run streams progress, records stage timing and model usage, stops when the
client disconnects, and bounds retries and failures.

This phase builds the runtime and ports today's rules. It does not try to meet
the 60-second target. Phase 4 rebuilds selection and placement for speed on top
of this runtime.

## Current behavior

The legacy flow lives in `~/Code/freelance/livinit/codebase/livinit_pipeline`.
Paths in this section are relative to that repository. It uses FastAPI and
asyncio, not LangGraph.

- `POST /pipeline` takes `PipelineRequest` (`src/api/models.py:9`). Every fresh run
  first calls a model to classify the route (`src/nodes/orchestrator_route_policy.py:938`).
- The intent call is synchronous inside async code, so it blocks the event loop
  (`src/core/planner/intent_understanding.py:405`).
- Shared stages: room loading, intent, `extract_room`, and `rag_scope_assets`.
  Three variant tasks follow (`src/api/execution/pipeline_stream.py:245`). Each runs
  selection, preview download, initial layout, repair, refinement, and final
  validation. Variants 1 and 2 differ only by a prompt directive
  (`src/nodes/asset_selection/agent.py:264`).
- The request waits for all three variants. There is no stage or variant timeout.
  Model calls have a 120-second timeout and up to five retries (`src/core/llm.py`).
- Nothing checks for a client disconnect. Cleanup depends on the stream generator
  closing (`pipeline_stream.py:487`).
- Timing goes to `runs/<id>/meta/run_meta.json`. Token cost is written per variant,
  but the reported run cost reads only the root file and leaves out variant costs
  (`src/api/sse.py:934`).
- Events: `start`, `node_start`, `node_complete`, `heartbeat`, `variant_ready`,
  `variant_failed`, `complete`, and `error`. Live model output streams only for
  variant 0 (`src/api/sse.py`, `src/nodes/node_events.py`).
- The web app requires `create_payload` on every uncommitted variant
  (`web-pipeline/lib/pipeline/variantsReady.ts:210`). It reads `render_manifest`,
  `selected_assets`, and `total_cost`.
- Recorded runs took 160 to 339 seconds ([pipeline overview](../pipeline-overview.md#recorded-run-timings)).

Most deterministic rule code can be ported directly. Layout analysis and
normalization are pure functions: about 4,200 lines in `src/core/layout/` plus
about 1,300 lines of support code. The selection validator is more coupled. It
reads the legacy state dictionary, can trigger the intent model through
`preflight.py`, and imports Supabase and model helpers. It needs a wrapper that
takes explicit inputs.

## Scope

In scope:

- A Python service in `pipeline/` using FastAPI and LangGraph (`langgraph>=1.2`).
- Shared stages: interpretation, room context, and retrieval. Variant stages:
  selection, placement, correction, and final validation.
- Ported rules and today's limits. Selection, placement, and correction are plain
  model calls checked by those rules.
- Today's request fields for geometry rooms. Today's event names, carrying the
  fields the viewer reads.
- A run record per request, cancellation, and bounds.

Non-goals:

- Meeting the 60-second target or a valid-completion target. Phases 4 to 6 own these.
- Jev, stricter candidate filtering, placement seeding, targeted repair, and
  reselection after a layout fails. These belong to phase 4.
- The refinement pass. Phase 4 can bring it back if quality requires it.
- More distinct variants than today's directives, previews, and the web app
  connection. These belong to phase 5.
- `create_payload`, `turn_payload`, authentication, Supabase uploads, billing, room saving,
  and the chat reply.
- Scanned-room input (`usdz_path`, `split_splat_import_id`) and design edits.
- Fit confirmation. The initial flow never asks the user. See the rule changes below.
- Route classification. The endpoint only handles initial generation.
- LangGraph checkpointing. A run is short and is not resumed.

## Decisions

| Decision | Rationale |
| --- | --- |
| Use Python for the whole pipeline. | Jev's SDK is Python only. LangGraph's per-node timeouts and error handlers are Python only. The legacy rule code is Python. |
| Port the rules. Selection, placement, and correction are plain model calls checked by those rules. | Rules carry over to phase 4 unchanged. Phase 4 replaces only the stage internals, so no legacy agent loop is ported only to be rewritten. |
| Keep today's limits: 8 selection turns, 8 correction proposals, 110% budget allowance, category caps, density, and required items. | Preserves the current rules. Phase 4 tunes the limits with measurements. |
| Use `gemini-3.8-flash` through `google-genai` with low thinking, set by `LLM_DESIGN_MODEL`. | Today's production model, so comparisons isolate runtime changes. |
| Produce viewer fields now. Saving fields and the chat reply wait for phase 5. | `create_payload` needs a saved room ID and the web connection, which phase 5 owns. Until then the web app rejects these variants, so phase 3 is checked through the API directly. |
| Leave out refinement and Jev. | Refinement is not in the issue's stage list. It took 2 to 31 seconds per variant in recorded runs. Phase 4 benchmarks each Jev use. |
| Reuse `product-data/src` for retrieval and Gemini prices. | One owner for embedding settings, filters, and prices. `pipeline/app/__init__.py` adds `product-data/src` to the import path, as `prepare_assets.py` already does for `gemini_usage`. |
| Map the placement mode from prepared `placement_type`. | Replaces the legacy placement-classifier model call. `floor` stays `floor`, `surface` becomes `tabletop`, `wall` becomes `wall_mounted`, and `ceiling` becomes `ceiling_mounted`. |

Deliberate rule changes, recorded as #1 requires:

- The candidate pool has no reserve for popular products, because prepared records
  have no popularity data. The brand-preference reserve stays.
- The separate model audit of style coherence during selection moves to phase 4,
  where Jev is a candidate for it. Deterministic attribute checks against prepared
  fields stay.
- Purchasable products with an unknown price are not candidates, because they
  cannot count against the budget. This follows the phase 2 rule that unknown
  values cannot satisfy strict requirements. Design-only decor stays eligible
  and counts zero.
- No fit confirmation. Today the flow can pause to ask the user to accept furniture
  that overcrowds the room (`src/nodes/asset_selection/agent.py:80`,
  `src/nodes/asset_selection/fit.py:455`). Phase 3 never asks. It makes the fit
  choice itself, using the two decisions the legacy code already supports:
  1. **Smaller furniture first.** The model keeps the requested items and counts
     and picks more compact products, judged by their real dimensions. This is the
     legacy automatic-continue guidance (`fit.py:90`).
  2. **Fewer pieces next.** If a second selection still fails a fit check, the
     requested counts are capped at the counts the fit estimate says will fit.
     This is the legacy `use_recommendation` decision (`fit.py:53`). Lower-priority
     optional furniture goes first, functional groups stay complete, and required
     items such as the bed stay. The minimum-load rule is then waived, as it is
     today after an accepted fit decision.

  The variant fails with `asset_selection_failed` only if no selection passes
  within the 8 turns. The run record notes which fit step was applied.

## Runtime design

### Graph

```text
START ─┬─ interpret ─┬─ retrieve ── Send x3 ── variant ── END
       └─ room ──────┘

variant subgraph, one per Send:
  select ── place ── correct (repeats up to 8 times) ── validate
```

The parent state holds the request, intent, room context, candidate pool, and
`variants`, a dictionary keyed by variant index that a reducer merges. Each
`Send` passes the variant index, its direction text, and the shared values. The
variant subgraph state holds only values the variant owns: selection, selection
feedback, layout, findings, attempt counts, and outcome. Variant code copies any
shared record before it changes it.

`variant` is a parent node that runs the compiled subgraph and returns
`{"variants": {index: result}}`. It catches every exception except cancellation
and returns a failed result. LangGraph applies parallel branches all or nothing:
if one branch raises, the other branches' updates are discarded
([LangGraph docs](https://docs.langchain.com/oss/python/langgraph/use-graph-api#run-graph-nodes-in-parallel)).
Catching the exception keeps one failed variant from dropping the others.

`variant_ready` and `variant_failed` go out through the LangGraph stream writer
as soon as each variant finishes. They do not wait for the other variants.

### Stages

| Stage | Work | Model calls |
| --- | --- | --- |
| interpret | Port only the new-design prompt and schema from `intent_understanding.py`, plus the deterministic cleanup in `intent_packet.py`. Drop the edit branch (`route`, `current_assets`). Also drop the fields that only edit, fit-confirmation, or chat-reply code reads: `ambiguity_level`, `lifestyle_requirements`, `material_preferences`, `fabric_preferences`, `budget_strategy`, `rationale`, and `support_pairs`. Output: requested, excluded, and avoided categories, item counts, price and attribute constraints, style and function hints, fit flexibility, and the circulation-path flag. The ported rules check selections against these. | 1 |
| room | Port `room_facts.py`, `feasibility_digest.py`, `room_policy.py`, `fit_policy.py`, and `fit_check.py`, and run them on the request geometry and the interpreted counts. Output: walls, openings, blocked zones, protected paths, density budget, category caps, room scale, and a fit estimate with the counts that fit. | 0 |
| retrieve | Build one semantic query from the intent and call the phase 2 retrieval function. Exact filters: category allowed for the room and not excluded, model URL, all three dimensions, known placement, and a known price for purchasable items. Keep up to 1,000 matches, then at most 20 per category and 4 decor items per category. Run a category-filtered query for each required category with no match and for each brand preference. | Query embedding only |
| select | The model picks items and quantities from the pool, given the intent, room context, budget, and variant direction. The ported validator checks budget, counts, density, required items (including the non-shoppable plant), fit, and attributes. Failures go back to the model for up to 8 turns. When the fit estimate shows the request overcrowds the room, the model is told to pick compact products from the start. A fit failure then leads to smaller products first, and to capped counts on the next fit failure. | 1 per turn |
| place | The model returns position, rotation, and `on_top_of` for every instance, guided by the ported prompt rules in `layout_rules.py`. Ported `normalize_layout` and `analyze_layout` then run. | 1 |
| correct | While P0, P1, or critical P2 findings remain, send the measured findings and current poses to the model. Apply the returned poses, then normalize and analyze again. Keep the best candidate: fewest physical findings first, then critical function, then the rest. Stop after 8 proposals. | 1 per proposal |
| validate | Port the final check from `render_scene.py`: each selected instance appears exactly once, and no P0, P1, or critical P2 finding remains. Build the render manifest, then emit `variant_ready`, or emit `variant_failed` with `layout_validation_failed`. | 0 |

Record fields map to the ported code as follows:

- Instance keys are `<category>_<n>`, such as `sofa_1`, with `asset_id` stored
  separately. Ported category checks also match keywords in the instance key
  (`src/core/categories.py:235`). Categories pass through the ported
  `normalize_category`.
- `is_decor_item` is true when `source_table` is `pipeline.decor_items`.
  Non-purchasable items count zero toward the budget. TV prices stay excluded.
- Variant directions use the text of `_VARIANT_DIRECTIVES` unchanged: no
  direction for variant 0, soft and warm for variant 1, compact and cool for
  variant 2, plus the room-specific versions.
- No stage downloads images or calls Supabase during a request.

### Request and events

`POST /pipeline` accepts `user_intent`, `budget`, `room_type`, `room_area`,
`room_vertices`, `wall_height`, `room_doors`, `room_windows`, and `wall_finishes`.
This prototype drops `upload_to_supabase` and `billing_operation_id`, which the
web proxy forwards today (`web-pipeline/app/api/pipeline/route.ts:17`). It
rejects them, `usdz_path`, `split_splat_import_id`, and any other unknown field
with HTTP 422. Phase 5 decides which of these fields the web connection needs.

The stream sends `data: {"type": ...}` frames under today's event names:

- `start` when the run begins.
- `node_start` and `node_complete` for each stage, with `node`, `variant_index`,
  and `elapsed`. The legacy `result` dump is dropped.
- `heartbeat` after 10 seconds without another event.
- `variant_ready`, whose `data` is exactly `{variant}`. The variant carries
  `variant_index`, `variant_id`, `committed: false`, `render_manifest`,
  `selected_assets`, `total_cost`, `selection_validation`, and
  `asset_selection_failed: false`.
- `variant_failed`, with `variant_index`, `reason`, `message`, and `errors`.
- `complete`, with `data.variants` holding the ready variants in arrival order.
- `error`, sent when a shared stage fails or the run deadline passes before any
  variant has started.

The stream never sends `fit_confirmation_required`.

Step 1 checks every field the web app reads from these events and adds any that
are missing.

The render manifest keeps today's shape (`src/nodes/render_scene.py:33`):
`room_area`, `room_vertices`, `room_doors`, `room_windows`, `wall_height`, `layout`
(each instance's `uid`, `instance_key`, `category`, `position`, and `rotation`),
and `assets` (`asset_id`, `uid`, `name`, `category`, `image_url`, `glb_url`,
`width`, `depth`, `height`, `placement_mode`, `is_decor_item`, `is_placeholder`).
Prepared records have no `frontView`, `center`, or `topdown_url`, so the manifest
omits them. Phase 5 adds them.

### Bounds, cancellation, and the run record

| Bound | Value | Today |
| --- | --- | --- |
| Model call | 60 seconds per attempt. At most 3 attempts, retrying only on rate-limit errors, server errors, and timeouts. | 120 seconds, up to 5 retries |
| Selection | 8 turns | 8 |
| Correction | 8 proposals | 8 |
| Run deadline | 300 seconds, the web proxy's `maxDuration`. Unfinished variants become `variant_failed` with reason `timeout`, and `complete` lists the ready ones. | None |

The bounds live as constants in `app/graph.py`. Model-call limits use the
`google-genai` `HttpOptions` timeout and `HttpRetryOptions`. When the client
disconnects, Starlette cancels the response task. The endpoint then cancels the
graph task, which cancels in-flight model calls. No new stage starts, and the
run record is written with status `cancelled`.

Each request writes one JSON run record to `pipeline/.data/runs/<run_id>.json`,
which is gitignored. The record contains:

- The request summary.
- Status: `complete`, `error`, `cancelled`, or `timeout`.
- One entry per stage, with stage name, variant index, start time, elapsed time,
  and outcome.
- One entry per model call, with stage, variant index, model, input, cached,
  output, and thought tokens, cost, elapsed time, and attempts.
- One entry per variant, with outcome, reason, and ready time.
- Totals: first ready variant, all ready, full run, and cost across all variants.

Phase 6 compares runs using these records.

## Implementation steps

1. **Service skeleton and contract check.** List every field the web app reads
   from each event and from the render manifest (`web-pipeline/services/ApiClient.ts`
   around lines 766 to 1010, `features/pipeline/usePipelineRun.ts`,
   `lib/pipeline/variantsReady.ts`, and `types/pipeline.ts:198`). Build the request
   model, event builders, the SSE endpoint, the run context and record, and the
   model client. Check: a fake graph streams the full event sequence.
2. **Port the rules.** Copy the legacy modules listed under File changes into
   `pipeline/app/rules/` and rewrite their imports. Remove model calls, Supabase
   access, and branches for other routes. Add a selection-validation entry that
   takes explicit inputs, and the placement-mode mapping. Check: parity against
   the recorded legacy runs.
3. **Shared stages.** Build interpretation, room context, and retrieval. Retrieval
   requires the phase 2 retrieval function.
4. **Variant stages and graph.** Build selection, placement, correction, and
   validation, plus the `Send` fan-out, failure isolation, and variant directions.
5. **Bounds and cancellation.** Add the call limits, loop limits, run deadline,
   and disconnect handling.
6. **Local runs.** Send one request per room type to the local service with the
   real model and the local catalog. Record timings next to the legacy runs.

## File changes

New paths are proposed. Legacy sources are under
`~/Code/freelance/livinit/codebase/livinit_pipeline/src/`. Port only what each
module's imports require. If porting needs another helper, add a row here.

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| `pipeline/app/graph.py` (proposed) | Create | State types and reducer. Graph wiring: interpretation and room in parallel, retrieval, `Send` x3, variant subgraph. Failure isolation and bound constants. | Owns the LangGraph topology. No runtime exists in this repository. |
| `pipeline/app/shared_stages.py` (proposed) | Create | Interpretation (prompt, schema, call), room context, and retrieval with pool limits. | Runs once per request. |
| `pipeline/app/variant_stages.py` (proposed) | Create | Selection loop, placement, correction loop, final validation, and variant directions. | Runs once per variant. |
| `pipeline/app/main.py` (proposed) | Create | FastAPI app, `POST /pipeline`, SSE framing, heartbeat, run deadline, cancellation on disconnect, and run-record write. | HTTP entry point for the runtime. |
| `pipeline/app/contracts.py` (proposed) | Create | Request model, event payload builders, and render manifest builder. | Keeps today's request and event shapes in one place. |
| `pipeline/app/run.py` (proposed) | Create | Run context: a stage timer that emits node events and records timing, model-call entries, variant outcomes, and the JSON record. | Timing, progress, and usage share one record. |
| `pipeline/app/llm.py` (proposed) | Create | Async Gemini structured-output call with timeout and attempt limit. Records tokens and cost per stage and variant, using `gemini_usage.estimate_cost`. | Every stage's model calls go through one bounded path. |
| `pipeline/app/__init__.py` (proposed) | Create | Add `product-data/src` to the import path. | Reuse the phase 2 retrieval function and the price table. |
| `pipeline/requirements.txt` (proposed) | Create | `langgraph>=1.2`, `fastapi`, `uvicorn`, `google-genai`, `psycopg[binary]`, `pgvector`, `shapely`, `numpy`, `pydantic`, and `python-dotenv`. Verify the versions at install. | Runtime dependencies. |
| [pipeline/README.md](../../pipeline/README.md) | Edit | Add the run command, environment variables (`GEMINI_API_KEY`, `LOCAL_CONNECTION_STRING`, `LLM_DESIGN_MODEL`), and run-record location. | Describes how to run the service. |
| `pipeline/app/rules/layout/analysis.py` (proposed) | Create | Port `core/layout/analysis.py`. | Layout findings by level (P0, P1, P2). |
| `pipeline/app/rules/layout/validation_functional.py` (proposed) | Create | Port `core/layout/validation_functional.py`. | Access and function checks. |
| `pipeline/app/rules/layout/validation_geometry.py` (proposed) | Create | Port `core/layout/validation_geometry.py`. | Overlap, boundary, and opening checks. |
| `pipeline/app/rules/layout/relations.py` (proposed) | Create | Port `core/layout/relations.py`. Import the placement mode from `rules/placement_mode.py`. | Furniture relationship checks. |
| `pipeline/app/rules/layout/metrics.py` (proposed) | Create | Port `core/layout/metrics.py`. | Measurements used by the checks. |
| `pipeline/app/rules/layout/dining.py` (proposed) | Create | Port `core/layout/dining.py`. | Dining chair access. |
| `pipeline/app/rules/layout/comfort.py` (proposed) | Create | Port `core/layout/comfort.py`. | Sofa and table spacing. |
| `pipeline/app/rules/layout/bedroom.py` (proposed) | Create | Port `core/layout/bedroom.py`. | Bed access. |
| `pipeline/app/rules/layout/studio.py` (proposed) | Create | Port `core/layout/studio.py`. Import the placement mode from `rules/placement_mode.py`. | Studio group fit. |
| `pipeline/app/rules/layout/constants.py` (proposed) | Create | Port `core/layout/constants.py`. | Levels and critical P2 findings. |
| `pipeline/app/rules/layout/normalization.py` (proposed) | Create | Port `core/layout/normalization.py`. | Wall snapping, rug fit, and heights. |
| `pipeline/app/rules/geometry/primitives.py` (proposed) | Create | Port `core/geometry/primitives.py`. | Footprint geometry. |
| `pipeline/app/rules/door_geometry.py` (proposed) | Create | Port `core/door_geometry.py`. | Door swing and clearance zones. |
| `pipeline/app/rules/protected_paths.py` (proposed) | Create | Port `core/protected_paths.py`. | Protected routes (P1). |
| `pipeline/app/rules/categories.py` (proposed) | Create | Port `core/categories.py`. | Category keyword matching. |
| `pipeline/app/rules/pipeline_shared.py` (proposed) | Create | Port `core/pipeline_shared.py`. | Shared helpers imported by the checks. |
| `pipeline/app/rules/room_policy.py` (proposed) | Create | Port `core/room_policy.py`. | Room-type eligibility and dining chair counts. |
| `pipeline/app/rules/layout_rules.py` (proposed) | Create | Port `core/layout_rules.py`. | Prompt rules for placement and correction. |
| `pipeline/app/rules/placement_mode.py` (proposed) | Create | Copy `deterministic_placement_mode` from `core/planner/placement_semantics.py:111`, plus the `placement_type` mapping. | The legacy module creates a model client at import time. |
| `pipeline/app/rules/planner/room_facts.py` (proposed) | Create | Port `core/planner/room_facts.py`. | Walls, openings, and blocked zones. |
| `pipeline/app/rules/planner/feasibility_digest.py` (proposed) | Create | Port `core/planner/feasibility_digest.py`. | Density budget and category caps. |
| `pipeline/app/rules/planner/fit_policy.py` (proposed) | Create | Port `core/planner/fit_policy.py`. | Density thresholds and count caps. |
| `pipeline/app/rules/planner/fit_check.py` (proposed) | Create | Port `core/planner/fit_check.py`. | Estimates whether the requested counts fit, and the counts that do. |
| `pipeline/app/rules/planner/_util.py` (proposed) | Create | Port `core/planner/_util.py`. | Room-scale and rounding helpers imported by the fit check. |
| `pipeline/app/rules/planner/intent_packet.py` (proposed) | Create | Port `core/planner/intent_packet.py` without the dropped fields. | Intent cleanup and count constraints. |
| `pipeline/app/rules/planner/selection_count_guidance.py` (proposed) | Create | Port `core/planner/selection_count_guidance.py`. | Count feedback for selection. |
| `pipeline/app/rules/planner/taxonomy.py` (proposed) | Create | Port `core/planner/taxonomy.py`. | Maps prepared categories to rule categories. |
| `pipeline/app/rules/selection/validation.py` (proposed) | Create | Port `nodes/asset_selection/validation.py`, keeping only fresh-design checks. The fit decision comes from the selection step instead of the request. Add an entry that takes explicit inputs, and copy the pure helpers it uses from `revision.py`. | Budget, count, density, required-item, and fit checks. |
| `pipeline/app/rules/selection/constants.py` (proposed) | Create | Port `nodes/asset_selection/constants.py`. | `BUDGET_FLEX_PCT` and required roles. |
| `pipeline/app/rules/selection/fit.py` (proposed) | Create | Port `nodes/asset_selection/fit.py`, keeping the automatic-continue guidance and the `use_recommendation` count targets. Drop the helpers that pause to ask the user (`_fit_confirmation_from_warning`, `_should_defer_failed_selection_to_fit_confirmation`). | Physical fit checks and the two automatic fit steps. |
| `pipeline/app/rules/selection/preflight.py` (proposed) | Create | Port the fresh-design parts of `nodes/asset_selection/preflight.py`. Take the intent as input instead of calling the model. | Required items and room checks. |
| `pipeline/app/rules/selection/catalog.py` (proposed) | Create | Port `nodes/asset_selection/catalog.py` without the Supabase loader. | Candidate lookups used by validation. |

## Acceptance criteria and verification

| Criterion | Check |
| --- | --- |
| Shared stages run once per request, then three variants each run select, place, correct, and validate. | Graph test with a fake model counts calls per stage. The run record agrees. |
| A variant that raises, times out, or reaches its limits emits `variant_failed` with a reason, and the other variants still finish. | Graph test makes one variant raise and another reach its limit. |
| A selection that does not fit the room never pauses the run. The model first gets smaller products, then capped counts, and the run record notes which step was applied. | Graph test where a fake model keeps returning furniture too large for the room: turn 2 asks for compact products, and turn 3 uses capped counts. |
| Only layouts with every selected instance placed once and no P0, P1, or critical P2 finding emit `variant_ready`. | Graph test with a fake model returns a blocking layout, and the parity check below covers the rules. |
| Ported rules match legacy results. | Parity test: legacy layouts and selections from the three runs in the pipeline overview, copied as fixtures from `runs/`. Ported `analyze_layout` returns the recorded blocking findings, and ported selection validation returns the recorded pass or fail. |
| Today's geometry request is accepted. Scanned-room, `upload_to_supabase`, and `billing_operation_id` fields get HTTP 422. Events use today's names and variant fields. | API test on the request model. Step 1 field list compared with emitted events. |
| The run record lists every stage with elapsed time and every model call with tokens and cost. Totals cover all variants. | Graph test reads the record. |
| After a client disconnect, no new model call starts and the record says `cancelled`. | API test with a slow fake model closes the client stream. |
| The call, turn, and proposal limits and the run deadline each end with a recorded reason, not a hang. | Graph tests with a fake model that keeps failing. |
| Requests run end to end for all four room types with the real model and catalog. | Manual local run. Each variant ends ready or failed with a reason. Timings are recorded next to the legacy runs. This is not a speed gate. |

Tests use a fake model client, because they check runtime behavior, not model output.

## Dependencies and risks

- **Phase 2.** Retrieval and populated embeddings must exist. The retrieve stage
  adapts to the phase 2 function signature.
- **Lower valid rate at first.** Plain model stages without seeding or the legacy
  repair tools may pass validation less often than today. Phase 3 records
  failure reasons. Phase 4 improves the rate.
- **Legacy naming assumptions.** Ported checks expect legacy categories and
  meaningful instance names. Category mapping, `<category>_<n>` keys, and the
  parity test address this.
- **Viewer orientation.** The viewer turns each model by `frontView`
  (`web-pipeline/lib/scene3d/signatures.ts:113`), and prepared records lack it.
  Legacy read it from raw `metadata.annotations` (`src/core/sync/sync_catalog.py:48`).
  Phase 5 adds `frontView` and `center` to prepared records.
- **Web app rejects these variants until phase 5**, because it requires
  `create_payload`. Phase 3 is verified through the API.
- **Cancellation** depends on Starlette cancelling the response task when the client
  disconnects. The disconnect test confirms this.

## Deferred work

- Phase 4: Jev, the refinement decision, the style-coherence audit, reselection
  after a layout fails, placement seeding, targeted repair, and stricter candidate
  filtering.
- Phase 5: `create_payload`, `turn_payload`, authentication, room saving, the chat
  reply, `frontView` and `center` in prepared records, previews, and the web
  app connection.

## Unresolved questions

None.
