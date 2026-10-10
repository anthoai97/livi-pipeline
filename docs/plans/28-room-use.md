# Furnish rooms to their size, and add decor with leftover budget

Issue: [#28](https://github.com/anthoai97/livi-pipeline/issues/28). Status: ready for implementation.

## Objective

- Living rooms and studios get the useful optional pieces their size allows.
- Every room gets a few decor pieces when budget is left.
- Minimal briefs stay minimal.

## Current behavior (verified)

- **Use-of-space scores are low.** On the reviewer's 1 to 5 scale, use of space is 2.8 to 3.9 in living rooms and studios. Bedroom and dining room score about 4.1 to 4.2. (Benchmark of 2026-10-10, 9 designs per case.)
- **Side tables are never candidates when the brief asks for a coffee table.**
  - The required `surface` role is coffee_table, desk and side_table.
  - `plan_slots` ([shared_stages.py:366](../../pipeline/app/shared_stages.py)) skips that required slot once a requested item covers it.
  - `room_requirements` removes required categories from the optional roles.
  - So side_table ends up in no slot. The reviewer asks for a sofa-side table in most living-room designs, and one design picked a nightstand instead.
- **The selection prompt says to buy as little as possible.** In living rooms and studios, `_selection_prompt` ([variant_stages.py:469-477](../../pipeline/app/variant_stages.py)) says "Do not add storage, media, extra seating, side tables, or decor by default" and "Choose the smallest coherent set". The bedroom instead gets completeness guidance ("judge bedroom completeness across the usable room ... an extra piece needs a purpose, not unused budget"), and it scores higher on use of space.
- **Decor exists but the prompt says not to add it.** Decor slots cover planter, sculpture, floor_mirror and wall_mirror (`DECOR_CATEGORIES`, [shared_stages.py:273](../../pipeline/app/shared_stages.py)), 3 to 4 candidates each. Purchasable stock in the local catalog:
  - surface sculptures: 28, median $55;
  - floor mirrors: 5;
  - planters: 13;
  - wall mirrors: 1;
  - wall art and prints: 4, mostly $2,500.
- **The budget line** ([variant_stages.py:558](../../pipeline/app/variant_stages.py)) says the budget is a cap, not a target. It stays.
- **Round 1 tried prompt text only** ("useful optional pieces up to the comfortable load"). Use of space did not move, because the side-table candidates were missing, and selection got about 5 s slower. That change was dropped.

## Scope

- In scope:
  - the side-table slot bug;
  - selection guidance for living rooms and studios;
  - decor guidance for every room.
- Non-goals:
  - new categories or catalog work (wall art is too thin to use);
  - solver changes;
  - new stages, fields or validation rules;
  - bedroom and dining room furnishing rules beyond decor.

## Decisions

- **Fix candidates before wording.** The model cannot pick what the pool lacks, so phase 1 comes first and is measured on its own.
- **Reuse the bedroom's completeness wording** for living rooms and studios, adapted to their pieces, instead of new rules. It is already in the code and the bedroom scores higher. This changes a ported legacy rule, which the user approved for this issue.
- **Decor comes from the existing decor slots, guided by the prompt:**
  - up to 2 pieces per design;
  - only when the total stays within the stated budget, not the 10% flex;
  - only pieces that suit the room: a tabletop sculpture on a surface, a plant, a floor mirror.

  The design-only plant rule is unchanged. No validation is added; a design without decor still passes.
- **The fill floor** (`ROOM_THRESHOLDS` sparse values, [fit_policy.py:30](../../pipeline/app/rules/planner/fit_policy.py)) changes only if phases 1 and 2 leave use of space flat.

## Phases

1. **Side-table candidates.** In `plan_slots`, when a requested item covers a required role, add that role's remaining categories (for example side_table and desk when a coffee table is requested) as an `optional` slot. This is about 3 lines. Measure.
2. **Room-sized selection.** In `_selection_prompt`, give living rooms and studios their own `optional_piece_guidance` and `selection_size_guidance`, as the bedroom and dining branches have:
   - each seat has a side surface and a light within reach;
   - the conversation group is complete for the room size;
   - a spacious room uses its long walls for storage or display;
   - every extra piece needs a purpose;
   - minimal and essentials-only briefs stay minimal.

   Reword the last sentence of the "Furnish the room" line so it does not tell the model to stop at the floor. Measure.
3. **Decor with leftover budget.** In every room, replace "or decor by default" with the decor rule above. Measure the decor count per design and its cost.
4. **Fill floor.** Only if use of space is still flat after phase 3: raise the `sparse` value for medium and spacious rooms. Measure.

Each phase is one commit. Phases stop early once the acceptance criteria are met.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | `plan_slots`: add an optional slot with a covered required role's remaining categories | Side tables (and desks) reach the candidates when a coffee table is requested |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_selection_prompt`: living room and studio guidance, the decor rule for every room, the reworded "Furnish the room" sentence | Selection fills the room to its size and adds decor with leftover budget |
| [pipeline/app/rules/planner/fit_policy.py](../../pipeline/app/rules/planner/fit_policy.py) | Edit, phase 4 only | Raise the medium and spacious `sparse` floors | Only if wording and candidates are not enough |
| [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md) | Edit | One line each on the slot rule and the decor rule | Keep the overview current |

No new files planned. Tests: one case in `tests/test_stages.py` for the phase 1 slot rule.

## Acceptance criteria and verification

- **Benchmark:** 6 cases × 3 runs (`scripts/benchmark.py --runs 3`), then `scripts/review.py` on those runs. Compare with the 2026-10-10 baseline.
- **Use of space** rises by about 0.3 or more in living rooms and studios. No other review column drops by more than about 0.3. Bedroom and dining room do not drop.
- **Side tables:** a living room with a requested coffee table has side-table candidates (phase 1 test). Most living-room designs include a side table or lamp table by a seat.
- **Decor:** designs with budget left carry 1 or 2 decor pieces; no design goes over budget because of decor.
- **Minimal briefs:** a minimal brief stays minimal. Check one run with "keep it minimal".
- **Speed and delivery:** median run time is about 25 s, and every run returns 3 of 3 designs.
- `python -m pytest` passes.

## Risks

- **More pieces mean more selection output and solver work,** so run time may rise. Watch the median at each phase, and keep the decor cap at 2.
- **More pieces mean more room for check failures.** Failing items are removed, not the design, so watch the removed-item count.
- **Thin decor stock** limits variety. Wall art stays out until the catalog has it.
- **Model latency varies a lot** (brief reading took 7 to 15 s on the same day). Compare post-interpret time as well as the total.

## Deferred

- Wall art and prints in the decor slots, once the catalog has enough of them at normal prices.

## Unresolved questions

None. The decor cap is 2 and decor must fit within the stated budget, not the 10% flex. Say if you want different numbers.
