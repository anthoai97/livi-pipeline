# Fix the rule failures the legacy pipeline review found

Issue: [#65](https://github.com/anthoai97/livi-pipeline/issues/65) (open).
Source: the legacy pipeline team's review of `eafb4bb` (#61), 2026-10-14
(`tmp_review_md/README.md`, local, not committed). Status: ready for
implementation.

## Objective

Designs stop breaking the rules the review found: no table without chairs,
clear walkways from the door, no unrequested TV, every requested painting
visible, wall pieces on their wall. The remaining rule values match the
legacy pipeline. The benchmark reports a rule-based pass rate.

## Current behavior (verified on `main` 37224e6, 2026-10-15)

`main` has the same pipeline code as the reviewed `eafb4bb`; only `web/` auth
changed since. Paths below are under `pipeline/app/`.

- **Chairs removed from their table.** `finish` runs `_drop_items`
  (`variant_stages.py:1598`) while blocking findings remain. It never removes an
  anchor (`_ANCHOR_CATEGORIES`, `variant_stages.py:683`): bed, lounge seating,
  dining table. Dining chairs are not anchors, so they go first. The
  `incomplete_dining_group` finding names only the table, so the loop stops and
  the table ships alone. `_drop_failing` (`:947`) uses the same anchor set
  during selection.
- **Few chair layouts.** `_dining_templates` (`rules/layout/solver.py:504`)
  keeps `DINING_OPTIONS = 3` chair splits: the two most balanced and the
  smallest envelope. A table in a corner (chairs on two adjacent sides) is
  often cut. In the 2.8 x 3.2 m case no kept split fits, and the chairs are
  left unplaceable.
- **Table without chairs in a living room.** The `DINING GROUP` check
  (`rules/selection/validation.py:438-474`) and the analyzer's dining
  measurements (`rules/layout/analysis.py:132-134`) run only in dining rooms
  and studios.
- **No walkway check.** The 0.56 m route from a door to each zone is prompt
  text only (`rules/layout_rules.py:72,154-157`). P1 holds wall alignment and
  protected paths (`rules/layout/constants.py:127-130`). Protected paths exist
  only between opposing doors (`rules/protected_paths.py:35-38`), so a
  one-door studio has none. The legacy check is
  `livinit_pipeline/src/core/layout/circulation.py`
  (`compute_circulation_violations`).
- **Floor lamps away from walls.** The lamp's wall distance is a soft cost
  (`solver.py:801-807`). When no lamp slot passes, the seed accessory pass
  places it anywhere (`solver.py:887-888`). Rugs are not floor items, so a lamp
  on the rug costs nothing.
- **Unrequested TV.** `room_requirements` offers the media role
  (`tv_stand`, `tv`, `media_unit`) in every room type
  (`rules/selection/validation.py:196-200`). `_missing_tv`
  (`variant_stages.py:986`) adds a TV whenever a stand is selected.
- **Wall TV height.** `WALL_TV_CENTER_Z = 1.1` (`rules/layout/constants.py:13`,
  used in `rules/layout/relations.py:227`). A TV with no fitting stand is
  wall-mounted (`build_instances`, `rules/selection/validation.py:111`). The
  legacy keeps a TV on its stand and mounts it only when asked, at the generic
  1.60 m centre.
- **Second painting not visible.** Run `2e33b5b3` (dining 5.5 x 5.8 m, "two
  paintings on the walls") delivered `wall_art_1` and `wall_art_2` in every
  design. In designs 0 and 1, `wall_art_2` is "RÖDALM wall art frame"
  (`3e1e044b`), stored as 0.52 x 0.72 x 0.03 m: depth and height swapped. The 3D
  view scales models to these sizes (`web/src/components/RoomScene.tsx:416`),
  so it renders as a 3 cm slab sticking 0.72 m out of the wall. `keep_slot`
  (`shared_stages.py:429`) has no thickness check. Related gaps:
  - a stated count is not checked once the fit step is compact or capped
    (`rules/selection/validation.py:342-345,566-569`);
  - two wall pieces can overlap: the seed fallback ignores other wall art
    (`solver.py:955-962`) and `compute_overlaps` covers only floor items
    (`rules/layout/validation_geometry.py:40`).
- **Wall pieces off their wall.** `finish` re-normalizes after every move and
  removal. `_snap_wall_mounted_positions` and `_snap_wall_aligned_placements`
  (`rules/layout/normalization.py:104,155`) skip a piece when the nearest wall
  segment is shorter than it (`_flush_against_wall`, `:81-91`).
- **Density.** Selection counts 2 x width x depth of every non-rug piece
  (`rules/selection/validation.py:356-358`). The legacy counts floor pieces
  only. The ring formula in `fit_check.py` is used only for the up-front
  estimate, as in the legacy.
- **Checks #61 deleted** (commit `0ad2399`): the bedroom rug reaching 0.45 m past
  each bed side (or a runner), coffee table height 0.35-0.50 m and side table
  height 0.45-0.65 m. The sofa 0.40 m window zone was kept
  (`rules/layout/constants.py:68`).
- **Pair constraint on one product.** `_constraint_audit`
  (`variant_stages.py:746`) asks Jev one question per product. "Matching dining
  chairs" is judged on a single chair product and can fail it.
- **Render manifest.** `render_manifest` (`contracts.py:138`) drops `on_top_of`,
  so a TV on its stand looks unsupported to Studio and other checkers.
- **Benchmark.** `scripts/benchmark.py` prints `full_run_s` only. Blocking
  findings are not saved in the run record; only `non_blocking_findings` keys
  are (`graph.py:249`). `scripts/review.py` has judge scores only.

## Scope

In scope: the review's items 1 to 4, its doc comments that apply to the
pipeline, the two-painting case, and the benchmark changes.

Non-goals:
- Holding back a design that still has blocking findings. Every design is still
  delivered (user decision, 2026-10-15).
- Arc lamp footprints: we only have catalog width x depth.
- The 0.91 m bed-foot route to a door; it is not a legacy rule.
- Saving, `create_payload`, auth and billing for a `/pipeline` swap.
- Fixing the RÖDALM row in `pipeline_assets_v2`; that is a data handover.

## Decisions

1. **Every design is still delivered.** The fix is to never split a group,
   not to hold designs back.
2. **A table and its chairs are one group.** Where the table is an anchor
   (dining room, studio), its chairs are anchors too and are never removed. In
   other rooms, removing the table or a chair removes the whole dining group.
   This applies to `finish` and to `_drop_failing`.
3. **A dining table needs chairs in every room.** Selection and the analyzer's
   dining checks run whenever a dining table is selected.
4. **Port the legacy walkway check as a P1 finding.** We reuse its approach:
   the room shrunk by 0.28 m minus the furniture grown by 0.28 m, and every door
   and service area (bed sides and foot, seat fronts, chair pull-out, storage
   fronts) in one connected piece. Because `layout_issue_score` ranks P1 first,
   the solver acts on it with no new cost term. The finding names its blockers,
   so `finish` can move or drop them.
5. **Floor lamps stand against a wall.** Lamp slots at a sofa end move to
   touch the nearest wall within 0.30 m. A lamp that fits no slot is left out
   with a note instead of being placed anywhere.
6. **TV only when asked, outside living rooms** (legacy). Living rooms keep
   media as a suggestion, as the legacy does.
7. **A TV stands on its stand by default** (legacy). A requested TV makes a TV
   stand or media unit required, unless the brief asks for a wall-mounted TV.
   Intent records that as the functional hint `wall_mounted_tv`. A wall TV uses
   `WALL_MOUNT_Z` like other mounted pieces; `WALL_TV_CENTER_Z` goes. This
   follows the stakeholder's "legacy rules" direction.
8. **Thin pieces must be thin.** Retrieval drops wall art and mirrors whose
   depth is larger than their width or height. That is a swapped-size record,
   not a product we can show.
9. **Stated counts of wall pieces stay enforced** after a fit decision. Wall
   pieces use no floor, so a compact step has no reason to cut them. Dining
   chairs already work this way.
10. **Wall pieces on the same wall must not overlap.** Their overlap is a P0
    finding, and the seed fallback avoids existing wall pieces.
11. **Snap to the nearest wall the piece fits on**, instead of skipping the
    piece when the nearest segment is too short.
12. **Bring back the three deleted checks as selection warnings** (not errors,
    no retrieval drop): bed rug width, coffee table height, side table height.
13. **Each Jev product question gives its selected units.** "Matching dining
    chairs" on one chair product scored 0.58-0.66 (fail below 0.5); with
    "selected units: 6" it scored 0.87-0.90. A room-wide question over all
    products was tried and dropped: it failed palette constraints such as
    "oak, walnut, cream and muted green" turn after turn.
14. **Benchmark pass = zero blocking findings.** A missing chair is already
    blocking (`incomplete_dining_group` is critical P2), so this covers
    "anchor group complete".

## Phases

### 1. Small fixes

- `contracts.py`: `render_manifest` layout carries `on_top_of` from the
  instance.
- `rules/layout/constants.py`, `rules/layout/relations.py`: delete
  `WALL_TV_CENTER_Z`; `_wall_mount_z` uses `WALL_MOUNT_Z` for a TV, still above
  the furniture under it.
- `rules/selection/validation.py`: density counts floor pieces only
  (`placement_mode_for_asset(asset) == "floor"`, rugs excluded).
- `docs/plans/61-legacy-room-rules.md:44`: chairs default to 4, or 2 in a
  studio.

### 2. Table and chairs stay together

- `variant_stages.py`: in `_drop_items` and `_drop_failing`, removal goes
  through one rule: dining chairs share the table's anchor status, and in
  rooms where the table is not an anchor, removing either removes every dining
  table and chair. Keep `_ANCHOR_CATEGORIES` as the room anchor source.
- `rules/selection/validation.py`: the `DINING GROUP` check runs whenever a
  dining table is selected. Seat range: the stated count, else 2 to 4 outside
  dining rooms.
- `rules/layout/analysis.py`: dining measurements run whenever the layout has a
  dining table.
- `rules/layout/solver.py`: `_dining_templates` also keeps the smallest corner
  split (chairs on two adjacent sides) and the smallest single-side split.
  `DINING_OPTIONS` rises to 5.

### 3. Clear walkways

- `rules/layout/validation_geometry.py`: `compute_circulation_violations`, a
  port of the legacy function with our `door_inward_normal`, floor-asset and
  functional-group helpers. Width `PATH_SECONDARY_M` (0.56).
- `rules/layout/analysis.py`, `rules/layout/constants.py`,
  `rules/layout/metrics.py`: P1 key `circulation_violations` and its count.
- `rules/layout/solver.py`: lamp slots against the wall (decision 5); the
  accessory fallback no longer places a floor lamp anywhere.
- `rules/layout_rules.py`: the review's `DESIGN_RULES` line for the walkway
  matches the check.
- Check the solver run time on the 13 benchmark cases. If the check adds more
  than 2 s per design, run it only on the shortlist (`SHORTLIST`, `BEAM`) and
  the final ranking.

### 4. TV rules

- `rules/selection/validation.py`: in `room_requirements`, the media role is
  optional in bedrooms and studios only when the brief requests a TV or media
  (`requested_items`, `tv_focused`). A requested TV without
  `wall_mounted_tv` makes `media_support` required.
- `shared_stages.py`, `rules/planner/intent_packet.py`: the intent prompt and
  packet accept `wall_mounted_tv` in `functional_hints`.
- `rules/selection/validation.py` (`build_instances`): a TV is wall-mounted
  only with `wall_mounted_tv`; otherwise it sits on its support.
- `rules/room_policy.py`, `rules/layout_rules.py`: texts say "TV on its stand;
  wall-mounted only when asked".

### 5. Paintings and wall pieces

- `shared_stages.py` (`keep_slot`): drop a wall art, painting or mirror product
  whose depth is larger than its width or height.
- `rules/selection/validation.py`: after a fit decision, explicit counts are
  still enforced for wall and ceiling pieces.
- `rules/layout/validation_geometry.py` (`compute_overlaps`): wall pieces on
  the same wall that overlap in width and height are an overlap.
- `rules/layout/solver.py` (`_accessorize`): the fallback spot avoids placed
  wall pieces.
- `rules/layout/normalization.py`: `_flush_against_wall` callers try the next
  nearest cardinal wall segment long enough for the piece before skipping.
  Confirm with the `living_room_l` and `studio` benchmark cases first; the
  review did not trace which step moved the piece.

### 6. Restored warnings and the set constraint

- `rules/selection/constants.py`, `rules/selection/validation.py`: the bed rug
  width, coffee table height and side table height checks from `c7c64bc`,
  returned as `warnings` only.
- `variant_stages.py` (`_checked`, `_product_text`): each product in the Jev
  audit carries its selected units.

### 7. Benchmark and docs

- `graph.py`, `run.py`: the run record keeps `blocking_findings` keys per
  variant.
- `scripts/benchmark.py`: columns pass/fail (zero blocking findings), time
  without retrieve (`all_ready_s` minus the `retrieve` stage), then
  `full_run_s`.
- `scripts/review.py`: the report shows the pass rate first, then the scores.
- `scripts/benchmark_requests.json`: add `dining_room_compact_two`
  (2.8 x 3.2 m), `studio_tight` (3.66 x 4.27 m), `living_room_tv_console`
  (4.6 x 4.9 m) and `dining_room_paintings` (5.5 x 5.8 m, "two paintings on the
  walls"), with the geometry and briefs from the legacy cases
  (`run_room_e2e_cases.py`: `dining-compact-two`, `studio-tight-fit`,
  `typical-tv-console-rect-coffee`, `dining-six-sideboard-paintings`). The
  first two go in the `quick` set.
- `docs/pipeline/pipeline-overview.md`, `pipeline-details.md`: TV, chair group,
  walkway, density and restored warnings. `benchmark-process.md`: case count
  and the pass/fail column.

### 8. Follow-up after the first benchmark (2026-10-16)

The first benchmark and judge review found these; each change was checked
against the same four rooms for pass rate, failed turns and time.

- `shared_stages.py` (`retrieve`): a requested slot that its limits leave
  empty is searched again without them, like a required one. One run of the
  two-painting room lost both paintings to an empty slot.
- `shared_stages.py` (`keep_slot`): a TV stand or media unit deeper than wide
  is dropped as a swapped record (3 in the catalog, such as "Coronado Walnut
  Wood Credenza", 0.48 x 1.96 m).
- `rules/layout/solver.py` (`_accessorize`): a floor accessory the seed set in
  a service band moves to a spot clear of the bands and door zones, and stays
  when there is none. It is never reported unplaceable, so no turn fails.
- `rules/layout/solver.py` (`_preference`): a media group or a piece taller
  than the sill in a window's 0.40 m zone costs 1 more. A soft cost only.
- `rules/selection/preflight.py`, `rules/selection/validation.py`: a TV that
  fits no selected stand hangs on the wall with a `TV SUPPORT` warning, not
  an error, so it does not fail a turn.
- The room-wide set question (old decision 13) is dropped; see decision 13.

## File changes

Paths are relative to the repository root.

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_drop_items` and `_drop_failing` keep the dining group whole; Jev product text gives selected units | Table shipped without chairs; pair check failed one product |
| [pipeline/app/rules/layout/validation_geometry.py](../../pipeline/app/rules/layout/validation_geometry.py) | Edit | Add `compute_circulation_violations`; wall-piece overlap in `compute_overlaps` | Walkway failures; hidden overlapping paintings |
| [pipeline/app/rules/layout/solver.py](../../pipeline/app/rules/layout/solver.py) | Edit | Corner and single-side chair splits; lamp slots against the wall; fallback avoids wall pieces | Chairs found no spot; lamps in the walkway |
| [pipeline/app/rules/layout/analysis.py](../../pipeline/app/rules/layout/analysis.py) | Edit | P1 circulation; dining checks whenever a table is present | Solver and finish see the new findings |
| [pipeline/app/rules/layout/constants.py](../../pipeline/app/rules/layout/constants.py) | Edit | P1 key `circulation_violations`; delete `WALL_TV_CENTER_Z` | Blocking set; legacy TV rule |
| [pipeline/app/rules/layout/metrics.py](../../pipeline/app/rules/layout/metrics.py) | Edit | Count the new P1 key | `layout_issue_score` ranks it |
| [pipeline/app/rules/layout/relations.py](../../pipeline/app/rules/layout/relations.py) | Edit | Wall TV at `WALL_MOUNT_Z` | Legacy TV rule |
| [pipeline/app/rules/layout/normalization.py](../../pipeline/app/rules/layout/normalization.py) | Edit | Snap to the nearest wall long enough for the piece | Wall pieces off their wall |
| [pipeline/app/rules/selection/validation.py](../../pipeline/app/rules/selection/validation.py) | Edit | Dining group in every room; media role and TV support; TV mount by hint; floor-only density; wall counts kept after fit; restored warnings | Selection rules from the review |
| [pipeline/app/rules/selection/constants.py](../../pipeline/app/rules/selection/constants.py) | Edit | Restore bed rug and table height constants | Restored warnings |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | `keep_slot` thickness check; intent prompt `wall_mounted_tv` and set constraints | Invisible painting; TV mount; pair check |
| [pipeline/app/rules/planner/intent_packet.py](../../pipeline/app/rules/planner/intent_packet.py) | Edit | Accept `wall_mounted_tv` | TV mount hint survives cleaning |
| [pipeline/app/rules/room_policy.py](../../pipeline/app/rules/room_policy.py) | Edit | TV text | Policy says the rule |
| [pipeline/app/rules/layout_rules.py](../../pipeline/app/rules/layout_rules.py) | Edit | TV and walkway lines | Prompts match the checks |
| [pipeline/app/contracts.py](../../pipeline/app/contracts.py) | Edit | `on_top_of` in the manifest layout | Support visible to Studio and checkers |
| [pipeline/app/graph.py](../../pipeline/app/graph.py) | Edit | Pass `blocking_findings` keys to the run record | Benchmark pass/fail |
| [pipeline/app/run.py](../../pipeline/app/run.py) | Edit | `record_variant` stores them | Benchmark pass/fail |
| [pipeline/scripts/benchmark.py](../../pipeline/scripts/benchmark.py) | Edit | Pass/fail and time-without-retrieve columns | Report what decides shipping |
| [pipeline/scripts/review.py](../../pipeline/scripts/review.py) | Edit | Pass rate first | Same |
| [pipeline/scripts/benchmark_requests.json](../../pipeline/scripts/benchmark_requests.json) | Edit | Four cases | Hard rooms and the painting case |
| [docs/plans/61-legacy-room-rules.md](61-legacy-room-rules.md) | Edit | Studio chair default | Doc said 4 |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md), [pipeline-details.md](../pipeline/pipeline-details.md), [benchmark-process.md](../pipeline/benchmark-process.md) | Edit | Rules and benchmark | Docs match the code |

No new files planned.

## Acceptance criteria

- `dining_room_compact_two`: every design delivers the table with its 2 chairs,
  each chair with its 0.56 m pull-out.
- No delivered design has a dining table with zero chairs, in any room type.
- `studio_tight` and `studio`: no `circulation_violations` in at least 2 of 3
  designs; no unrequested TV.
- A floor lamp in a delivered design is within 0.30 m of a wall.
- `dining_room_paintings`: every design has 2 wall art pieces, each with depth
  below its width and height, not overlapping.
- The benchmark table shows pass/fail and time without retrieve.

## Verification

From `pipeline/` with `conda activate livinit`, no LLM calls in unit tests:

```bash
python -m pytest tests/test_rules.py tests/test_solver.py tests/test_stages.py
```

New unit tests, only where behavior changes:
- `_drop_items`: a layout with an `incomplete_dining_group` and a blocked
  chair keeps both chairs.
- `compute_circulation_violations`: a chair across the only door route is
  flagged; the same chair moved aside is not.
- `keep_slot`: the RÖDALM-shaped record (0.52 x 0.72 x 0.03 m) is dropped.
- `validate_selection`: a requested count of 2 paintings under a compact fit
  step still errors with 1 selected.

Then the benchmark on `dining_room_compact_two`, `studio_tight`,
`dining_room_paintings` and `living_room_l`, 1 run each, fastest config. Run
the review's grader (`qc_regrade.py`) if the legacy team shares it.

## Dependencies and risks

- The walkway check runs in every solver ranking. Measure its time (phase 3).
- Stricter dining and walkway rules can leave more `unplaceable` pieces in
  small rooms. Decision 1 means those designs still ship, with notes.
- The RÖDALM row stays wrong in `pipeline_assets_v2` until the data owner fixes
  it; the retrieval check hides it meanwhile.

## Deferred

- Holding back designs with blocking findings.
- Correcting swapped sizes in `pipeline_assets_v2` (data handover).
- Arc lamp footprints.

## Unresolved questions

None.
