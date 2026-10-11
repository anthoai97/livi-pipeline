# Bring the room rules in line with design practice

Issue: [#54](https://github.com/anthoai97/livi-pipeline/issues/54) (open).

## Objective

Review every room-specific rule (living room, bedroom, dining room, studio)
against the clearances, distances and completeness standards designers work
from, fix the ones that are off, add the few that are missing, and replace the
legacy policy texts with short, current ones.

## Current behavior (verified on `main`, 2026-10-11)

Where the rules live:

- Thresholds: [layout/constants.py](../../pipeline/app/rules/layout/constants.py),
  [layout/bedroom.py](../../pipeline/app/rules/layout/bedroom.py) (`BED_ACCESS_MIN_M` 0.56,
  headboard gap 0.20, bedside gap 0.30), [layout/comfort.py](../../pipeline/app/rules/layout/comfort.py)
  (`TV_VIEW_WIDTH_RANGE` 1.2 to 3.5 x width, sofa band 0.56),
  [layout_rules.py](../../pipeline/app/rules/layout_rules.py) (sofa to table 0.46 to 0.61,
  sofa to wall 0.08 to 0.15, paths 0.91 and 0.56),
  [categories.py](../../pipeline/app/rules/categories.py) (`WALL_MOUNT_Z` 1.6),
  [door_geometry.py](../../pipeline/app/rules/door_geometry.py) (`DOOR_CLEARANCE` 0.6).
- Room size classes: `room_scale` in [planner/_util.py](../../pipeline/app/rules/planner/_util.py):
  compact under 18 m², medium under 30 m², spacious above, for every room type.
  The class sets the caps (`ROLE_CAPS`, `CATEGORY_CAPS` in
  [fit_policy.py](../../pipeline/app/rules/planner/fit_policy.py)), the density floor
  and the completion strategy ("keep furniture count low" for compact).
- Required items: `room_requirements` and `validate_selection` in
  [selection/validation.py](../../pipeline/app/rules/selection/validation.py);
  lighting is advisory (`BLOCKING_REQUIRED_ROLES` in
  [selection/constants.py](../../pipeline/app/rules/selection/constants.py) holds
  seating and surface only).
- Policy texts: `STUDIO_`, `DINING_` and `BEDROOM_FURNISHING_GUIDANCE` in
  [room_policy.py](../../pipeline/app/rules/room_policy.py); the room blocks of
  `build_rules_block` and `build_selection_guidance_block` in `layout_rules.py`;
  the room bullets of `_intent_prompt` ([shared_stages.py](../../pipeline/app/shared_stages.py))
  and `_selection_prompt` ([variant_stages.py](../../pipeline/app/variant_stages.py)).
  They still instruct the model about existing-room edits, frozen edit scope,
  explaining omissions in gaps, and reporting infeasibility. None of those
  exist: there are no edits, the model writes no reasons, and a failed check
  removes items instead of failing the design.
- The solver mirrors the checks: `ACCESS_M = BED_ACCESS_MIN_M` is the width of
  every service band (bed sides and foot, chair pull-out, storage fronts, sofa
  front) in [layout/solver.py](../../pipeline/app/rules/layout/solver.py);
  `SOFA_TABLE_GAPS_M` derives from the sofa-to-table range.
- Scores ([2026-10-11 report](../pipeline/report-2026-10-11.md)): dining
  completeness 3.0, studio overall 3.0 and use of space 3.0, living room use of
  space 3.44; bedroom circulation 4.89 and grouping 5.0.
- Bugs found in the rule inventory: `_is_media_display_asset`
  ([layout/relations.py](../../pipeline/app/rules/layout/relations.py)) matches
  `tv_stand` as a display, so a TV on a stand skips the support checks; a raw
  `chair` is a dining chair to the checker but an accent chair to the solver;
  a studio with no dining group gets a critical `dining_completeness` finding
  ([layout/analysis.py](../../pipeline/app/rules/layout/analysis.py) runs the
  dining checks for every studio); a studio cabinet is reported under both the
  bedroom and the dining storage-front keys; the selection prompt requires a
  rug in a studio while `room_requirements` and the layout rules make it
  optional; the dining light gap in
  [selection/preflight.py](../../pipeline/app/rules/selection/preflight.py) is
  checked for dining rooms only; `bed_count > 1 or bed_count != 1`.

## Scope

- Numeric rules per room, with the solver's mirrored bands.
- Completeness rules per room.
- Design rules text and the legacy policy texts.
- The listed bugs.

Non-goals: room shapes and walls (#53); distinct layouts (#52); new room
types; new catalog categories; the budget rules.

## Decisions

Figures are the ranges designers and the usual planning references (NKBA
guidelines, Panero and Zelnik, Time-Saver Standards) agree on, chosen at the
value that suits apartment-scale rooms.

### Shared rules

| Rule | Now | Change to | Why |
| --- | --- | --- | --- |
| Door clear zone | opening width x 0.60 m deep | opening width x opening width (0.9 m default) | The leaf swings through its own width; 0.6 m leaves a 0.9 m door blocked. |
| Art center height (`WALL_MOUNT_Z`) | 1.60 m | 1.50 m | Eye level 1.45 to 1.55 m; galleries hang at 1.52 m. |
| TV center, sofa viewing | 1.10 m | keep | Seated eye level. |
| TV center, bed viewing | 1.10 m | 1.30 m | A reclined viewer's eye is higher. |
| TV distance | 1.2 to 3.5 x screen width | 1.5 to 2.8 x screen width | 1.3 to 2.4 x the diagonal, the comfortable band for HD and 4K. |
| Sofa back to wall | 0.08 to 0.15 m, flush flagged | 0 to 0.15 m | A flush sofa is normal; the seed places it at 0.05 m and the check flagged it. |
| Secondary path | 0.56 m | 0.60 m | 24 in minimum for a single-piece access band. |
| Room size class | 18 / 30 m² for every room | living 18 / 30, bedroom 11 / 16, dining 10 / 16, studio 25 / 40 | A 15 m² bedroom is a normal bedroom, not a compact living room. |
| Desk chair pull-out | checked in bedroom and studio | every room | The solver reserves it everywhere; a living-room desk needs it too. |
| Floor lamp reach | nearest seat of any kind | lounge seats; dining chairs only when there is none | Matches the solver and the rule's purpose. |
| Ceiling height | 2.8 m fixed (`CEILING_HEIGHT_M`) for ceiling mounts, the art cap and the pendant gap | the request's `wall_height` | A 2.4 m room gets a chandelier through the ceiling; a 3.2 m room gets one 0.4 m below it. |
| Tall furniture at a window | text only | a non-blocking P2 finding: a floor piece taller than the sill (`sill_height`, default 0.9 m) in front of a window | The review acts on measured findings, not reminders. |
| Secondary zone in a spacious room | digest note "avoid separate room zones" | a spacious room may add a reading corner or a desk zone | What designers do with the space; matches the pockets in #53. |

### Living room

| Rule | Now | Change to | Why |
| --- | --- | --- | --- |
| Sofa to coffee table | 0.46 to 0.61 m | 0.38 to 0.50 m | 14 to 18 in: reachable from the seat with legroom. |
| Coffee table size | none | width 0.5 to 0.75 x sofa width; height 0.35 to 0.50 m (selection) | Proportion and seat-height rules of thumb. |
| Side table height | none | 0.45 to 0.65 m (selection) | Within reach of the arm. |
| Sitting rug width | at least 90% of the sofa width | at least the sofa width; 0.3 m wider preferred (solver cost) | Front legs on the rug, rug past the arms. |
| Lighting | advisory | required: one lamp with the seating group | Every seat has a light; the report asks for it but nothing enforces it. |
| Accent seating | optional | at least one accent seat in a medium room, two in a spacious one | A seating group is a conversation group, not a lone sofa. |
| TV size facts | studio only | every room with a TV candidate: the screen's distance range against the room's opposed-wall span | The model picks a screen that suits the room. |
| Accent chair to table, facing seats, media centerline | 0.65 m, 1.22 to 2.74 m text, 0.46 m | keep | Within practice. |

### Bedroom

| Rule | Now | Change to | Why |
| --- | --- | --- | --- |
| Bed side access | 0.56 m on one side | 0.60 m; both sides for beds 1.4 m wide or more unless the room is compact | 24 in minimum; a double bed is made and entered from both sides. |
| Foot access | 0.56 m | 0.60 m; 0.91 m when the foot strip is the route to the door | A walkway needs the main-path width. |
| Storage front | 0.56 m | 0.76 m | A 0.45 m drawer plus the person opening it. |
| Clothes storage | opt-in | one dresser or wardrobe required, unless compact or excluded | A bedroom without clothes storage is unfinished. |
| Nightstands | pair preferred | one per open side for beds 1.4 m or wider in medium and spacious rooms | Bedside service on each side that is used. |
| Bed rug | underlap rule only | centered rug at least bed width + 0.9 m wide, or runners | 0.45 m of rug to step onto each side. |
| TV center | 1.10 m | 1.30 m | See shared rules. |
| Bed and door | none | design rule: the bed is seen from the door but not in line with it | The usual placement rule. |

### Dining room

| Rule | Now | Change to | Why |
| --- | --- | --- | --- |
| Chair pull-out band | 0.56 m behind the chair footprint | 0.46 m; 0.56 m where a path passes behind | 0.9 m from the table edge to sit down, 1.1 m to pass behind. |
| Table depth for two sides | none | at least 0.75 m to seat both long sides, else one side (selection) | Two place settings face to face need 0.75 m; 0.9 m is comfortable. |
| Seat pitch | max(0.60 m, chair width + 0.05) | keep; 0.65 m preferred (solver cost) | 24 in minimum, 26 in comfortable. |
| Default chairs | 4 | 4; 6 in a spacious dining room when the table seats them | A 16 m² dining room is laid for six. |
| Pendant height | at least 0.76 m above the table | 0.76 to 0.90 m | 30 to 36 in. |
| Pendant width | none | at most table width minus 0.30 m (selection) | 15 cm inside each table edge. |
| Sideboard front | 0.56 m | 0.76 m | Doors and drawers plus a person. |
| Rug | covers 95% of table and chairs | extends 0.60 m past each table edge | Chairs stay on the rug when pulled out. |
| Completeness | table and chairs | medium and spacious rooms also need one serving or storage piece and one overhead light, unless excluded | The lowest completeness score (3.0) comes from bare dining rooms. |

### Studio

| Rule | Now | Change to | Why |
| --- | --- | --- | --- |
| Dining group | always required | required when requested or the studio is 25 m² or more; otherwise optional, and the completeness check runs only when a dining group exists | A small studio eats at a counter or a two-seat table; forcing a group crowds it. |
| Rug | prompt requires, checks optional | optional; when selected, one per zone | Removes the contradiction. |
| Zoning | "each takes its own part" | design rules: the bed in the corner farthest from the door; the sofa's back or a shelf separates sitting from sleeping; dining by the entry wall | How studios are zoned. |
| Default chairs, TV facing, divider | 2, 45°, allowed | keep | Within practice. |

### What each room gets

Products come only from the required items, the optional roles
(`COUNT_GUIDANCE_ROLE_CATEGORIES`) and `DECOR_CATEGORIES` (planter,
sculpture, floor mirror, wall mirror). Wall art, pendants and chandeliers are
never searched unless requested, so the dining policy's "consider a pendant
and art" cannot be followed. Each room's set becomes:

| Room | Required | Required from medium size | Optional slots | On request |
| --- | --- | --- | --- | --- |
| Living | sofa; coffee table or side table (a desk counts only when work is requested); rug; lamp; plant | one accent seat (two when spacious); storage or media unit from medium, not only spacious | side table, floor lamp, storage, media, wall art; a reading or desk zone when spacious | TV, desk |
| Bedroom | bed; one nightstand; one bedside lamp; rug; plant | a nightstand per open side for beds 1.4 m or wider; dresser or wardrobe | bench, accent chair, floor lamp, floor or wall mirror, wall art | wardrobe (hanging storage), TV, desk |
| Dining | table; chairs (4 default, 6 when spacious) ; plant | sideboard or cabinet; pendant or chandelier | wall art or mirror, rug | bar cart, bench |
| Studio | bed; nightstand; bedside lamp; sofa; plant optional | dining table with 2 chairs from 25 m², or when requested; dresser or wardrobe | coffee or side table, floor lamp, rug per zone, bookcase, overhead light | TV, desk |

Slots follow this table: `DECOR_CATEGORIES` gains `wall_art` and
`painting`; `room_requirements.optional_roles` gains an `overhead_lighting`
role (pendant, chandelier) for dining rooms and studios; the studio's
optional categories gain storage, floor lamp and bookcase.

### Texts

- The three `*_FURNISHING_GUIDANCE` strings become at most six sentences
  each: the core group, default counts, what is opt-in, what is dropped first.
- The room blocks in `build_rules_block` become short numbered lines built
  from the constants, in the style of `DESIGN_RULES`.
- Every sentence about existing-room edits, frozen or editable scope, saved
  viewing targets, explaining omissions, reporting infeasibility, never
  silently repairing or removing, and selection feedback goes from
  `_intent_prompt`, `_selection_prompt`, `build_selection_guidance_block`,
  the P0 to P2 rules and the room blocks.

## Phases

### 1. Thresholds and bugs

- Change the constants in the shared, living, bedroom, dining and studio
  tables. New constants: `BEDROOM_TV_CENTER_Z` 1.3, `STORAGE_FRONT_M` 0.76,
  `DINING_PULLOUT_M` 0.46, `DINING_LIGHT_TABLE_MAX_GAP_M` 0.90,
  `SITTING_RUG_MIN_WIDTH_RATIO` 1.0, secondary path 0.60.
- `room_scale(area_sqm, room_type)` with per-room cutoffs; callers pass the
  room type.
- The solver's `_bands` takes the width per band: bed sides and foot 0.60
  (0.91 for a foot strip on a door route), chair pull-out 0.46, storage front
  0.76, sofa front 0.56. `SOFA_TABLE_GAPS_M` follows the new range.
- Door clear zone depth = opening width in `door_geometry` and the P0 text.
- Ceiling height: `ceiling_mount_z`, `_wall_mount_z`, the ceiling and wall
  mount checks, the dining light gap, the solver's `_placement`,
  normalization's `_correct_z` and the P0 text take the room's `wall_height`
  (2.8 m only when the request has none). `CEILING_HEIGHT_M` goes.
- A `window_front_violations` P2 finding (non-blocking): a floor piece taller
  than the window's sill overlapping the strip in front of the window.
- The desk check runs in every room; the floor-lamp check measures lounge
  seats; the bed-viewing TV height applies when the viewer is a bed.
- Bugs: `_is_media_display_asset` matches whole categories; a `chair`
  selected with a dining table is a `dining_chair` in both checker and solver
  (`normalize_category` with the selection's context, or the selection
  relabels it); the studio runs the dining checks only with a dining group and
  reports a storage front once; the dining light gap is checked for studios;
  `bed_count != 1`.

### 2. Completeness

- Slots: `DECOR_CATEGORIES` adds `wall_art` and `painting`;
  `room_requirements.optional_roles` adds `overhead_lighting` (pendant,
  chandelier) for dining rooms and studios; the living-room surface role is
  coffee table and side table, with desk only when work is requested; the
  digest's optional categories add storage for medium living rooms and
  storage, floor lamp and bookcase for studios.
- Living room: lighting joins `BLOCKING_REQUIRED_ROLES`; one accent seat is
  required in medium rooms and two in spacious ones; selection checks for
  coffee table size and side table height.
- TV size facts (`comfort_placement_facts` and the opposed-wall reference) go
  into `_selection_prompt` for every room whose candidates include a TV.
- `dining_chair_count` defaults to 6 in a spacious dining room; the DINING
  GROUP check accepts 4 to 6 there when the table's edges seat them.
- Bedroom: `room_requirements` adds one nightstand and one bedside lamp, a
  nightstand per open side for beds 1.4 m or wider in medium and spacious
  rooms, and clothes storage (dresser, wardrobe) unless compact or excluded;
  `validate_selection` errors to match; rug width check.
- Dining room: `room_requirements` adds a serving or storage piece
  (sideboard, cabinet) and an overhead light (pendant, chandelier) in medium
  and spacious rooms unless excluded; table depth and pendant width checks.
- Studio: the dining requirement becomes conditional (requested, or 25 m²
  and up); a nightstand and bedside lamp are required; clothes storage from
  medium size; the rug line in `_selection_prompt` says optional.
- Every new required or optional piece gets a search slot through
  `room_requirements`, so the model has candidates to pick.

### 3. Design rules and texts

- `DESIGN_RULES` gains the bedroom door rule and the three studio zoning
  rules; the living-room line uses the new sofa-to-table range.
- Rewrite the three policy strings; replace the `build_rules_block` room
  blocks and the `build_selection_guidance_block` texts with short lines from
  the constants; strip the stale sentences from the prompts.
- `_room_completion_guidance`: the spacious note says a reading corner or
  desk zone may use the extra space instead of "avoid separate room zones".

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/rules/layout/constants.py](../../pipeline/app/rules/layout/constants.py) | Edit | Dining pull-out 0.46, light gap max 0.90, new storage-front and bedroom TV constants; tier lists unchanged. | Phase 1 thresholds. |
| [pipeline/app/rules/layout/bedroom.py](../../pipeline/app/rules/layout/bedroom.py) | Edit | Access 0.60; two-sided rule for wide beds; foot strip 0.91 on a door route; storage front 0.76; TV height 1.30. | Bedroom table. |
| [pipeline/app/rules/layout/dining.py](../../pipeline/app/rules/layout/dining.py) | Edit | Pull-out 0.46 (0.56 on a path); storage front 0.76; light 0.76 to 0.90; rug 0.60 past the edges. | Dining table. |
| [pipeline/app/rules/layout/comfort.py](../../pipeline/app/rules/layout/comfort.py) | Edit | TV range 1.5 to 2.8; rug width ratio 1.0; bed viewer height. | Shared and living tables. |
| [pipeline/app/rules/layout_rules.py](../../pipeline/app/rules/layout_rules.py) | Edit | Sofa to table 0.38 to 0.50; sofa to wall 0 to 0.15; secondary path 0.60; room blocks and selection guidance rewritten; stale sentences removed; new design rules. | Shared and living tables; phase 3. |
| [pipeline/app/rules/categories.py](../../pipeline/app/rules/categories.py) | Edit | `WALL_MOUNT_Z` 1.5; `ceiling_mount_z(height, ceiling)`; `CEILING_HEIGHT_M` removed. | Art height; ceiling from the request. |
| [pipeline/app/rules/layout/validation_geometry.py](../../pipeline/app/rules/layout/validation_geometry.py), [pipeline/app/rules/layout/normalization.py](../../pipeline/app/rules/layout/normalization.py) | Edit | Ceiling and wall mount checks and z correction take the room's ceiling; `window_front_violations`. | Ceiling height; window finding. |
| [pipeline/app/rules/door_geometry.py](../../pipeline/app/rules/door_geometry.py) | Edit | Clear zone depth = opening width. | Door swing. |
| [pipeline/app/rules/planner/_util.py](../../pipeline/app/rules/planner/_util.py) | Edit | `room_scale(area_sqm, room_type)`. | Per-room size classes. |
| [pipeline/app/rules/planner/feasibility_digest.py](../../pipeline/app/rules/planner/feasibility_digest.py), [pipeline/app/rules/planner/fit_check.py](../../pipeline/app/rules/planner/fit_check.py) | Edit | Pass the room type to `room_scale`; bedroom optional categories include storage. | Same. |
| [pipeline/app/rules/layout/solver.py](../../pipeline/app/rules/layout/solver.py) | Edit | Band widths per kind; rug width cost; seat pitch 0.65 cost. | Mirror the checks. |
| [pipeline/app/rules/layout/analysis.py](../../pipeline/app/rules/layout/analysis.py) | Edit | Desk check in every room; studio dining checks only with a dining group; one storage-front report. | Phase 1 bugs. |
| [pipeline/app/rules/layout/relations.py](../../pipeline/app/rules/layout/relations.py) | Edit | `_is_media_display_asset` by whole category. | TV stand bug. |
| [pipeline/app/rules/layout/validation_functional.py](../../pipeline/app/rules/layout/validation_functional.py) | Edit | Floor lamp reach to lounge seats; sofa wall gap from 0. | Shared table. |
| [pipeline/app/rules/planner/taxonomy.py](../../pipeline/app/rules/planner/taxonomy.py) | Edit | `chair` with a dining table resolves to `dining_chair`. | Chair bug. |
| [pipeline/app/rules/selection/validation.py](../../pipeline/app/rules/selection/validation.py) | Edit | Required items per room (phase 2) and their errors; coffee table, side table, table depth, pendant width and rug checks; `bed_count != 1`. | Completeness. |
| [pipeline/app/rules/selection/constants.py](../../pipeline/app/rules/selection/constants.py) | Edit | Lighting in `BLOCKING_REQUIRED_ROLES`; the living surface role without desk. | Living completeness. |
| [pipeline/app/rules/planner/taxonomy.py](../../pipeline/app/rules/planner/taxonomy.py) | Edit | `overhead_lighting` role (pendant, chandelier) in the count-guidance roles. | Dining and studio lights get searched. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | `DECOR_CATEGORIES` adds `wall_art`, `painting`; the overhead-lighting slot for dining rooms and studios. | Art and pendants get searched. |
| [pipeline/app/rules/selection/preflight.py](../../pipeline/app/rules/selection/preflight.py) | Edit | Dining light gap for studios too. | Bug. |
| [pipeline/app/rules/room_policy.py](../../pipeline/app/rules/room_policy.py) | Edit | Three short policy strings; dining requirement helper for studios by area; `dining_chair_count` 6 when spacious. | Phase 3 texts; studio and dining rules. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py), [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | Stale sentences removed from the prompts; studio rug line; bedroom and dining completeness lines. | Phase 3 texts. |
| [pipeline/tests/test_rules_parity.py](../../pipeline/tests/test_rules_parity.py), [pipeline/tests/test_solver.py](../../pipeline/tests/test_solver.py), [pipeline/tests/test_stages.py](../../pipeline/tests/test_stages.py) | Edit | Fixtures and assertions at the new thresholds; one test per new check and per bug. | Verify. |
| [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md), [docs/pipeline/adding-a-room-type.md](../pipeline/adding-a-room-type.md), [docs/pipeline/adding-a-product-category.md](../pipeline/adding-a-product-category.md) | Edit | New values and required items; the 0.56 m note. | Docs describe `main`. |

## Verification

From `pipeline/` with `conda activate livinit`:

1. Phase 1: the parity fixtures measured at the new thresholds give the
   expected findings (a sofa 0.42 m from its table passes, 0.55 m fails; a
   flush sofa passes; a dresser with 0.60 m in front fails, 0.80 m passes); a
   TV on a stand gets the support checks; a studio with bed and sofa only has
   no dining finding; a 15 m² bedroom is `medium`.
2. Phase 2: a living room without a lamp fails selection; a medium bedroom
   without a dresser or wardrobe fails, a compact one passes; a medium dining
   room without a sideboard or pendant fails; a 20 m² studio without a dining
   table passes and a 30 m² one fails; the new size checks fail and pass on
   the boundary values.
3. Phase 3: the prompts contain none of the removed phrases (a test greps
   for "existing-room", "frozen", "infeasib", "explain"); the rules block for
   each room lists its numbered rules.

```bash
python -m pytest tests/test_rules_parity.py tests/test_solver.py tests/test_stages.py
```

Then the benchmark, 3 runs per case, and `scripts/review.py`. Targets against
the [2026-10-11 report](../pipeline/report-2026-10-11.md): dining completeness
above 3.0 by more than chance (0.5), studio overall and use of space above
3.0, no case's layout quality down by more than 0.5, every design delivered,
selection turns not up by more than one on average.

## Unresolved questions

None. The values were chosen from design practice as asked; any row above can
be reopened on its own.
