# Make the solver the only fit check

Issue: [#45](https://github.com/anthoai97/livi-pipeline/issues/45). Status: implemented.

## Objective

A selection passes the select step only when the code solver places every item. When the solver leaves items out, the model gets another turn and is told what did not fit and why. The rough size estimates stop rejecting selections, and the place step stops repairing them. Items that still do not fit after the last turn are left out, and the design is delivered without them. This keeps fit logic in one place as rooms get bigger and hold more objects.

## Current behavior (verified)

**Incident:** run `f9109392…`, "A cosy Scandinavian dining room for four…", $10,000, 3.4 x 3.8 m, Design 2.

1. **select:** the model picked the Lane Dining Table (1.52 m square, $5,170), 4 chairs, a rug and a plant. The dining-cluster estimate said they fit, so the solver was not asked. The selection passed on turn 1.
2. **place:** the solver left out the table and all 4 chairs. `_swap` tried the next smaller table (Jasper, 1.40 m). All 5 were still left out, so the score did not change, and the swap was reverted and the loop stopped. Smaller tables, such as the Otago 1.37 m that Design 3 placed in the same room, were never tried. `_drop_chair` refused, because the brief asks for 4 seats.
3. **finish:** `_drop_items` first removes every item missing from the layout, with no protection for the table, bed or sofa. The design was delivered "ready" with a rug and a plant ($1,499).

The run record does not keep the chosen chairs, so the exact failure was not reproduced. In a local check with catalog chair sizes, the solver placed the Lane table. The record itself shows the sequence above.

**Two fit checks that disagree:**
- `select` → `_checked` ([variant_stages.py:889](../../pipeline/app/variant_stages.py)) runs `validate_selection` with the fit estimates. The estimates compare each piece or cluster with the empty room's clear area:
  - the over-crowded footprint;
  - anchor seating, complete bed and rug against the room;
  - the dining and desk clusters;
  - a tabletop item against its supports.
- The solver runs (`_solver_fit`, line 1041) only when an estimate fails. When the solver places everything, it overrules the estimate. A selection the estimate wrongly passes reaches the solver only in place, where it can no longer go back to the model.
- `place` → `_solve` (line 1187) repairs a failed solve itself: up to 2 swaps to the next smaller product (`_swap`) and up to 2 chair removals (`_drop_chair`). A repair is kept only if the score improves.

**Rate today:** in the local run records, 1 of 763 designs lost its table, bed or sofa, and 4 of 710 solves left items out. Studios take the solver longest: 10 items, 1.0 s median, 4.9 s max.

## Scope

- In scope: the fit check in select, the fit estimates in the validator, place's repairs, and the docs.
- Non-goals:
  - changing the solver's search or rules;
  - changing finish (review, final check, `_drop_items`);
  - adding new run notes;
  - adding new model calls or stages;
  - changing room rules or the catalog.

## Decisions

- **The solver decides fit in select.** `_checked` validates without fit estimates. For a selection that passes, it runs the solver once.
  - If the solver leaves items out, the selection fails with one `LAYOUT FIT` error. The error names each left-out item with its title and size and asks for products from the same slots that fit together. The model then gets another turn. The estimate's cluster sizes are not quoted: in the incident room the estimate said the dining group fits (it allows chairs on two adjacent sides), which contradicts the solver. A two-per-side seating hint was tried and did not change the replay results, so it was removed.
  - Blocking findings on placed items do not fail select. Finish's review and final check handle them, as today.
- **The fit step follows the solver.** When the solver leaves out a floor item, `fit_failed` is set, so the next turn steps to compact and then to capped counts (`_next_fit_step`). When it leaves out only a tabletop item, the selection fails but the fit step stays, as `support_fit_failed` does today. The room-level `fit_warning`, which starts a design at compact, stays.
- **A `LAYOUT FIT` failure skips last-turn removal.** `_drop_failing` cannot see the solver result, so it would remove an unrelated product. The selection goes to place as it is.
- **Place data drops `swaps` and `drops`** in the stage data, the web schema and status text, and the README.
- **The solver runs once per checked selection.** That covers the model's selection and each budget repair or fill trial. It does not run inside `_validated`, which `_drop_failing` calls many times.
- **The estimate's size errors are deleted, not switched off.** The `fit_estimates` and `fit_checks` flags go away, along with the errors they gate. The estimate's numbers (cluster envelopes, sizes) and its warnings stay as metrics. Errors that are not size estimates stay. These include "no eligible support surface" and "insufficient edge length for N chairs".
- **Place only solves.** `_swap`, `_drop_chair`, `MAX_SWAPS`, `MAX_DROPS` and the repair loop in `_solve` are deleted. The solver is deterministic, so place gets the same layout select checked. Running it again costs 0.2 to 1 s per design.
- **Last turn and finish are unchanged.** If the last turn still fails the solver check, the selection goes to place as today. Finish's `_drop_items` removes what is missing, the design is delivered without it, and no note is added.

## Phases

1. **Solver check in select.**
   - Change `_solver_fit` in place: it solves a validated selection and returns the `LAYOUT FIT` error and whether a floor item was left out. It returns nothing when every item is placed.
   - `_checked`: validate, then call `_solver_fit` on a passing selection. Merge its error into the report (`valid`, `errors`, `fit_failed`). Delete the "fit estimate overruled" path and its note.
   - `_validated`, `validate_selection` and `_layout_preflight_for_assets`: drop `fit_estimates` and `fit_checks`, the over-crowded error, and the size errors they gate. Drop `support_fit_failed` if nothing reads it after this change.
   - `_next_fit_step`: read the solver-based `fit_failed`. Update its docstring.
2. **Place only solves.** Delete `_swap`, `_drop_chair`, `MAX_SWAPS`, `MAX_DROPS` and the repair loop in `_solve`. Update the `place` and `_solve` docstrings.
3. **Docs and measurement.** Update the docs. Run the benchmark and the incident request.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_checked` always runs `_solver_fit` on a passing selection. `_solver_fit` returns the `LAYOUT FIT` error. `_validated` loses `fit_estimates`. `_next_fit_step` reads the solver result. `select` docstring. Delete `_swap`, `_drop_chair`, `MAX_SWAPS`, `MAX_DROPS` and the repair loop in `_solve` | One fit check that can send a selection back to the model |
| [pipeline/app/rules/selection/validation.py](../../pipeline/app/rules/selection/validation.py) | Edit | Drop `fit_estimates`, the over-crowded error and the estimate-based `fit_failed`/`support_fit_failed`. Keep the footprint metrics | The estimates no longer decide fit |
| [pipeline/app/rules/selection/preflight.py](../../pipeline/app/rules/selection/preflight.py) | Edit | Drop `fit_checks` and the room and support size errors it gates (anchor seating, bed, rug, tabletop support, desk and dining clusters). Keep the metrics, warnings and the non-size errors | Same |
| [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md) | Edit | Replace the "fit estimate overruled" section (lines 244-250) with the solver check. Remove the swap and chair-removal paragraph in place (lines 349-358). Add one line on the fit step | Keep the details current |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | Place row (line 38): drop "Swaps or drops a piece that cannot fit." Select: the solver checks fit | Keep the overview current |

No new files planned.

Tests in `tests/test_stages.py`:
- Delete the swap and chair-removal tests.
- Replace the two "fit estimate overruled" tests with one: a selection the solver cannot fully place fails with `LAYOUT FIT`, steps to compact, and passes with a smaller table on the next turn.
- Adjust the oversized-selection and lamp-on-side-table tests to the solver-based fit step.

`tests/test_rules_parity.py` should still pass: its recorded legacy errors contain none of the deleted size errors.

## Acceptance criteria and verification

- **Incident:** replay the run `f9109392…` request 3 times (9 designs). Every design has a dining table and 4 chairs.
- **Benchmark:** `python scripts/benchmark.py --runs 3` (18 runs, 54 designs), then `python scripts/review.py`, compared with [report-2026-10-11](../pipeline/report-2026-10-11.md):
  - 54 of 54 designs delivered;
  - no design delivered without its table, bed or sofa;
  - median run time about 28.8 s, all runs under 60 s;
  - 4b select p90 not much above 20.6 s;
  - reviewer layout and overall scores not lower by more than about 0.3.
- **Turns:** report the share of designs that need more than 1 select turn, before and after.
- `python -m pytest` passes.

## Risks

- **More select turns.** Some selections the estimate passed, but the solver leaves items out, will now take another model turn (about 5 s). This is the intended trade, but watch the select p90.
- **Selections the estimate used to reject.** These now pass whenever the solver places them, including crowded ones. Blocking findings on placed items still go to finish's review and final check, as they do today for selections the estimate passed. Watch the reviewer's circulation comments and the final-check removals.
- **Studio solve time.** It can reach about 5 s with 13 items, and select may run it up to 3 times per turn (selection plus 2 budget trials). Watch the select time for studios.
- **Feedback quality.** In tight dining rooms the model cannot tell from the text which table and chair pairs the solver will place; designs use all 4 turns and finish removes chairs. Follow-up: [#46](https://github.com/anthoai97/livi-pipeline/issues/46).

## Unresolved questions

None.
