# Furnish any room shape

Issue: [#53](https://github.com/anthoai97/livi-pipeline/issues/53) (open).

## Objective

Furnish a room with any simple outline: the seven room-builder presets
(rectangle, L, alcove, T, angled, Z, U) with adjustable walls, and anything
else a user draws. The pipeline reads the real walls, area, pockets and
openings; places furniture along every wall, inner walls included; keeps the
passages between pockets clear; and tells the models what the room really is.

## Current behavior (verified on `main`, 2026-10-11)

Line numbers are from that day and drift; search for the named symbol.

- **Request.** `room_area` is the bounding box and `room_vertices` the
  outline ([contracts.py:39-43](../../pipeline/app/contracts.py)). A door or
  window is a dict with `center`, `width`, `depth`, `height` and an optional
  `wall` name from `top`, `right`, `bottom`, `left`. The web sends the name
  only for rectangles ([room.ts:61](../../web/src/lib/room.ts)); it already
  holds the wall as an edge index (`doorWall`, `room.ts:13`) but does not send it.
- **Walls.** `room_walls` ([door_geometry.py:67-86](../../pipeline/app/rules/door_geometry.py))
  keeps only edges within 0.2 rad of an axis and names them by facing, so a
  Z-room's two "back" edges share one name and a diagonal edge disappears.
  `nearest_wall` (`:89-94`) raises when no edge is near an axis.
- **Room facts.** One wall record per name with the bounding box's length
  ([room_facts.py:152-157, 233-257](../../pipeline/app/rules/planner/room_facts.py)),
  `"shape": "rectangle"` (`:277`), and anchor walls ranked on those phantom
  walls. Fit is judged against the bounding box in the digest
  (`feasibility_digest.py:190-197`), the catalog filter
  (`selection/catalog.py:228-238`, `shared_stages.py:436-441`) and the
  selection fit check (`selection/preflight.py:51-139`).
- **Paths.** Built only between doors on opposing named walls, spanning the
  box ([protected_paths.py:29-79](../../pipeline/app/rules/protected_paths.py)),
  and only when the interpret model sets `requires_straight_circulation_path`,
  which the prompt reserves for an explicit request
  (`shared_stages.py:209`). Non-rectangles never get one; rectangles rarely.
- **Placement.** Wall candidates come from the four box walls at fixed yaws
  ([candidates.py:45-82](../../pipeline/app/rules/geometry/candidates.py));
  the outline only removes those outside. The dining grid and free-piece
  corners use the box ([solver.py:559-572, 794-801](../../pipeline/app/rules/layout/solver.py)).
  Yaw-to-wall helpers snap to a name ([relations.py:260-286](../../pipeline/app/rules/layout/relations.py));
  normalization moves a wall piece to the nearest axis wall
  ([normalization.py:103-177](../../pipeline/app/rules/layout/normalization.py));
  the wall-mount and wall-aligned checks require an axis wall
  ([validation_geometry.py:246-310](../../pipeline/app/rules/layout/validation_geometry.py));
  the sofa wall gap skips non-axis sofas and can raise on a missing name
  ([validation_functional.py:145-167](../../pipeline/app/rules/layout/validation_functional.py)).
  Seed guidance reasons in a four-name algebra
  ([seed_guidance.py:54-70, 279-327](../../pipeline/app/rules/planner/seed_guidance.py)).
- **Prompts.** The coordinate block names four walls
  ([layout_rules.py:83-98](../../pipeline/app/rules/layout_rules.py)); the
  selection prompt sends `W x D` (`variant_stages.py:548`); the review allows
  quarter turns only (`variant_stages.py:1317, 1378-1384`).
- **Measured.** The same living room solved in a rectangle, an L and an
  angled room (6 x 5 m box): every item placed, no blocking findings, but the
  L squeezed the sitting group into its 3 m arm with the TV in the other arm,
  and the angled room left its diagonal wall and 4 m back wall empty.
- `cleanup.py` is called only from `tests/test_rules_parity.py`.

## Scope

Four phases, each shipped on its own, in this order. Rooms are simple
polygons with no holes. Phases 1 to 3 keep quarter-turn placement; phase 4
adds diagonal walls.

Non-goals: rooms as zone sets instead of room types (a later issue); the
room builder itself (outside this repo; it must send the same request);
furniture crossing a passage; curved walls.

## Decisions

Made from interior-design practice, as asked.

1. **Any simple polygon, no preset name.** The pipeline derives walls,
   pockets, area and paths from the vertices. A preset is a starting point in
   the builder; adjusted walls make it any shape.
2. **Openings carry their wall index.** `wall` becomes the edge index
   (edge i runs from vertex i to vertex i + 1); `center` stays. Replaced
   directly, no contract version. The pipeline builds the opening rectangle
   on that edge from `center` and `width`, so `depth` goes.
3. **A pocket is a usable rectangle.** The maximal axis-aligned rectangles
   inside the outline with both sides at least 1.2 m (the smallest zone: a
   desk with its chair pulled out), dropping any that larger pockets already
   cover. The Z-room gives two pockets; its 1.33 m throat is covered by both
   and is a passage. The L gives two arms sharing the corner. The U gives the
   band and two arms. The alcove gives the main room and the alcove.
4. **One zone per pocket.** A zone (sleeping, sitting with its media, dining,
   work) lives inside one pocket: its templates, bands and the sofa-to-TV
   sightline. Two zones may share a large pocket when both fit. Storage,
   lamps and free pieces go anywhere. A conversation group or a TV across a
   passage is what designers avoid, since the route cuts the group.
5. **Passages stay clear, always.** A route is kept between every pair of
   doors, 0.91 m wide (the main circulation width in the rules), through the
   polygon. The `requires_straight_circulation_path` flag goes: clear
   circulation between entries is not something a brief opts into.
6. **Angled walls in two steps.** Through phase 3 a diagonal edge bounds the
   pockets; the corner outside them is free floor for a plant, lamp or chair.
   Phase 4 lets wall pieces back onto it.
7. **Walls get a label for people and models.** The facing word, plus
   "inner" when the edge is not on the bounding box, numbered when repeated
   ("back", "back inner", "left 2"), as the builder shows them. Code uses the
   index.
8. **Designs differ by pocket.** The solver's zone plan key becomes
   `(kind, pocket, wall)`, so the dealing in #52 gives the three designs
   different pocket assignments when the room allows.

## Phases

### 1. Real walls, openings, area and paths

- **Contract and web.** `room_doors` and `room_windows` entries get
  `wall: int` and lose `depth` and the wall names. `room.ts` sends
  `draft.doorWall` and `draft.windowWall`; `types.ts` types the opening;
  `benchmark_requests.json` converts its six `"wall": "top"` entries (the
  test fixtures carry no wall names).
- **Walls.** `door_geometry.RoomWall` becomes one record per edge: `index`,
  `start`, `end`, `length`, inward unit `normal`, `label`, `on_box`.
  `room_walls` keeps every edge. `nearest_wall` is by distance to any edge.
  `door_wall` reads the index, falling back to the nearest edge to `center`.
  `door_opening_rect`, the door clearance zone and `door_inward_normal` build
  rectangles oriented along the edge. Window rectangles and blockers in
  `primitives.py` and `relations.py` do the same.
- **Room facts.** `walls` lists every edge with its length, label, openings
  and clear spans measured along the edge; `shape` is `rectangle` or
  `polygon` with the vertex count; `area` is the polygon area;
  `candidate_anchor_walls` rank edges. `format_room_facts_for_prompt` lists
  walls as "wall 3 (back inner), 1.33 m, door at 0.5 m". The digest's
  density budget and `room_scale` use the polygon area.
- **Paths.** `build_protected_paths` always runs: for each pair of doors, the
  shortest route between their inward points inside the polygon eroded by
  0.455 m (a straight segment when it fits, else through the eroded
  polygon's vertices), buffered to 0.91 m with flat ends and clipped to the
  room. Path records carry their polygon; `protected_path_polygon` reads it.
  The intent flag, its prompt line and `intent_requires_protected_path` go.
- **Coordinate prompt.** `coordinate_system_block` lists the walls by index
  and label with their endpoints instead of four names.

Result: the Z-room reports 8 walls and 13.9 m², keeps the route between its
doors clear, and selects for its size. Placement still uses the box walls.

### 2. Pockets and zone assignment

- **Pockets.** `room_facts.pockets`: each with `bounds`, `width`, `depth`,
  `area`, the wall indexes that bound it, and its openings, largest first.
  `free_floor` is the outline minus the pockets (the angled corner).
  Computed by sweeping the rectilinear outline's x and y cuts; an angled edge
  is handled by the same sweep on a 5 cm step along it.
- **Fit.** The sectional and product fit filters, the digest's anchor fit
  and the selection fit check measure against the largest pocket's sides, not
  the box. The selection prompt says the room's pockets and their sizes.
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
  compares edges. Findings name walls by index and label (`formatting.py`).
- `cleanup.py` is deleted with its parity tests.

Result: pieces stand on inner walls; a wall TV on the Z-room's "back inner"
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
| [pipeline/app/rules/door_geometry.py](../../pipeline/app/rules/door_geometry.py) | Edit | [1] `RoomWall` per edge with index, normal, label; `room_walls`, `nearest_wall`, `door_wall`, `door_inward_normal`, `door_opening_rect` and the clearance zone oriented along the edge; `WALL_NAMES` goes. | The wall model everything else reads. |
| [pipeline/app/rules/planner/room_facts.py](../../pipeline/app/rules/planner/room_facts.py) | Edit | [1] Per-edge walls, polygon area, shape, anchor ranking, prompt text. [2] `pockets` and `free_floor`. | The facts the digest, solver and prompts use. |
| [pipeline/app/rules/protected_paths.py](../../pipeline/app/rules/protected_paths.py) | Edit | [1] Routes between door pairs through the eroded polygon, always; path polygons stored; flag helper removed. | Passages stay clear in any shape. |
| [pipeline/app/contracts.py](../../pipeline/app/contracts.py) | Edit | [1] Opening model with `wall: int`, `center`, `width`, `height`, `sill_height`. | Openings on their wall. |
| [web/src/lib/types.ts](../../web/src/lib/types.ts), [web/src/lib/room.ts](../../web/src/lib/room.ts) | Edit | [1] Send the edge index; drop names and `depth`. | Matches the contract. |
| [pipeline/scripts/benchmark_requests.json](../../pipeline/scripts/benchmark_requests.json) | Edit | [1] Wall indexes. [2] Add a Z-shaped living room, an L-shaped studio, an alcove bedroom, a U-shaped dining room. [4] An angled living room. | Cases for every shape. |
| [pipeline/app/rules/layout_rules.py](../../pipeline/app/rules/layout_rules.py) | Edit | [1] `coordinate_system_block` lists walls by index. [4] Rotation text. | Models see the real walls. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | [1] Intent schema and prompt without the path flag. [2] Product fit by largest pocket. | Paths always; fit by pocket. |
| [pipeline/app/rules/planner/intent_packet.py](../../pipeline/app/rules/planner/intent_packet.py) | Edit | [1] Drop `requires_straight_circulation_path`. | Same. |
| [pipeline/app/rules/planner/feasibility_digest.py](../../pipeline/app/rules/planner/feasibility_digest.py) | Edit | [1] Area from the polygon. [2] Anchor fit by largest pocket. | Selection sizes to the real room. |
| [pipeline/app/rules/selection/catalog.py](../../pipeline/app/rules/selection/catalog.py), [pipeline/app/rules/selection/preflight.py](../../pipeline/app/rules/selection/preflight.py) | Edit | [2] Fit against the largest pocket. [4] Diagonal orientations. | Same. |
| [pipeline/app/rules/layout/solver.py](../../pipeline/app/rules/layout/solver.py) | Edit | [2] Pockets in `_Room`; zone candidates per pocket; zone key with pocket. [3] Wall candidates per edge; free-piece corners from the polygon. [4] Any-angle `_turn`. | Zones in pockets; placement on real walls. |
| [pipeline/app/rules/geometry/candidates.py](../../pipeline/app/rules/geometry/candidates.py) | Edit | [3] `_wall_candidates` per edge; grid and centered candidates clipped to the polygon; wall helpers by edge. | Spots on every wall. |
| [pipeline/app/rules/geometry/living_group.py](../../pipeline/app/rules/geometry/living_group.py), [pipeline/app/rules/geometry/primitives.py](../../pipeline/app/rules/geometry/primitives.py) | Edit | [1] Oriented opening blockers. [4] Any-angle group bounds and extents. | Diagonal walls. |
| [pipeline/app/rules/planner/seed_guidance.py](../../pipeline/app/rules/planner/seed_guidance.py) | Edit | [3] Opposite and adjacent walls by geometry; edge indexes. | Guidance on real walls. |
| [pipeline/app/rules/layout/relations.py](../../pipeline/app/rules/layout/relations.py) | Edit | [1] Oriented window blockers. [3] `wall_behind`, `wall_ahead`, per-edge bands. | Checks on real walls. |
| [pipeline/app/rules/layout/normalization.py](../../pipeline/app/rules/layout/normalization.py) | Edit | [3] Snap to the edge behind at its yaw. | No teleport to an axis wall. |
| [pipeline/app/rules/layout/validation_geometry.py](../../pipeline/app/rules/layout/validation_geometry.py), [pipeline/app/rules/layout/validation_functional.py](../../pipeline/app/rules/layout/validation_functional.py) | Edit | [3] Wall checks by edge; sofa wall gap by the edge behind; focal alignment by edge. | No cardinal gates or name crashes. |
| [pipeline/app/rules/layout/formatting.py](../../pipeline/app/rules/layout/formatting.py) | Edit | [3] Walls named by index and label. | Findings the review can act on. |
| [pipeline/app/rules/layout/cleanup.py](../../pipeline/app/rules/layout/cleanup.py) | Delete | [3] Unused in production. | Obsolete. |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | [1] Selection prompt states walls and area. [2] Pockets in the selection and review prompts. [4] `allowed()` accepts wall-aligned yaws. | Models see the real room. |
| [pipeline/app/preview.py](../../pipeline/app/preview.py) | Edit | [1] Draw openings on their edge. | Previews match. |
| [pipeline/tests/test_solver.py](../../pipeline/tests/test_solver.py), [pipeline/tests/test_rules_parity.py](../../pipeline/tests/test_rules_parity.py), [pipeline/tests/test_stages.py](../../pipeline/tests/test_stages.py) | Edit | Shape helpers for the seven presets; tests per phase (below); parity tests of cleanup removed. | Verify each phase. |
| [web/tests/run.test.ts](../../web/tests/run.test.ts) | Edit | [1] Opening fields. | Same. |
| [pipeline/README.md](../../pipeline/README.md), [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md), [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md), [docs/pipeline/adding-a-room-type.md](../pipeline/adding-a-room-type.md), [docs/pipeline/adding-a-product-category.md](../pipeline/adding-a-product-category.md) | Edit | Request fields, step 2 (walls, pockets, paths), the "bounding rectangle" pitfall. | Docs describe `main`. |

## Verification

From `pipeline/` with `conda activate livinit`. A test helper builds the seven
presets at the builder's default proportions, like `web/src/lib/shapes.ts`.

1. Phase 1: `room_walls` returns one wall per edge for every preset with the
   right labels; the Z-room facts report 8 walls and 13.9 m²; a door on an
   inner wall gets its opening rectangle on that wall; two doors in a Z-room
   get a route through the throat that stays inside the outline; a rectangle
   with opposing doors gets the straight path it had.
2. Phase 2: pockets per preset match the table in decision 3; the living
   room's sitting group lands in the largest pocket and the dining group in
   another, with no blocking findings; the three designs of a Z-room differ by
   pocket.
3. Phase 3: a wall TV and a sideboard on the Z-room's inner walls pass the
   wall checks; the same selection solved in each preset has every item
   inside and no blocking findings; the existing solver tests still pass.
4. Phase 4: wall art on the angled wall passes; a sofa facing the angled
   wall's normal is accepted by the review's `allowed()`.

```bash
python -m pytest tests/test_solver.py tests/test_rules_parity.py tests/test_stages.py tests/test_delivery.py
cd ../web && pnpm test
```

Then the benchmark with the new cases, 3 runs each, and
`scripts/review.py`: every design delivered, layout quality for the rectangle
cases within chance of the [2026-10-11 report](../pipeline/report-2026-10-11.md),
and the reviewer's use-of-space score for the shaped rooms at or above the
rectangle cases' 3.68.

## Unresolved questions

None. Decisions 3 to 6 were made from design practice as asked; any of them
can be reopened before the phase that uses it.
