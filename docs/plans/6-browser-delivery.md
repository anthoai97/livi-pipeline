# Phase 5: deliver variants to the browser

Issue: [#6](https://github.com/anthoai97/livi-pipeline/issues/6). Parent: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).
Status: ready for implementation after phase 4 ([#5](https://github.com/anthoai97/livi-pipeline/issues/5),
PR [#10](https://github.com/anthoai97/livi-pipeline/pull/10)) is merged. This plan
builds on that branch. Nothing in this plan has been built or run.

## Objective

Connect the `web/` prototype to the live pipeline, so the brief, progress, design
chooser, and Studio run on real generation instead of a recorded run. Each ready
design gets a top-down layout preview. The browser measures how long each design
takes to load and draw, and saves those times in the run record next to the
generation times.

## Current behavior

Pipeline (PR #10 branch):

- `POST /pipeline` streams SSE frames (`pipeline/app/main.py`). `variant_ready` and
  `variant_failed` go out as soon as each variant finishes. One failed variant does
  not stop the others. A 300-second run deadline and cancellation on disconnect are
  in place.
- Stages: `interpret`, `room`, `retrieve`, and `rank` (shared), then `select`,
  `place` (code solver), `repair`, `correct` (fallback), and `validate` per variant
  (`pipeline/app/graph.py`). Events use the legacy node names in
  `contracts.NODE_NAMES`. `rank` shares `select_asset_intent`, and `repair` shares
  `layout_fix`.
- `node_complete` carries only `node`, `index`, `variant_index`, and `elapsed`
  (`RunContext.stage` in `pipeline/app/run.py`).
- Phase 4 makes the three variants distinct (Jev ranking per direction,
  `PRODUCT_REUSE_RATE`).
- `pipeline/scripts/review.py` already draws a top-down plan of a ready variant
  (`render_plan`, Pillow, footprints only, no image downloads) for benchmark reviews.
- The run record is one JSON file, `pipeline/.data/runs/<run_id>.json`, written when
  the run ends.

Catalog: `front_view` is set for all 4,775 catalog products, and the viewer treats
the 320 decor items' null as 0. `center`, `topdown_url`, and an S3 model URL are set
for all 5,095 prepared products. 5,084 images are on S3. No catalog work is needed.

Web prototype (`web/`, React and three.js):

- Routes follow the legacy app: `/`, `/generate`, `/select-variant`, `/studio`.
- `useRun` (`web/src/lib/run.ts`) reduces pipeline events into shared and
  per-design progress, but the events come from `mockPipeline`
  (`web/src/lib/mock.ts`), which replays `web/src/mock/recorded-run.json`.
- Progress rows read `node_complete.data`, which the pipeline does not send. The
  fields are listed in `web/README.md`.
- Designs show as they finish, and the first opens early. "Continue with this
  design" saves to a `localStorage` mock of the design service, stops designs
  still running, and opens Studio.
- The dev server proxies `/s3` to the asset bucket for GLB files. There is no
  `/api` proxy.
- `RoomScene` loads GLBs with `useGLTF` and shows a loading count. Nothing measures
  load or display time.

## Scope

In scope:

- A real `POST /pipeline` SSE reader in the web app, replacing the mock replay.
- `node_complete.data` display facts from every pipeline stage.
- One top-down layout preview PNG per ready variant, served by the pipeline and
  shown as the chooser thumbnail.
- Browser load and display timing per shown design, saved in the run record.

Non-goals:

- Distinct variants. Phase 4 already covers them.
- Real design saving, `create_payload`, `turn_payload`, login, Supabase, and the
  chat reply. Saving stays in the `localStorage` mock.
- The legacy `web-pipeline` app.
- A cutoff for slow designs. The user can continue with any ready design, which
  stops the rest. Otherwise the run deadline applies.
- Image-based refinement. Phase 4 can use the previews later.
- Deployment. This phase runs locally: the Vite dev server proxies to the pipeline.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Browser app | The `web/` prototype, with saving mocked | It already has the flow on the legacy routes and reads the pipeline's event shapes. The legacy app's parser rejects variants without `create_payload`. |
| Slow designs | The user decides | Ready designs already show at once, and continuing stops the rest. A cutoff would hide slow variants instead of measuring them. |
| Layout previews | Render with the existing `render_plan` after a variant passes validation | No image downloads, so no request-time fetch (legacy preview downloads took up to 61 s). One renderer serves the chooser, reviews, and later refinement. |
| Preview delivery | Save PNGs next to the run record, and send a `preview_url` on `variant_ready` | Keeps SSE frames small. The browser loads the image through the `/api` proxy. |
| Browser timing | Post to the pipeline, saved in the run record | Generation and display times then sit in one record for comparison. |
| Mock replay | Delete `mock.ts` and the recording | The web README says connecting the pipeline replaces it. Keeping both modes adds a second path. |

## Design

### Pipeline: stage display data

`RunContext.stage` yields a `StageContext` with a `data` dict. Each stage fills it,
and `contracts.node_complete` sends it as `data`. This matches the web README table,
mapped to the PR #10 stages:

| Stage (node) | `data` |
| --- | --- |
| interpret (`interpret`) | `style_hints`, `requested_categories` |
| room (`extract_room`) | `room_area`, `wall_height`, `doors`, `windows`, `floor_area_sqm`, `usable_area_sqm`, `protected_paths`, `fit`, `fit_message` |
| retrieve (`rag_scope_assets`) | `slots`, `candidates`, `gaps`, `preview`: the top product per slot (`asset_id`, `name`, `category`, `image_url`, `price`, `slot`, `kind`) |
| rank (`select_asset_intent`, shared) | `ranked_slots`, `shared_products` |
| select (`select_asset_intent`) | `turn`, `valid`, `errors`, `fit_step`, `total_cost`, `items` |
| place (`layout_initial`) | `placed`, `findings`, `blocking`, `swaps`, `drops` |
| repair, correct (`layout_fix`) | `placed`, `findings`, `blocking`, `improved` |
| validate (`render_scene`) | `valid`, `errors` |

Counts are integers, and product lists are capped at the slot count. The data only
describes work already done, so no stage changes its behavior.

### Pipeline: layout previews

- Move `render_plan`, its helpers and style constants, and `png_bytes` from
  `pipeline/scripts/review.py` into `pipeline/app/preview.py`. The review script
  imports them from there.
- After `validate` passes, the stage renders the plan in a worker thread and saves
  it to `pipeline/.data/runs/<run_id>/variant_<index>.png`. The render time goes
  into the run record. If rendering fails, the variant is still ready without a
  preview, and the record notes the error.
- `ready_variant` adds `preview_url: "/runs/<run_id>/previews/<index>.png"`, or null.
- `GET /runs/{run_id}/previews/{index}.png` serves the file. The run ID must be 32
  hex characters and the index 0 to 2. Anything else returns 404.

### Pipeline: browser timing

- `POST /runs/{run_id}/client-timing` takes one entry per shown design:
  `variant_index`, `received_ms` (from request submission to `variant_ready`
  arrival), `loaded_ms` (to all models loaded), `displayed_ms` (to the first frame
  after loading), `models`, `failed_models`, and `screen`. The values are
  non-negative and bounded; unknown fields get HTTP 422.
- While the run is active, `main.py` keeps its `RunContext` in a dictionary keyed by
  run ID, and the entry joins the record when the run ends. After the run, the
  endpoint appends the entry to the saved JSON record under an `asyncio.Lock`.
  An unknown run ID returns 404.
- The record gains `client_timing`, a list of these entries.

### Web

- Replace `mockPipeline` with a reader in `web/src/lib/pipeline.ts` (proposed). It
  posts the brief to `/api/pipeline`, parses `data:` frames from the response body,
  passes each event to the store, and stops on `AbortSignal`. `EventSource` cannot
  send a POST body.
- `vite.config.ts` proxies `/api` to `http://localhost:8000` and strips `/api`.
- `types.ts`: add `preview_url` to `ReadyVariant`, and the rank and repair data
  fields to `NodeData`.
- `tasks.ts`: build the progress rows from the real data, including the shared
  `rank` step and the `layout_fix` repair data.
- `Choose.tsx`: each option shows its preview image while its 3D scene loads, and
  as its thumbnail in the option list.
- `RoomScene.tsx`: report when every model of the shown manifest has loaded or
  failed, and when the first frame after that has drawn. `run.ts` turns these
  times into a timing entry for each design the first time it is shown, and posts
  it to `/api/runs/<run_id>/client-timing`.
- `run.ts` keeps the run ID from `start` for previews and timing.

### Failure paths

| Case | Behavior |
| --- | --- |
| The pipeline is not running | The reader's fetch fails, and Generate shows the error with a retry. |
| A variant fails | `variant_failed` shows the failure text, as the prototype does today. |
| The preview fails to render | The variant is ready with `preview_url: null`, and the chooser shows the 3D scene only. |
| A model fails to load | The scene keeps its placeholder, and the timing entry counts it in `failed_models`. |
| A timing post fails or arrives for an unknown run | The browser ignores it. The pipeline returns 404 for an unknown run. |
| The user continues early | The reader aborts, the pipeline cancels the remaining variants, and the record says `cancelled` with the ready ones kept. |

## Implementation steps

1. **Stage data.** Add `data` to the stage context and `node_complete`, then fill it
   in each stage. Check: a graph test with fakes finds the listed keys on every
   `node_complete`.
2. **Previews.** Move `render_plan` into `app/preview.py`, render after validation,
   add `preview_url`, and serve the files. Check: stage and API tests, and
   `review.py` still renders.
3. **Timing endpoint.** Add the active-run registry, the endpoint, and
   `client_timing` in the record. Check: API tests for an active run, a finished
   run, an unknown run, and an invalid body.
4. **Web reader.** Add the SSE reader and the `/api` proxy, then delete the mock
   replay. Check: `pnpm typecheck` and `pnpm build`.
5. **Web display.** Progress rows from real data, preview thumbnails, and load and
   display timing.
6. **Local runs.** Run the brief for all four room types with the pipeline and web
   dev servers, using the agent browser. Record generation and display times.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/run.py](../../pipeline/app/run.py) | Edit | `StageContext.data`, sent in `node_complete`. Preview render time and `client_timing` in the record. | Display facts and browser times share the stage timer and the record. |
| [pipeline/app/contracts.py](../../pipeline/app/contracts.py) | Edit | `node_complete` takes `data`. `ready_variant` adds `preview_url`. Client timing request model. | Event and request shapes live here. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | Fill `data` for interpret, room, retrieve, and rank. | Shared progress rows. |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | Fill `data` for select, place, repair, correct, and validate. Render the preview after validation passes. | Per-design progress rows and previews. |
| [pipeline/app/main.py](../../pipeline/app/main.py) | Edit | Active-run registry, `GET /runs/{run_id}/previews/{index}.png`, and `POST /runs/{run_id}/client-timing`. | HTTP routes for previews and timing. |
| `pipeline/app/preview.py` (proposed) | Create | `render_plan`, helpers, constants, and `png_bytes`, moved from `scripts/review.py`. | The app cannot import from a script. One renderer serves validation and reviews. |
| `pipeline/scripts/review.py` (PR #10) | Edit | Import the renderer from `app.preview`. | Removes the duplicate. |
| [web/src/lib/run.ts](../../web/src/lib/run.ts) | Edit | Use the real reader, keep the run ID, and post timing entries. | Store wiring. |
| `web/src/lib/pipeline.ts` (proposed) | Create | `POST /api/pipeline` SSE reader with abort. | Replaces `mock.ts`, which is deleted. |
| [web/src/lib/mock.ts](../../web/src/lib/mock.ts) | Delete | Mock replay. | Replaced by the real stream. |
| [web/src/mock/recorded-run.json](../../web/src/mock/recorded-run.json) | Delete | The recording. | Only `mock.ts` reads it. |
| [web/src/lib/types.ts](../../web/src/lib/types.ts) | Edit | `preview_url`, rank and repair `NodeData`, and the timing entry. | Matches the pipeline contract. |
| [web/src/lib/tasks.ts](../../web/src/lib/tasks.ts) | Edit | Progress rows from real data, including rank and repair. | The recording had no rank stage. |
| [web/src/screens/Choose.tsx](../../web/src/screens/Choose.tsx) | Edit | Preview image as the option thumbnail and while the scene loads. | Shows the preview. |
| [web/src/components/RoomScene.tsx](../../web/src/components/RoomScene.tsx) | Edit | Load and first-frame callbacks for the shown manifest. | Browser timing. |
| [web/vite.config.ts](../../web/vite.config.ts) | Edit | Proxy `/api` to the pipeline. | Same-origin calls without CORS. |
| [web/README.md](../../web/README.md) | Edit | Running with the pipeline, the contract, previews, and timing. Remove the mock section. | Keeps the setup accurate. |
| [pipeline/README.md](../../pipeline/README.md) | Edit | The new routes and record fields. | Service reference. |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | Stage data, previews, and browser timing. | Flow reference. |

## Acceptance criteria and verification

| Criterion | Check |
| --- | --- |
| Every `node_complete` carries the data listed for its stage. | Graph test with fakes. |
| Each ready variant has a preview PNG and `preview_url`. A render failure leaves the variant ready without a preview. | Stage test, and an API test that fetches the PNG. |
| The preview route rejects a malformed run ID or index. | API test. |
| Browser timing reaches the run record whether the run is still active or already finished. Unknown runs return 404, and invalid bodies return 422. | API tests. |
| The brief runs end to end on the real pipeline for all four room types: real progress rows, each design shown when ready, preview thumbnails, and Studio after "Continue with this design". | Manual run with the agent browser against local servers. |
| A failed design shows its failure text, and continuing early stops the rest with `cancelled` in the record. | Same manual run, plus the existing cancellation test. |
| Each shown design has a timing entry with received, loaded, and displayed times. | The run records of the manual runs. |
| The web app type-checks and builds. | `pnpm typecheck` and `pnpm build`. |

## Dependencies and risks

- **PR #10 must merge first.** Stage names, the solver data (`swaps`, `drops`), and
  `scripts/review.py` come from it.
- **Non-rectangular rooms.** The brief offers L, T, and angled outlines. The solver
  filters rectangle candidates by the outline, so these rooms may place worse.
  This phase shows the result as is.
- **GLB loading** goes through the `/s3` dev proxy, so measured load times include
  the proxy. The record keeps `screen` and model counts for context.
- **Browser state.** Choose keeps options in memory, so a reload returns to the
  brief, as today.

## Unresolved questions

None.
