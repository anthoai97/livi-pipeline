# Restore the legacy room layout rules

Issue: [#61](https://github.com/anthoai97/livi-pipeline/issues/61) (open). Comparison:
[legacy-rule-differences.md](../pipeline/legacy-rule-differences.md).

## Objective

The thresholds, required pieces, density limits and policy texts say what the
legacy `livinit_pipeline` says. The solver, the review and the #54 bug fixes
stay. Checks that only exist because of #54's design references go.

## Current behavior (verified on `main` d3ea93f, 2026-10-14)

#54 changed every row of section 1 and 2 of the comparison. The values live
in `layout_rules.py`, `layout/constants.py`, `layout/bedroom.py`,
`layout/comfort.py`, `categories.py`, `door_geometry.py`,
`planner/_util.py`, `planner/fit_policy.py`, `selection/constants.py`; the
required pieces in `room_requirements` (`selection/validation.py`); the
texts in `room_policy.py`, `layout_rules.py`, `shared_stages.py`,
`variant_stages.py`. The solver mirrors the bands (`layout/solver.py`).

## Scope

Sections 1 to 3 of the comparison. Not in scope: section 4 (checks dropped by
the solver re-architecture), room shapes (#53), the legacy edit flow and its
prompt sentences, the legacy fit recommendation and layout judge.

## Decisions

1. **Keep what is not a rule.** The room's `wall_height` for mounts (legacy
   fixed 2.8 m), the #54 bug fixes, the solver and the dealt layouts (#52),
   the `DESIGN_RULES` block for the review (its numbers follow the constants).
2. **Remove the #54 checks that the legacy did not have**, not just their
   thresholds: two-sided bed access, the 0.91 m bed-foot route, the desk check
   outside bedroom and studio, the pendant 0.90 m maximum and 0.15 m inset,
   one-sided seating under 0.75 m deep, the window-front finding, the coffee
   table, side table and bed rug size checks, the dining rug 0.60 m rule. The
   constants and the `DESIGN_RULES` lines added for them go with them.
3. **Required pieces are the legacy core groups.** The completeness pieces
   (`gap_ok` entries) go from `room_requirements`. Their search slots and
   optional digest categories stay, so the model still sees nightstands,
   sideboards, pendants and lamps as candidates, and the texts suggest them
   as the legacy texts did. The studio dining group is required again;
   dining chairs default to 4, or 2 in a studio; a space-first dining brief may
   drop to a smaller usable group, never below a stated count.
4. **Density is the legacy measure.** `ROLE_PROFILES` get back their
   clearance ring and weight, `ROOM_THRESHOLDS` the per-scale set (compact
   0.52 / 0.72 / 0.30, medium 0.55 / 0.75 / 0.32, spacious 0.60 / 0.82 /
   0.25), and selection overcrowding is two times the footprint over the
   available area. `FIT_ESTIMATE_THRESHOLDS` folds back into
   `ROOM_THRESHOLDS`. The `OVERCROWDED` warning after a fit step and the
   anchor-keeping rule from #54 phase 4 stay: they are flow, not a limit.
5. **Texts keep the current short form with the legacy content.** The three
   policy strings, the room rule lines and the selection guidance say the
   legacy values and the legacy suggest-and-opt-in policy. Sentences about
   edits, frozen scope and reporting infeasibility stay out; nothing reads
   them.

## Phases

### 1. Thresholds and size classes

- `layout_rules.py`: `PATH_SECONDARY_M` 0.56, sofa to table (0.46, 0.61),
  sofa to wall (0.08, 0.15); door zone text "0.60 m in front of any door".
- `layout/bedroom.py`: `BED_ACCESS_MIN_M` 0.56; `STORAGE_FRONT_M` 0.56;
  delete `BED_TWO_SIDED_MIN_WIDTH_M`, `bed_foot_access_m`, the
  `two_sided_required` row and `side_access_blocked`; foot band 0.56.
- `layout/dining.py`, `layout/constants.py`: pull-out 0.56 for every chair
  (delete `DINING_PULLOUT_M`, `path_behind`); storage front 0.56; delete
  `DINING_LIGHT_TABLE_MAX_GAP_M`, `light_too_high_over_table`,
  `DINING_RUG_EXTENSION_M`, `DINING_TWO_SIDED_MIN_DEPTH_M` and the
  one-sided envelope rule; dining rug back to 95% coverage of table and
  chairs (`rug_misses_seated_group`).
- `layout/comfort.py`: `TV_VIEW_WIDTH_RANGE` (1.2, 3.5),
  `SITTING_RUG_MIN_WIDTH_RATIO` 0.90.
- `layout/constants.py`, `relations.py`: delete `BEDROOM_TV_CENTER_Z`; a wall
  TV centers at 1.10 m for every viewer.
- `categories.py`: `WALL_MOUNT_Z` 1.6.
- `door_geometry.py`: clear zone depth 0.60 m (`DOOR_CLEARANCE`).
- `planner/_util.py`: `room_scale(area_sqm)` with 18 / 30 for every room;
  callers drop the room type.
- `layout/analysis.py`, `validation_functional.py`, `validation_geometry.py`:
  desk check in bedroom and studio only; floor lamp reach to the nearest seat
  of any kind; delete `compute_window_front_violations` and its P2 key.
- `layout/solver.py`: bands 0.56 for bed sides and foot, chair pull-out,
  storage fronts; `SOFA_TABLE_GAPS_M` follows the range; TV cost follows the
  range; drop the bed-foot route width.

### 2. Required pieces

- `selection/validation.py`: `room_requirements` keeps bed, sofa, surface
  (coffee table, side table or desk), rug, plant, dining table and chairs
  only; delete the `gap_ok` loop and `two_sided_bed`; `REQUIRED_CATEGORIES`
  gets back `desk` under surface and `lighting` as advisory.
- `room_policy.py`: `studio_dining_required` always true (delete it and
  `STUDIO_DINING_MIN_AREA_SQM`); `dining_chair_count` returns the stated
  count or 4; a space-first brief allows `min(2, target)` seats as before.
- `planner/feasibility_digest.py`: studio required roles include the dining
  group; optional categories back to the legacy lists (bedroom nightstand,
  dresser, table lamp, rug, planter; studio nightstand, table lamp, side
  table, planter); the completion note "avoid separate room zones".
- `selection/constants.py`, `selection/validation.py`: delete the size
  constants and `_size_errors` for coffee table, side table, bed rug, pendant
  inset and table depth.
- `selection/preflight.py`: dining light gap minimum only.
- `shared_stages.py`, `variant_stages.py`: the slots for lamps, nightstands,
  storage, sideboards and overhead lights stay; the prompt lines call them
  suggested, not required.

### 3. Density

The current limits are stricter than the legacy (a compact living room may
cover 30% of the usable floor; legacy 50%). The user chose the legacy limits
on 2026-10-14.

- `planner/fit_policy.py`: `ROLE_PROFILES` (width, depth, clearance, weight)
  with the legacy clearances; load = (w + 2c)(d + 2c) x weight;
  `ROOM_THRESHOLDS` per scale; delete `FIT_ESTIMATE_THRESHOLDS`.
- `planner/fit_check.py`, `feasibility_digest.py`, `room_facts.py`: thresholds
  by scale; the digest zeroes the sparse floor for dining rooms and studios.
- `selection/validation.py`: overcrowded when 2 x footprint of non-rug pieces
  exceeds the available area; sparse floor 0.30 / 0.32 / 0.25 of 2 x
  footprint for living rooms and bedrooms.

### 4. Texts

- `room_policy.py`: the three policy strings say the legacy content: studio
  three core groups, two dining chairs, bedside service when space permits,
  no clothing storage as filler; dining four chairs, sideboard and pendant as
  suggestions, rugs and mirrors opt-in; bedroom wardrobe, TV and desk opt-in,
  bedside lighting and storage planned, not required.
- `layout_rules.py`: `ROOM_LAYOUT_RULES`, `MEDIA_RUG_RULES`, the selection
  guidance and `DESIGN_RULES` use the restored constants; delete the
  bed-and-door line and the three studio zoning lines; "a TV requires a
  tv_stand or media_unit" returns to the living guidance.
- `shared_stages.py`, `variant_stages.py`: the intent and selection prompts
  drop the required-piece lines.

### 5. Tests, docs and benchmark

- `tests/test_rules.py`, `test_solver.py`, `test_stages.py`: fixtures at the
  legacy thresholds; delete the tests for the removed checks and required
  pieces; keep the bug-fix tests.
- `docs/pipeline/pipeline-details.md`, `adding-a-room-type.md`: values and
  required items.
- Delete `docs/pipeline/legacy-rule-differences.md`: once the rules match the
  legacy, the comparison describes nothing on `main`.
- Benchmark on one case per room type, one run each, fastest config.

## File changes

| File | Action | Change |
| --- | --- | --- |
| `pipeline/app/rules/layout_rules.py` | Edit | Constants, rule lines, selection guidance, `DESIGN_RULES` |
| `pipeline/app/rules/layout/bedroom.py` | Edit | 0.56 bands; two-sided and foot-route rules deleted |
| `pipeline/app/rules/layout/dining.py` | Edit | 0.56 pull-out and storage; rug 95%; light minimum only; no one-sided rule |
| `pipeline/app/rules/layout/constants.py` | Edit | Delete the #54 constants; P2 keys without `window_front` |
| `pipeline/app/rules/layout/comfort.py` | Edit | TV range, rug ratio |
| `pipeline/app/rules/layout/relations.py` | Edit | Wall TV center 1.10 for every viewer |
| `pipeline/app/rules/layout/analysis.py` | Edit | Desk check scope; no window-front |
| `pipeline/app/rules/layout/validation_functional.py` | Edit | Lamp reach to any seat |
| `pipeline/app/rules/layout/validation_geometry.py` | Edit | Delete `compute_window_front_violations` |
| `pipeline/app/rules/layout/metrics.py` | Edit | Counts without `window_front` |
| `pipeline/app/rules/layout/solver.py` | Edit | Bands and gaps from the constants |
| `pipeline/app/rules/categories.py` | Edit | `WALL_MOUNT_Z` 1.6 |
| `pipeline/app/rules/door_geometry.py` | Edit | Clear zone 0.60 m deep |
| `pipeline/app/rules/planner/_util.py` | Edit | `room_scale(area_sqm)` |
| `pipeline/app/rules/planner/fit_policy.py` | Edit | Legacy profiles and thresholds |
| `pipeline/app/rules/planner/fit_check.py`, `feasibility_digest.py`, `room_facts.py` | Edit | Scale and threshold callers; legacy optional lists; studio dining role |
| `pipeline/app/rules/room_policy.py` | Edit | Policy strings; chair count; studio dining helper deleted |
| `pipeline/app/rules/selection/validation.py` | Edit | Core-group requirements; legacy overcrowding; no size checks |
| `pipeline/app/rules/selection/constants.py` | Edit | Delete size constants; `REQUIRED_CATEGORIES` surface and lighting |
| `pipeline/app/rules/selection/preflight.py` | Edit | Light gap minimum only |
| `pipeline/app/shared_stages.py`, `variant_stages.py` | Edit | Prompt lines |
| `pipeline/tests/test_rules.py`, `test_solver.py`, `test_stages.py` | Edit | Fixtures and deleted tests |
| `docs/pipeline/pipeline-details.md`, `adding-a-room-type.md` | Edit | Values and required items |
| `docs/pipeline/legacy-rule-differences.md` | Delete | Comparison is obsolete once the rules match |

## Verification

From `pipeline/` with `conda activate livinit`:

```bash
python -m pytest tests/test_rules.py tests/test_solver.py tests/test_stages.py
```

Then one benchmark run per room type at the fastest config. Compare the
reviewer's scores with [report-2026-10-12.md](../pipeline/report-2026-10-12.md);
completeness may drop (fewer required pieces), nothing else should.

## Unresolved questions

None.
