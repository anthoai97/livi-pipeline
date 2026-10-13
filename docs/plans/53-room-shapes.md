# Furnish any room shape

Issue: [#53](https://github.com/anthoai97/livi-pipeline/issues/53) (open).

## Objective

Furnish a room with any simple outline: the room builder's five presets
(rectangle, L, alcove, T, angled) with adjustable walls, its Z and U
templates, and anything else a user draws. The pipeline reads the real walls,
area, pockets and openings; places furniture along every wall, inner walls
included; keeps the passages between pockets clear; and tells the models what
the room really is.

## Current behavior (verified on `main`, 2026-10-13)

Line numbers drift; search for the named symbol. The builder and the legacy
pipeline live in `codebase/` next to this repo (`web-pipeline` on `trunk`,
`livinit_pipeline`).

### The room builder (`web-pipeline`)

- Five presets in the picker plus Custom (`lib/rooms/shapes.ts`). Z and U are
  templates that create a Custom outline (`lib/rooms/templates.ts`). Any
  outline can be dragged; walls stay at least 0.6 m and a room has at most 100
  vertices. Defaults at width w, length l:

  | Shape | Default outline |
  | --- | --- |
  | L | top-right corner cut, 0.4w x 0.4l |
  | Alcove | back-wall recess, w/3 wide, 0.3l deep, centred |
  | T | stem 0.4w wide, centred; bar 0.45l deep |
  | Angled | top-left corner cut, 0.27w x 0.37l |
  | Z | two opposite corners cut, w/3 x l/3 each |
  | U | back-wall notch w/3 wide, l/2 deep, centred |

  The 4 x 5 m Z has 8 walls, 15.6 m² and a 1.33 m throat (the issue's
  13.9 m² is off).
- Openings (`lib/wizard/buildRoomGeometry.ts`): `center` on the edge, `width`,
  `depth` 0.05, `height`, `sill_height`, and `wall` as a cardinal name from the
  edge's dominant direction, for every shape. The wizard holds the edge index
  but does not send it. The proxy (`app/api/pipeline/route.ts`) requires
  `depth > 0`.
- Wall names on screen (`lib/rooms/wallNames.ts`): "Back", "Back (inner)",
  "Right (front)" and "Right (back)" for two walls on one line, "Back-right"
  for a diagonal.

### This pipeline

- **Request.** `room_area` is the bounding box and `room_vertices` the
  outline ([contracts.py](../../pipeline/app/contracts.py)). A door or window
  is a free-form dict. The web form ([room.ts](../../web/src/lib/room.ts))
  sends the wall name only for rectangles and holds the edge index
  (`doorWall`) without sending it; its `shapes.ts` has rectangle, L, T and
  angled only.
- **Walls.** `room_walls` ([door_geometry.py](../../pipeline/app/rules/door_geometry.py))
  keeps only edges within 0.2 rad of an axis and names them by facing, so a
  Z-room's two "back" edges share one name and a diagonal edge disappears.
  `nearest_wall` raises when no edge is near an axis.
- **Room facts.** One wall record per name with the bounding box's length
  ([room_facts.py](../../pipeline/app/rules/planner/room_facts.py)),
  `"shape": "rectangle"`, and anchor walls ranked on those phantom walls.
  `area` is already the polygon's, and the digest and `fit_check.py` read it.
  Still measured against the box: the sectional check
  (`feasibility_digest.py`), `filter_oversized_seating` (`selection/catalog.py`),
  the product fit filter (`shared_stages.py`), the selection fit check
  (`selection/preflight.py`, `_fits_rect`), the selection prompt's `W x D` line
  (`variant_stages.py`) and `scripts/review.py`'s area.
- **Paths.** Built only between doors on opposing named walls, spanning the
  box ([protected_paths.py](../../pipeline/app/rules/protected_paths.py)),
  and only when the interpret model sets `requires_straight_circulation_path`,
  which the prompt reserves for an explicit request. Non-rectangles never get
  one; rectangles rarely. Path records are axis-aligned rectangles and these
  read them as such: `selection/preflight.py:_protected_path_zones` splits the
  room into two rectangles for `selection/validation.py`;
  `compute_protected_path_violations` reports `translations_to_clear_m` from
  the bounds; `build_room_facts` wraps each path with `_rect_payload`; the
  review prompt dumps the records as JSON.
- **Placement.** Wall candidates come from the four box walls at fixed yaws
  ([candidates.py](../../pipeline/app/rules/geometry/candidates.py),
  `_wall_candidates`); the outline only removes those outside. The dining grid
  and free-piece corners use the box ([solver.py](../../pipeline/app/rules/layout/solver.py),
  `_placements`, `_preference`). Yaw-to-wall helpers snap to a name
  ([relations.py](../../pipeline/app/rules/layout/relations.py), `_back_wall_name`);
  normalization moves a wall piece to the nearest axis wall
  ([normalization.py](../../pipeline/app/rules/layout/normalization.py));
  the wall-mount and wall-aligned checks require an axis wall
  ([validation_geometry.py](../../pipeline/app/rules/layout/validation_geometry.py));
  the sofa wall gap skips non-axis sofas and raises on a missing name
  ([validation_functional.py](../../pipeline/app/rules/layout/validation_functional.py)).
  Seed guidance reasons in a four-name algebra
  ([seed_guidance.py](../../pipeline/app/rules/planner/seed_guidance.py)).
- **Prompts.** `coordinate_system_block` names four walls
  ([layout_rules.py](../../pipeline/app/rules/layout_rules.py)); the review
  allows quarter turns only (`variant_stages.py`, `allowed`).
- **Web.** `RoomScene.tsx` and `Generate.tsx` take an opening's size from
  `max(width, depth)`.
- **Fixtures.** `tests/fixtures/legacy_seed_cleanup.json` carries wall names
  on every opening; `test_rules.py` and `test_solver.py` load it.
  `benchmark_requests.json` has 10 cases, four with `"wall": "top"`.
- **Measured.** The same living room solved in a rectangle, an L and an
  angled room (6 x 5 m box): every item placed, no blocking findings, but the
  L squeezed the sitting group into its 3 m arm with the TV in the other arm,
  and the angled room left its diagonal wall and 4 m back wall empty.

### Legacy code to port (`livinit_pipeline/src/core`)

- `room_shape.py`: `_rectangles` sweeps the maximal rectangles of an
  orthogonal polygon from its vertex coordinates; `shape_guidance` lists them
  with their overlaps; `shortest_inside_route` is a visibility-graph route
  inside a polygon. Run on the shapes above it gives the L, T, alcove and
  angled rooms two rectangles each, the U three, and the Z its two pockets
  plus a throat column and a cross band that the two pockets together cover.
  On the angled room it stops at the diagonal's endpoints and leaves the
  corner triangle out.
- `protected_paths.py`, `_polygon_paths`: erodes the polygon by half the
  width, routes between the doors' inward points, buffers with flat ends,
  clips to the room, and stores `polygon` and `centerline` with the bounds as
  `center`, `width`, `depth`. Its preflight returned no path zones for a
  concave room.

## Scope

Four phases, each shipped on its own, in this order. Rooms are simple
polygons with no holes. Phases 1 to 3 keep quarter-turn placement; phase 4
adds diagonal walls.

Non-goals: rooms as zone sets instead of room types (a later issue); changes
to the room builder (it keeps sending the same request); furniture crossing a
passage; curved walls.

## Decisions

Made from interior-design practice and the builder's behavior, as asked.

1. **Any simple polygon, no preset name.** The pipeline derives walls,
   pockets, area and paths from the vertices. A preset is a starting point in
   the builder; adjusted walls make it any shape.
2. **An opening's wall is the edge nearest its `center`.** The builder puts
   the centre on the edge, so the lookup is exact. `wall` and `depth` stay
   accepted and ignored, so nothing changes in the builder. The pipeline
   builds the opening rectangle on that edge from `center` and `width`.
3. **A pocket is a usable rectangle.** The maximal axis-aligned rectangles
   inside the outline, from the legacy sweep, with both sides at least 1.2 m
   (the smallest zone: a desk with its chair pulled out), dropping any that
   the union of larger pockets covers. The Z gives two pockets; its throat
   column and cross band are covered by both and are passage. The L gives two
   arms sharing the corner. The U gives the band and two arms. The alcove
   gives the main room and the alcove. The T gives the bar and the stem.
4. **One zone per pocket.** A zone (sleeping, sitting with its media, dining,
   work) lives inside one pocket: its templates, bands and the sofa-to-TV
   sightline. Two zones may share a large pocket when both fit. Storage,
   lamps and free pieces go anywhere. A conversation group or a TV across a
   passage is what designers avoid, since the route cuts the group.
5. **Passages stay clear, always.** A 0.91 m route (the main circulation
   width in the rules) is kept between doors in different pockets, and between
   doors on opposing walls of one pocket, as the legacy did. Doors on adjacent
   walls of one pocket get no route: their clearance zones keep the entries
   clear, which is today's behavior for rectangles. When no 0.91 m route
   exists, the route is retried at 0.6 m (the builder's shortest wall), then
   dropped. The `requires_straight_circulation_path` flag goes: clear
   circulation between entries is not something a brief opts into.
6. **Angled walls in two steps.** Through phase 3 a diagonal edge bounds the
   pockets; the corner outside them is free floor for a plant, lamp or chair.
   Phase 4 lets wall pieces back onto it.
7. **Walls get the builder's labels.** The facing word; "(inner)" when the
   edge is set back from the outermost wall facing the same way; "(front)" and
   "(back)", or "(left)" and "(right)", for two walls on one line; "Back-right"
   for a diagonal, as `wallNames.ts` shows them. Code uses the index.
8. **Designs differ by pocket.** The solver's zone plan key becomes
   `(kind, pocket, wall)`, so the dealing in #52 gives the three designs
   different pocket assignments when the room allows.

## Phases

### 1. Real walls, openings, area and paths

- **Openings.** `door_geometry` resolves the edge from `center`; `door_wall`
  returns the edge index; `door_opening_rect`, the clearance zone and
  `door_inward_normal` build rectangles oriented along the edge. Window
  rectangles and blockers in `primitives.py` and `relations.py` do the same.
  `room.ts` stops sending the wall name; `types.ts` drops `wall` from the
  opening; `RoomScene.tsx` and `Generate.tsx` take the size from `width`.
  `legacy_seed_cleanup.json` and `benchmark_requests.json` drop their wall
  names.
- **Walls.** `door_geometry.RoomWall` becomes one record per edge: `index`,
  `start`, `end`, `length`, inward unit `normal`, `label`, `on_box`.
  `room_walls` keeps every edge. `nearest_wall` is by distance to any edge.
  `WALL_NAMES` goes.
- **Room facts.** `walls` lists every edge with its length, label, openings
  and clear spans measured along the edge; `shape` is `rectangle` or
  `polygon` with the vertex count; `candidate_anchor_walls` rank edges.
  `format_room_facts_for_prompt` lists walls as "wall 3 (Back (inner)),
  1.33 m, door at 0.5 m". The selection prompt and `scripts/review.py` state
  the walls and the polygon area instead of `W x D`.
- **Paths.** `build_protected_paths` always runs, ported from the legacy
  `_polygon_paths`: for each door pair under decision 5, the shortest route
  between their inward points inside the polygon eroded by 0.455 m, buffered
  to 0.91 m with flat ends and clipped to the room. A straight axis-aligned
  route keeps today's record (`axis`, `center`, `width`, `depth`); a routed
  one adds `polygon` and `centerline`, with the bounds as `center`, `width`,
  `depth` and `axis: "routed"`. `protected_path_polygon` reads `polygon`
  first. `_protected_path_zones` returns no zones for a routed path;
  `compute_protected_path_violations` reports `translations_to_clear_m` only
  for an axis-aligned one; `build_room_facts` adds the polygon to the blocked
  zone; the review prompt lists each path's centerline. The intent flag, its
  prompt line and `intent_requires_protected_path` go.
- **Coordinate prompt.** `coordinate_system_block` lists the walls by index
  and label with their endpoints instead of four names.
- **Web presets.** `web/src/lib/shapes.ts` gains the alcove preset and the Z
  and U templates at the builder's proportions, and `example()` takes a
  shape, so the form, its examples and the benchmark cases stay in step.

Result: the Z-room reports 8 walls and 15.6 m², keeps the route between its
doors clear, and selects for its size. Placement still uses the box walls.

### 2. Pockets and zone assignment

- **Pockets.** `room_facts.pockets`: each with `bounds`, `width`, `depth`,
  `area`, the wall indexes that bound it, and its openings, largest first,
  from the legacy `_rectangles` sweep with decision 3's minimum and cover
  rule. `free_floor` is the outline minus the pockets (the angled corner).
- **Fit.** The sectional check, `filter_oversized_seating`, the product fit
  filter and the selection fit check measure against the largest pocket's
  sides, not the box. The selection prompt says the room's pockets and their
  sizes.
- **Solver.** `_Room` holds the pockets. A zone group's candidates come from
  one pocket at a time: wall candidates along the pocket's bounding edges
  that are real walls, the dining grid inside the pocket, the studio divider
  on the pocket's axis. A candidate whose footprint or bands leave the pocket
  is dropped. The media group takes the sitting group's pocket. The zone key
  becomes `(kind, pocket index, wall index)`, so the beam keeps assignments
  that differ by pocket, and #52 deals them.
- **Review prompt.** Lists the pockets and which zone stands in each.

Result: the L puts sitting in the long arm and dining or the desk in the
short one; the alcove takes the bed or desk; designs differ by pocket.

### 3. Solver and checks on real walls

- **Candidates.** `_wall_candidates(walls, width, depth, preferred)` yields
  spots along each edge at its inward yaw, clamped to the edge's clear span,
  inner edges included. Grid and centered candidates clip to the polygon.
- **Relations.** `_back_wall_name` and `_front_wall_name` become
  `wall_behind(pose, walls)` and `wall_ahead` (the edge a ray from the piece
  hits, backwards or forwards). `preferred_directions`, the window seating
  band and `_window_wall_line` work per edge.
- **Seed guidance.** Opposite is the edge hit by a ray along the anchor's
  facing; adjacent are the edges sharing a vertex. `preferred_walls_by_uid`
  holds edge indexes.
- **Normalization.** Wall-aligned and wall-mounted pieces snap flush to the
  edge behind them at its inward yaw; no move to another wall. The sofa's
  window shift is along the edge normal.
- **Checks.** Wall-mount and wall-aligned checks measure the gap to the edge
  behind and the yaw against its normal; `wall_mount_placement_facts` lists
  every edge; path-clearing moves run along the edge normal. The sofa wall
  gap uses the edge behind the sofa, never a name. Media focal alignment
  compares edges. Finding records (`back_wall`, `expected_wall`, `wall_pair`)
  carry the edge index and label.

Result: pieces stand on inner walls; a wall TV on the Z-room's "Back (inner)"
wall passes the check.

### 4. Diagonal walls

- `_turn` and `oriented_living_group_bounds` rotate by any angle;
  `projected_half_extents` callers use the rotated polygon; `normalize_rotation`
  keeps any yaw.
- Wall candidates on a diagonal edge face its normal; the beam key uses the
  edge index as before.
- The review prompt says "turn a piece to face a wall or by quarter turns";
  `allowed()` accepts a yaw within 0.02 rad of any wall's inward normal, or a
  quarter turn from the current yaw. Preflight tries each wall's yaw.
- The room facts' `free_floor` shrinks to what the pockets and the diagonal
  wall band leave.

Result: art hangs on the angled wall; a sofa can sit across the corner.

## File changes

Main files first. Phase in brackets.

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/rules/door_geometry.py](../../pipeline/app/rules/door_geometry.py) | Edit | [1] `RoomWall` per edge with index, normal, label; `room_walls`, `nearest_wall`, `door_wall` (edge from `center`), `door_inward_normal`, `door_opening_rect` and the clearance zone oriented along the edge; `WALL_NAMES` goes. | The wall model everything else reads. |
| [pipeline/app/rules/planner/room_facts.py](../../pipeline/app/rules/planner/room_facts.py) | Edit | [1] Per-edge walls, shape, anchor ranking, prompt text, path polygons in blocked zones. [2] `pockets` and `free_floor` from the ported sweep. | The facts the digest, solver and prompts use. |
| [pipeline/app/rules/protected_paths.py](../../pipeline/app/rules/protected_paths.py) | Edit | [1] Routes between door pairs through the eroded polygon, always, ported from the legacy `_polygon_paths`; flag helper removed. | Passages stay clear in any shape. |
| [web/src/lib/types.ts](../../web/src/lib/types.ts), [web/src/lib/room.ts](../../web/src/lib/room.ts), [web/src/lib/shapes.ts](../../web/src/lib/shapes.ts), [web/src/components/RoomScene.tsx](../../web/src/components/RoomScene.tsx), [web/src/screens/Generate.tsx](../../web/src/screens/Generate.tsx) | Edit | [1] Drop the wall name; size from `width`; alcove, Z and U at the builder's proportions; examples by shape. | Matches the builder and the benchmark. |
| [pipeline/scripts/benchmark_requests.json](../../pipeline/scripts/benchmark_requests.json), [pipeline/scripts/review.py](../../pipeline/scripts/review.py) | Edit | [1] Wall names dropped; walls and polygon area in the review's room text. [2] Add a Z-shaped living room, an L-shaped studio, an alcove bedroom, a U-shaped dining room. [4] An angled living room. | Cases for every shape. |
| [pipeline/app/rules/layout_rules.py](../../pipeline/app/rules/layout_rules.py) | Edit | [1] `coordinate_system_block` lists walls by index. [4] Rotation text. | Models see the real walls. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | [1] Intent schema and prompt without the path flag. [2] Product fit by largest pocket. | Paths always; fit by pocket. |
| [pipeline/app/rules/planner/intent_packet.py](../../pipeline/app/rules/planner/intent_packet.py) | Edit | [1] Drop `requires_straight_circulation_path`. | Same. |
| [pipeline/app/rules/planner/feasibility_digest.py](../../pipeline/app/rules/planner/feasibility_digest.py) | Edit | [2] Sectional and anchor fit by largest pocket. | Selection sizes to the real room. |
| [pipeline/app/rules/selection/catalog.py](../../pipeline/app/rules/selection/catalog.py), [pipeline/app/rules/selection/preflight.py](../../pipeline/app/rules/selection/preflight.py) | Edit | [1] No path zones for a routed path. [2] Fit against the largest pocket. [4] Diagonal orientations. | Same. |
| [pipeline/app/rules/layout/solver.py](../../pipeline/app/rules/layout/solver.py) | Edit | [2] Pockets in `_Room`; zone candidates per pocket; zone key with pocket. [3] Wall candidates per edge; free-piece corners from the polygon. [4] Any-angle `_turn`. | Zones in pockets; placement on real walls. |
| [pipeline/app/rules/geometry/candidates.py](../../pipeline/app/rules/geometry/candidates.py) | Edit | [3] `_wall_candidates` per edge; grid and centered candidates clipped to the polygon; wall helpers by edge. | Spots on every wall. |
| [pipeline/app/rules/geometry/living_group.py](../../pipeline/app/rules/geometry/living_group.py), [pipeline/app/rules/geometry/primitives.py](../../pipeline/app/rules/geometry/primitives.py) | Edit | [1] Oriented opening blockers. [4] Any-angle group bounds and extents. | Diagonal walls. |
| [pipeline/app/rules/planner/seed_guidance.py](../../pipeline/app/rules/planner/seed_guidance.py) | Edit | [3] Opposite and adjacent walls by geometry; edge indexes. | Guidance on real walls. |
| [pipeline/app/rules/layout/relations.py](../../pipeline/app/rules/layout/relations.py) | Edit | [1] Oriented window blockers. [3] `wall_behind`, `wall_ahead`, per-edge bands. | Checks on real walls. |
| [pipeline/app/rules/layout/normalization.py](../../pipeline/app/rules/layout/normalization.py) | Edit | [3] Snap to the edge behind at its yaw. | No teleport to an axis wall. |
| [pipeline/app/rules/layout/validation_geometry.py](../../pipeline/app/rules/layout/validation_geometry.py), [pipeline/app/rules/layout/validation_functional.py](../../pipeline/app/rules/layout/validation_functional.py) | Edit | [1] `translations_to_clear_m` only for axis-aligned paths. [3] Wall checks by edge; sofa wall gap by the edge behind; focal alignment by edge; findings carry index and label. | No cardinal gates or name crashes. |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | [1] Selection prompt states walls and area; review prompt lists path centerlines. [2] Pockets in the selection and review prompts. [4] `allowed()` accepts wall-aligned yaws. | Models see the real room. |
| [pipeline/app/preview.py](../../pipeline/app/preview.py) | Edit | [1] Draw openings on their edge and routed paths as polygons. | Previews match. |
| [pipeline/tests/test_solver.py](../../pipeline/tests/test_solver.py), [pipeline/tests/test_rules.py](../../pipeline/tests/test_rules.py), [pipeline/tests/test_stages.py](../../pipeline/tests/test_stages.py), [pipeline/tests/fixtures/legacy_seed_cleanup.json](../../pipeline/tests/fixtures/legacy_seed_cleanup.json) | Edit | Shape helper for the five presets and two templates; tests per phase (below); fixture wall names dropped. | Verify each phase. |
| [web/tests/run.test.ts](../../web/tests/run.test.ts) | Edit | [1] Opening fields. | Same. |
| [pipeline/README.md](../../pipeline/README.md), [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md), [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md), [docs/pipeline/adding-a-room-type.md](../pipeline/adding-a-room-type.md), [docs/pipeline/adding-a-product-category.md](../pipeline/adding-a-product-category.md) | Edit | Request fields, step 2 (walls, pockets, paths), the "bounding rectangle" pitfall. | Docs describe `main`. |

## Verification

From `pipeline/` with `conda activate livinit`. A test helper builds the five
presets and the Z and U templates at the builder's default proportions (the
table above).

1. Phase 1: `room_walls` returns one wall per edge for every shape with the
   builder's labels; the 4 x 5 m Z-room facts report 8 walls and 15.6 m²; a
   door on an inner wall gets its opening rectangle on that wall; two doors in
   different pockets of a Z-room get a route through the throat that stays
   inside the outline; a rectangle with opposing doors gets the straight path
   it had, and one with doors on adjacent walls gets none.
2. Phase 2: pockets per shape match decision 3; the living room's sitting
   group lands in the largest pocket and the dining group in another, with no
   blocking findings; the three designs of a Z-room differ by pocket.
3. Phase 3: a wall TV and a sideboard on the Z-room's inner walls pass the
   wall checks; the same selection solved in each shape has every item inside
   and no blocking findings; the existing solver tests still pass.
4. Phase 4: wall art on the angled wall passes; a sofa facing the angled
   wall's normal is accepted by the review's `allowed()`.

```bash
python -m pytest tests/test_solver.py tests/test_rules.py tests/test_stages.py tests/test_delivery.py
cd ../web && pnpm test
```

Then the benchmark with the new cases, 3 runs each, and
`scripts/review.py`: every design delivered, layout quality for the rectangle
cases within chance of the [2026-10-12 report](../pipeline/report-2026-10-12.md)
(#54 final: use of space 3.40, layout quality 3.26, overall 3.25), and the
reviewer's use-of-space score for the shaped rooms at or above the rectangle
cases'.

## Phase 1 changes (2026-10-13)

Shipped from the pipeline side; the web form changes landed with it.

- **Walls.** `RoomWall` is one record per edge of the counter-clockwise
  outline: `index`, `start`, `end`, `length`, inward `normal`, the builder's
  `label` (ported from `wallNames.ts`, tie cases included) and `on_box`.
  `facing` is the cardinal word for an edge within 22.5° of an axis, None for
  a corner wall. `room_walls` keeps every edge; `nearest_wall` is by distance
  to any edge; `nearest_axis_wall` serves the checks and normalization that
  still reason in four cardinal walls until phase 3 (no crash on a corner
  wall). `WALL_NAMES`, `WALL_CARDINAL_TOLERANCE` and `infer_wall_from_center`
  are gone.
- **Openings.** The wall is the edge nearest `center`; `wall` and `depth` in
  the request are accepted and ignored (depth is read as the wall thickness).
  Opening and clearance rectangles are built on the edge from `center` and
  `width`: axis-aligned on an axis wall, an oriented `polygon` with its bounds
  as `center`/`width`/`depth` on a corner wall. Records carry `wall` (edge
  index), `wall_label`, `facing` and `inward_normal`. Window blockers use the
  same rectangle. `door_dimensions` takes `width` as the opening and `depth`
  as the thickness, without the old "turned opening" heuristics.
- **Paths.** `build_protected_paths(room_vertices, room_doors)` always runs,
  for every pair of doors whose inward normals oppose (dot < -0.5), inner
  walls included. A straight band inside the outline keeps the axis record
  (a rectangle with opposing doors gets exactly the old `center`, `width`,
  `depth`); otherwise the legacy route through the polygon eroded by half the
  width, buffered with flat ends and clipped, stored as `axis: "routed"` with
  `centerline` and `polygon`; retried at 0.6 m, else dropped. `wall_pair` holds
  edge indexes, plus `wall_labels`. The `requires_straight_circulation_path`
  flag, its schema line, prompt line and helpers are gone. Routed paths give
  no `translations_to_clear_m` and no preflight zones; the room facts' blocked
  zone carries the polygon; previews draw paths as grey bands.
- **Room facts.** `walls` lists every edge with index, label, facing,
  endpoints, length, openings (`at` along the edge) and clear spans; `shape`
  is `rectangle` or `polygon` with `vertex_count`; `candidate_anchor_walls`
  carry index, label and facing, ranked by usable length with the facing name
  as the tie-break (so rectangles rank exactly as before; a pure index
  tie-break mirrored the benchmark studio and moved its dining group 0.86 m
  from the bed) and the index last. The digest's `anchor_wall` has `wall`,
  `label`, `facing`; seed guidance takes each ranked edge's facing.
- **Prompts.** `coordinate_system_block(room_vertices)` lists the walls by
  index and label with endpoints. The selection prompt and the review prompt
  state the shape and polygon area; the review prompt lists each path's band
  or centerline and shows openings as "wall 3 (Back (inner))";
  `scripts/review.py` does the same.
- **Fixtures and tests.** `wall` keys dropped from `legacy_seed_cleanup.json`
  and `benchmark_requests.json`, the flag from the fixture intents (their
  paths now come from the doors). `tests/test_rules.py` gains `shape()` for
  the five presets and the Z and U templates at the builder's defaults, the
  label table for all seven, the Z-room facts (8 walls, 15.56 m², door on
  "Back (inner)"), the routed Z-room path through the throat, the rectangle's
  straight record and the adjacent-door case. The L-shaped solver test also
  runs on the angled preset.

Results: `python -m pytest tests/test_solver.py tests/test_rules.py
tests/test_stages.py tests/test_delivery.py tests/test_review.py
tests/test_runtime.py` passes (169 tests, 11 new; `test_llm.py` needs API
keys and was not run). A smoke run built walls,
openings, paths and facts for all seven shapes with a door on every wall,
corner and inner walls included, without an error.

Deferred to later phases, as planned: fit and the selection prompt's
viewing reference still use the bounding box; the solver's wall candidates
and the cardinal wall checks still use the box walls; a routed path is never
split into preflight zones.

## Unresolved questions

None. Decisions 2, 3, 5 and 7 were settled from the builder's and the legacy
pipeline's code on 2026-10-13; any of them can be reopened before the phase
that uses it. The issue text still says seven presets and 13.9 m².
