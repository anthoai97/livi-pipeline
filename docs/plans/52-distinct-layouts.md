# Give each design a different layout

Issue: [#52](https://github.com/anthoai97/livi-pipeline/issues/52) (open).

## Objective

Each of a run's three designs takes a different one of the solver's equally
good layouts, so the designs differ in where the bed, sofa or dining table
stands, not only in products. A design never gets a worse layout to be
different.

## Current behavior (verified on `main`, 2026-10-11)

- Each design solves on its own with the same room, intent and rules
  (`solve_layout`, [solver.py](../../pipeline/app/rules/layout/solver.py)).
  The solver's beam keeps up to 4 complete layouts, the best of each zone plan
  first (`_pick` with `_Partial.key`: the wall each sleeping, sitting, media or
  work group backs onto, and the room quarter of the dining table). The report
  lists the others as `alternatives`, best first, but not their zone plans.
- `place` (`_solve`, [variant_stages.py](../../pipeline/app/variant_stages.py))
  keeps the result and the alternatives that tie its `layout_issue_score` as
  `layout_options`, the result first. Every design builds the same list.
- `finish` (`_arrange`) shows the options to the model as top-down images
  labeled A to D and asks it to choose one, review it and adjust it. In the
  last 80 runs the model chose A in 184 of 233 reviews (79%).
- Result, last 80 runs, by zone plan: all three designs shared one plan in 8 of
  16 bedroom runs, 6 of 17 dining rooms, 6 of 25 living rooms and 0 of 21
  studios. 40 of 79 runs gave three different plans.
- The run record stores each ready design's layout
  (`_variant_summary`, [graph.py](../../pipeline/app/graph.py)) but not its
  zone plan, so counting distinct plans needs the poses re-read.

## Scope

- Deal the solver's tied layouts to the designs by zone plan, in `place`.
- The final review reviews and adjusts the design's own layout only.
- Record each design's zone plan.

Non-goals: changing the solver's candidates, beam width or scoring (only the
beam's diversity key gains a second level); different products per design
(already dealt in rank, #5); non-rectangular room shapes (a later issue);
per-design variant directions for placement.

## Decisions

1. **Deal in `place`, not in the solver.** The solver stays design-agnostic and
   returns every complete layout with its zone plan. `_solve` knows
   `variant_index` and takes the design's share. No coordination between the
   parallel designs is needed: every design computes the same ordered list of
   plans from its own solve, so index k is deterministic.
2. **Deal by distinct zone plan, best first; then by the lesser pieces.**
   Design 0 takes the best layout. Design 1 takes the best layout of the next
   distinct zone plan, design 2 the next. Only layouts that tie the best
   `layout_issue_score` with no more unplaceable items count, so a design
   never gets a worse layout. When the ties hold fewer zone plans than designs
   (a bedroom whose bed fits one wall), the next designs take layouts that
   differ in where the wall pieces stand: the wardrobe, dresser, bookcase or
   desk on another wall. That is what a designer does when the bed cannot
   move. A design repeats a layout only when nothing can move, and the note
   says so. The beam keeps that variety on purpose: its diversity key gets a
   second level, the walls of the wall singles, used only when the zone plans
   do not already fill the beam.
3. **The review no longer chooses, for any design.** If design 0 kept the
   choice among all tied layouts, it could choose the plan dealt to design 1
   and the duplicates return. So `Arrangement.choice`, the A to D labels and
   the multi-image prompt go. The review gets one image, reviews it and
   adjusts it as today. This removes the pick that #14 added; the data shows
   it chose A 79% of the time, and the adjustments are what change layouts.
   The finish call also sends one image instead of up to four.
4. **The record keeps the plan.** `_variant_summary` adds `plan`, the zone
   plan and the wall-single walls as `[[kind, value], ...]`, so the benchmark
   can count distinct layouts per run without re-reading poses.
5. **The benchmark counts them.** `scripts/benchmark.py` prints a `plans`
   column (distinct plans among a run's ready designs) and the summary line
   adds how many runs reached three.

## Phases

### 1. Solver reports plans and keeps wall variety

- `_Partial` gets `detail`: the back wall of each wall single placed so far,
  in placement order. `_expand` extends it for `group.kind == "wall"` as it
  extends `key` for the zone kinds.
- `_pick` takes two diversity levels: the best item of each `key` first, then
  the best of each `(key, detail)`, then the rest in order. At the zone steps
  `detail` is empty, so nothing changes there. At the wall-single steps the
  zone plans fill the beam when there are four of them, as today; with fewer,
  the free beam slots keep layouts whose wall pieces stand on other walls
  instead of the same layout slid along one wall.
- `solve_layout` returns `plan` for the result and for each alternative:
  `key` then `detail` as a list of `[kind, value]` pairs (`value` is a wall
  name, `"divider"`, or the dining quarter as `[right, back]` booleans).

### 2. Place deals the ties

`_solve` builds the tied options as today (result first, then alternatives with
the same score and no more unplaceable items), then orders them: the first
layout of each distinct zone plan, then the first of each distinct full plan
(zone plan plus wall walls), then the rest. It takes
`options[min(variant_index, len(options) - 1)]`, measures it, and returns it as
`layout` and `layout_plan` with its findings. `layout_options` is removed from
`VariantState`. The note becomes `solver: ... ; plan <k+1> of <n> distinct
(<m> tied)`, with `, repeats plan <j>` when the index exceeds the distinct
plans. `ctx.data` replaces `layout_options` with `plans` (distinct tied plans)
and `plan` (the index taken, 1-based).

### 3. Finish reviews one layout

- `finish` reads `layout` from the state; `_arrange(state, ctx)` reviews it.
- `Arrangement` loses `choice`. `_arrangement_prompt(state, image)` describes
  one layout: "The layout solver arranged this room as shown. Review it."
  The POSES and MEASURED FACTS blocks list that layout only. `_plan_png` titles
  the plan with the design's index, as the delivered preview does.
- `allowed`, the all-at-once then one-at-a-time adjustment loop, the notes for
  kept, reverted and rejected moves, and the failed-review fallback stay.
  The fallback note becomes "kept the solver's layout". `ctx.data` drops
  `layout_pick`.

### 4. Record, benchmark and docs

- `_variant_summary` adds `plan` from `place`'s state (`layout_plan`, the
  pairs from phase 1).
- `scripts/benchmark.py`: `summarize` adds `plans`, the number of distinct
  `plan` values among the ready variants (null when none carries one); the
  summary line adds `distinct_plans=<runs with 3>/<runs>`.
- README delivery table, overview step 4d and the flow diagram, details 4c
  and 4d: place deals the tied layouts by plan; finish reviews the design's
  layout.

### 5. Tests

- `test_solver.py`:
  - `test_studio_layouts_on_offer_differ_in_their_zone_plans` reads `plan`
    from the report instead of deriving it from poses.
  - A bedroom whose bed fits one wall (a 2.6 m by 3.6 m room with the door
    on a short wall), with a dresser and a wardrobe: the tied layouts hold at
    least two distinct full plans, differing in a storage wall, and every
    layout has no blocking findings.
- `test_stages.py`, with `solve_as` extended to take each layout's `plan`:
  - Three tied layouts with three zone plans: the three designs deliver the
    planter at three different positions; `place` data has `plans == 3`,
    `plan == [1, 2, 3]`; each finish call carries one image.
  - Three tied layouts where two share a zone plan and differ in a wall
    piece: design 1 takes the other zone plan, design 2 the wall variant.
  - Two tied layouts: design 2 repeats and its note says `repeats plan 2`.
  - One layout: every design delivers it; the prompt has no variant labels.
  - Failed review keeps the design's dealt layout (not the solver's best).
  - `arrangement()` fixtures drop `choice`.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/rules/layout/solver.py](../../pipeline/app/rules/layout/solver.py) | Edit | `_Partial.detail`; `_expand` extends it for wall singles; `_pick` with two diversity levels; `solve_layout` adds `plan` to the report and to each alternative; docstring. | The beam keeps layouts that differ in a wall piece when the zone plans cannot differ, and place deals by plan without re-deriving it from poses. |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_solve` orders the ties by distinct plan and takes the design's index; `place` returns `layout` and `layout_plan`, no `layout_options`; `finish` and `_arrange` review one layout; `Arrangement` drops `choice`; `_arrangement_prompt` and `_plan_png` take one layout; `VariantState` keys; notes and `ctx.data`. | The dealing, and the review without a pick. |
| [pipeline/app/graph.py](../../pipeline/app/graph.py) | Edit | `_variant_summary` adds `plan`. | The record can count distinct plans. |
| [pipeline/tests/test_stages.py](../../pipeline/tests/test_stages.py) | Edit | Replace the pick tests with the dealing tests in phase 5; `arrangement()` without `choice`. | Verify the new behavior. |
| [pipeline/tests/test_solver.py](../../pipeline/tests/test_solver.py) | Edit | Zone-plan test reads `report["plan"]`; one-bed-wall bedroom test. | Covers phase 1. |
| [pipeline/scripts/benchmark.py](../../pipeline/scripts/benchmark.py) | Edit | `summarize` adds `plans`; the summary line adds `distinct_plans`. | Counts distinct layouts per run. |
| [pipeline/README.md](../../pipeline/README.md) | Edit | `place` data: `plans`, `plan`; `finish` data: no `layout_pick`. | Delivery reference matches the events. |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | Diagram node 4d and the 4c, 4d rows. | Describes `main`. |
| [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md) | Edit | Overview line d, end of 4c (tied layouts dealt by plan), 4d steps 1 and 2. | Same. |

## Verification

From `pipeline/` with `conda activate livinit`:

```bash
python -m pytest tests/test_solver.py tests/test_stages.py tests/test_delivery.py
```

Then the benchmark, 3 runs per case, reading its new `plans` column: the
target is three distinct plans in every run, bedrooms included. Compare the
reviewer's layout scores and the distinct score with the
[2026-10-11 report](../pipeline/report-2026-10-11.md); layout quality must not
drop by more than chance (about 0.5 at 9 designs per brief).

## Unresolved questions

None. The three questions raised on 2026-10-11 are settled in decisions 2, 3
and 5.
