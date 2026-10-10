# Web prototype

React and three.js frontend for the live pipeline: brief, progress, design chooser,
and Studio. Generation uses the pipeline service. Design saving uses localStorage.

## Run

Start the [pipeline service](../pipeline/README.md) on port 8000. In another terminal:

```sh
cd web
pnpm install
pnpm dev
```

Open the URL Vite prints (normally `http://localhost:5173`). The dev server proxies
`/api/*` to `http://localhost:8000/*` and `/s3/*` to the asset bucket for GLB files.
Restart Vite after changing proxy settings. Deployment is outside this phase.

Run checks from `web/`:

```sh
pnpm test
pnpm typecheck
pnpm build
```

## Routes

The routes follow the legacy web app (`web-pipeline`):

| Route | Screen |
| --- | --- |
| `/` | Brief |
| `/generate?roomPurpose&budget&prompt&room&step` | Progress. A reload starts a new run for the brief in the URL. |
| `/select-variant` | Choose a design. Options live in memory; a reload returns to `/`. |
| `/studio?v=2&workspace&room&design` | Studio for the locally saved design. |

"Continue with this design" saves the ready variant in localStorage, aborts the
stream if generation is still running, and opens Studio. Studio loads the design
from the URL IDs. An unknown or malformed link shows the empty state. There is no
authentication or remote design saving.

The brief offers Rectangle, L-shaped, T-shaped, and Angled room outlines. The
pipeline receives the selected polygon, openings, room type, and budget. The 3D
view uses the returned manifest without refitting a recorded layout.

## Pipeline events and progress

`src/lib/pipeline.ts` posts the brief to `/api/pipeline` and reads SSE frames with a
streaming UTF-8 decoder. Events are validated at the boundary. HTTP failures,
invalid events, and EOF before `complete` or `error` show an error with retry.
A failed variant shows the backend message. Ready designs remain available when
another design fails or the run stops. An all-failed run offers retry.

Progress rows read `node_complete.data`:

| Node | `data` |
| --- | --- |
| `interpret` | `style_hints`, `requested_categories` |
| `extract_room` | `room_area`, `wall_height`, `doors`, `windows`, `floor_area_sqm`, `usable_area_sqm`, `protected_paths`, `fit`, `fit_message` |
| `rag_scope_assets` | `slots`, `candidates`, `gaps`, `preview` (top product per slot) |
| `select_asset_intent`, shared | `ranked_slots`, `shared_products` |
| `select_asset_intent`, per design | `turn`, `valid`, `errors`, `fit_step`, `total_cost`, `items` (distinct products, capped at the slot count) |
| `layout_initial` | `placed`, `findings`, `blocking`, `swaps`, `drops` |
| `layout_fix` | `placed`, `findings`, `blocking`, `improved` |
| `render_scene` | `valid`, `errors` |

## Previews and browser timing

The chooser displays the interactive 3D room with compact design selectors.
Generate, Choose, and Studio show the scene as models load, with placeholders for
missing models. The API still provides `preview_url` for inspection, but the web
flow does not display the 2D floor-plan images.

The browser records one timing entry per displayed design in the current run,
using milliseconds from submission:

| Field | Meaning |
| --- | --- |
| `variant_index` | Design index, 0 to 2 |
| `received_ms` | Arrival of `variant_ready` |
| `loaded_ms` | All manifest model instances loaded or failed |
| `displayed_ms` | First frame drawn after model settlement, without waiting for entrance animations |
| `models` | Model instances in the displayed manifest, including placeholders |
| `failed_models` | Instances with a missing URL, empty model, or load failure |
| `screen` | `generate`, `chooser`, or `studio`, wherever that first frame occurs |

Counts belong to the displayed manifest, including cache hits and repeated model
URLs. They do not use the global loading manager. Failed models keep their
placeholders; an empty manifest settles immediately.

The run ID comes from `start`. The browser posts timing once to
`/api/runs/<run_id>/client-timing` and ignores post failures. Changing designs or
starting another run cannot reuse the previous scene's counters. Continuing early
keeps the run's timing context for Studio. A page reload loses that context, so
reopening a locally saved design does not post new generation timing.

## Files

| Path | Role |
| --- | --- |
| `src/lib/pipeline.ts` | POST SSE reader with cancellation |
| `src/lib/types.ts` | Pipeline event schemas and shared types |
| `src/lib/run.ts` | Shared and per-design progress, run ID, and timing submission |
| `src/lib/tasks.ts` | Progress rows from stage facts |
| `src/lib/model-loading.ts` | Per-manifest model settlement |
| `src/components/RoomScene.tsx` | Room geometry, GLBs, loading state, and first-frame callback |
| `src/lib/shapes.ts` | Room outlines, walls, and polygon helpers |
| `src/lib/router.ts` | History router |
| `src/lib/designs.ts` | Local design store behind the Studio link |
| `src/screens/` | Brief, Generate, Choose, and Studio |
| `tests/` | Stream, cancellation, run timing, and model settlement tests |
