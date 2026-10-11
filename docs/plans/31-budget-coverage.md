# Spend 80 to 90% of the budget

Issue: [#31](https://github.com/anthoai97/livi-pipeline/issues/31). Status: ready for implementation.

## Objective

Each design spends at least 80% of the budget, aiming for about 90%. It never goes over the budget plus the 10% flex. Extra spend buys better versions of the room's pieces first, and then useful pieces the room is missing. Run time stays about the same.

## Current behavior (verified)

**Spend per case** in the 2026-10-10 full benchmark on `main` (18 runs, 54 designs). 40 of 54 designs are under 80%.

| Case | Budget | Median spend | Under 80% |
| --- | ---: | ---: | ---: |
| Living room | $7,000 | 83% | 3 of 9 |
| Bedroom | $6,000 | 81% | 3 of 9 |
| Dining room ("keep the floor bare") | $7,000 | 36% | 9 of 9 |
| Studio | $8,000 | 67% | 9 of 9 |
| Scandi living room | $10,000 | 51% | 9 of 9 |
| Relaxed studio | $10,000 | 63% | 7 of 9 |

**The selection prompt tells the model not to spend.** In `_selection_prompt` ([variant_stages.py](../../pipeline/app/variant_stages.py)):
- Line 560: "Budget is a spending cap, not a target ... There is no minimum spend."
- Line 571 (STRATEGY): "Unspent budget is acceptable; apart from the decor rule above, never add an item solely because budget remains."
- Line 575: "pick the best style match at a moderate price; reach for premium versions only after every furnishing role the room needs is covered."

**There is a repair for overspending only.** `_budget_repair` ([variant_stages.py:952](../../pipeline/app/variant_stages.py)) runs when the selection is over the allowance. It walks each design's ranked candidates, swaps the most expensive non-anchor products for cheaper ones of the same category, and keeps the result only if it validates again. Nothing pushes spend up.

**The candidate pools already hold pricier options.** Each slot searches products up to the budget allowance (`plan_slots`, `max_price`), and each design keeps its 4 to 6 best-ranked candidates per slot.

## Scope

- In scope: the selection prompt's budget rules, and `_budget_repair` working in both directions.
- Non-goals:
  - new stages or model calls;
  - catalog or retrieval changes;
  - web changes (the screen already shows "under budget");
  - room rules.

## Decisions

- **One budget rule replaces the old one.** The design targets 85 to 95% of the budget, with a floor of 80% and the allowance as the ceiling. Money goes first to better versions of the anchor and main pieces (sofa, bed, dining table, rug), then to useful missing pieces, and never to filler. The "no minimum spend", "unspent budget is acceptable" and "moderate price" lines go away; "never inflate prices" stays.
- **Code fills the gap when the model stops short.** `_budget_repair` is changed in place to handle both directions:
  - Over the allowance, it swaps for cheaper products, as today.
  - Under 80% of the budget, it swaps for pricier products of the same category from the design's own ranked candidates. It takes the higher-ranked style match first and includes anchors. It moves toward 90% and never goes over the budget.

  This is budget arithmetic over the existing slots, not product rules, and it needs no model call. The swapped selection is kept only if it validates again (fit checks, solver fit), exactly like today's repair.
- **Upgrades do not add pieces.** That keeps minimal briefs, like the dining room's "keep the floor bare", minimal while still spending on better pieces.
- **No new selection error for low spend.** An error would cost extra model turns (time). A design that stays under 80% after the repair is delivered with a run note that says why.

## Phases

1. **Prompt.** Replace the three lines above with the budget rule. Measure spend.
2. **Two-way repair.** Change `_budget_repair` and its call in `select` (line 829):
   - run it when the selection is over the allowance (today), or valid but under 80% of the budget;
   - in the second case, upgrade instead of downgrade;
   - note `budget fill: $6,306 -> $8,850 (old -> new, ...)`, or `budget fill rejected` with the errors, as the repair note does today.

   Measure spend, run time and the reviewer's budget and style scores.

Phase 2 is skipped if phase 1 alone brings almost every design to 80% or more.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_selection_prompt`: the new budget rule replaces lines 560, 571 and 575. `_budget_repair`: swap up when under 80%, toward 90%, never over the budget. `select`: call it in that case and note the result | The model is told not to spend, and nothing raises spend when it stops short |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | One line on the spend target and the two-way repair | Keep the overview current |

No new files planned. Tests: one case in `tests/test_stages.py` checks that a valid selection at about 60% is upgraded to 80% or more, with the same categories and validation still passing.

## Acceptance criteria and verification

- **Spend:** at least 80% for nearly every design, with a median of about 90%, and no design over budget plus 10%. Spend comes straight from the run records, so 1 run per case (6 runs, 18 designs) is enough to measure it.
- **Explained misses:** any design under 80% has a run note that says why, for example "no fitting upgrade".
- **Quality holds:** after the final phase, 3 runs per case and `review.py`. The reviewer's budget, style and overall layout scores do not drop by more than about 0.3 against the 2026-10-10 report.
- **Speed and delivery:** median time after brief reading stays about 20 s, and every run returns 3 of 3 designs.
- `python -m pytest` passes.

## Risks

- **Pricier pieces can be bigger.** A swap may fail the fit checks. The repair keeps a swap only if validation passes; otherwise it tries the next candidate.
- **Thin pools.** Some slots have few pricier options, so a design may stay under 80%. The run note shows this.
- **Style drift.** Upgrades take the design's ranked order first, so they stay within its style direction. Watch the reviewer's style score.
- **The reuse limit** between designs applies to swaps, as in today's repair.

## Unresolved questions

None. The targets are a floor of 80%, an aim of about 90%, upgrades only (no added pieces from the code step), and the anchor included. Say if any should differ.
