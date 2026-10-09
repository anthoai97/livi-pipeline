# Web prototype

React + three.js frontend for the room generation flow: brief, live progress, and
a 3D chooser for the three designs. It is mock-only until the pipeline is ready.

## Run

```sh
cd web
pnpm install
pnpm dev            # http://localhost:5173
```

The mock flow replays one recorded pipeline run (`src/mock/recorded-run.json`: a
dining room for four, 3 of 3 designs ready in 86 s), shortened to about 30 s and
fitted to the brief's room size, door, window, and ceiling. Products are always the
recorded dining room set, whatever room type is picked.

## Routes

They follow the legacy web app (`web-pipeline`):

| Route | Screen |
| --- | --- |
| `/` | Brief |
| `/generate?roomPurpose&budget&prompt&room&step` | Progress. The run is keyed by the params, so a reload restarts it. |
| `/select-variant` | Choose one of three designs. Options live in memory: a reload goes back to `/`. |
| `/studio?v=2&workspace&room&design` | Studio for the chosen design. |

"Continue with this design" mirrors the legacy finalize step: it creates the
design (mocked in `src/lib/designs.ts` with `localStorage` instead of
`POST /designs`), stops designs still running, and replaces the URL with the
Studio link. Studio loads the design from the URL ids and shows the legacy
empty state for an unknown or malformed link.

## Room shapes

The brief offers the legacy builder's Rectangle, L-shaped, T-shaped, and Angled
outlines with its default proportions (`src/lib/shapes.ts`, ported from
`web-pipeline lib/rooms/shapes.ts`). Walls are the polygon's edges, so the door and
window pick an edge. The 3D view looks in from a cut-away corner when the room has
inner walls. The mock fits the recorded layout into the largest rectangle inside
the floor and moves wall pieces onto the nearest real wall.

The dev server proxies `/s3/*` to the asset bucket, which sends no CORS headers
for GLB files.

## Pipeline contract the mock assumes

The recording holds the pipeline's SSE events (`pipeline/app/contracts.py`), plus
one field the pipeline does not send yet: `node_complete.data`, a few display facts
per stage. The progress rows and product cards read it.

| Node | `data` |
| --- | --- |
| `interpret` | `style_hints`, `requested_categories` |
| `extract_room` | `room_area`, `wall_height`, `doors`, `windows`, `floor_area_sqm`, `usable_area_sqm`, `protected_paths`, `fit`, `fit_message` |
| `rag_scope_assets` | `slots`, `candidates`, `gaps`, `preview` (top product per slot: `asset_id`, `name`, `category`, `image_url`, `price`, `slot`, `kind`) |
| `select_asset_intent` | `turn`, `valid`, `errors`, `fit_step`, `total_cost`, `items` (products) |
| `layout_initial`, `layout_fix` | `placed`, `findings`, `blocking`; `layout_fix` adds `improved` |
| `render_scene` | `valid`, `errors` |

Types are in `src/lib/types.ts`. Connecting the real pipeline means adding these
fields, replacing `mockPipeline` with a `POST /pipeline` SSE reader, and proxying
`/api` to the service.

## Layout

| Path | Role |
| --- | --- |
| `src/lib/mock.ts` | Mock flow: replays the recording, fitted to the brief |
| `src/lib/run.ts` | Run store: reduces events into shared and per-design progress |
| `src/lib/tasks.ts` | Progress rows built from `node_complete.data` |
| `src/components/RoomScene.tsx` | Room shell, openings, and GLB furniture from a render manifest |
| `src/lib/shapes.ts` | Room outlines, walls, and polygon helpers |
| `src/lib/router.ts` | Minimal history router |
| `src/lib/designs.ts` | Mock design store behind the Studio link |
| `src/screens/` | Brief, Generate, Choose, and Studio screens |
