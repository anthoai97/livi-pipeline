# Cut the selection calls

Issue: [#57](https://github.com/anthoai97/livi-pipeline/issues/57) (open).

## Objective

A design passes product selection in one model call most of the time. Problems
with one product (style, size, price) are fixed by swapping that product in
code, with Jev questions instead of model calls. Duplicate calls go only to
calls that are slow, and a retry costs less than a first turn.

## Current behavior (verified on `main` at `5710141`, 2026-10-12)

Code, in [variant_stages.py](../../pipeline/app/variant_stages.py):

- `select` (`:830`) makes one model call, fixes the TV (`_missing_tv`), then
  `_checked` (`:927`): the Jev audit (`_constraint_audit`, `:765`, style yes
  under `STYLE_VIOLATION_BELOW` 0.3 rejects), the validator (`_validated`),
  and one solve (`_solver_fit`). Over the allowance, or a passing selection
  under 80%, `_budget_repair` (`:1006`) swaps the most expensive products for
  the next cheaper (or pricier) product of the same category in the slot's
  ranked order and `_checked` runs again; a failing trial is noted as
  rejected and dropped. On turn 4, `_drop_failing` (`:939`) removes products.
  Any failing turn ends with a note, and `_after_select`
  ([graph.py:156](../../pipeline/app/graph.py)) runs another turn up to
  `MAX_SELECTION_TURNS` 4.
- The retry prompt (`_selection_prompt`, end) appends the previous uid list
  and `json.dumps(previous)`, the whole validation report.
- `_size_findings` ([validation.py:306](../../pipeline/app/rules/selection/validation.py))
  flags a coffee table outside 0.35 to 0.50 m high, a side table outside 0.45
  to 0.65 m, a bedroom rug neither a runner nor bed width plus the extra, a
  dining light too wide; it is an error when the pool holds a passing
  product. Candidates reach the model unfiltered by these rules.
- `generate` ([llm.py:137](../../pipeline/app/llm.py)) sends a duplicate of
  any call not answered after `MODEL_HEDGE_AFTER_S` 8 s ([graph.py:44](../../pipeline/app/graph.py)),
  records both, and the first answer wins.

Measured on the last 18 local runs (54 designs):

| | Value |
| --- | --- |
| Selection calls | 174 (3.2 per design); 44 designs needed a second turn, 28 a fourth |
| Failed turns | 128: Jev style 80 (77 style only), over budget 42, SIZE 30, missing required 17, layout fit 15 |
| Jev check rejections | 80; attributes failed in 3 |
| Budget repair | 3 kept, 30 rejected (24 by the Jev audit, 12 by SIZE, 7 by layout fit) |
| Hedged duplicates | interpret 12 of 18, select 9, finish 6; interpret median 7.8 s |
| Select input tokens | first turn 15,654 median, retries 18,072; 4% cached |

## Before and after

Per design unless stated; "now" is the 18-run measurement above, "after" is
the target the benchmark checks at the end of phase 3.

| Metric | Now | After |
| --- | ---: | ---: |
| Selection model calls | 3.2 | at most 1.5 |
| Designs needing a second turn | 44 of 54 (81%) | under 30% |
| Designs reaching turn 4 | 28 of 54 (52%) | under 5%, then the cap is 3 |
| Failed turns | 2.4 | under 0.5 |
| Turns failed on a Jev style rejection | 1.5 | about 0: repaired in code |
| Budget repairs kept | 3 of 33 | most: the swap is pre-checked |
| Hedged duplicates (all stages) | 10% of calls | under 3% |
| Retry prompt input tokens | 18,000 | about 14,000 (errors only) |
| Input tokens served from cache | 4% | over 40% |
| Selection cost per benchmark (306 calls) | $4.12 | at most $2.00 |
| Review scores | report baseline | within chance (0.5) |

## Scope

The selection turn loop, the budget repair, the retry prompt, the hedge
setting, two retrieval filters, and the benchmark columns. Non-goals: the
Jev thresholds (the model picks from Jev-ranked lists, so a rejection means
it chose a low-ranked product; the fix is the next candidate), the catalog
CSV, the rules texts (#54 part 3), fewer designs or candidates per slot.

## Decisions

1. **Product-level failures are repaired in code, not by a model retry.**
   A style rejection, a `SIZE` error with a passing alternative, and a budget
   miss name a product and a slot; the slot's ranked list holds the next
   choice. `_swap_repair` replaces `_budget_repair` and handles all three:
   style and size first, then the budget walk. The model is asked again only
   for selection-level errors: missing required items, counts, layout fit,
   unknown uids, under-furnished.
2. **A replacement must pass before it is used.** For each failing product
   the repair takes up to 3 candidates in slot order (same category, not
   selected, purchasable, reuse limit kept, size rule passed) and asks Jev the
   style question for all of them in one request, with the anchor. It takes
   the first with yes at or above 0.3. The budget walk uses the same
   candidate test, so a cheaper product that Jev would reject is skipped.
   One Jev call per repair round, about 0.3 s, instead of a 6 s model call.
3. **At most two repair rounds per turn**, each followed by `_checked`. A
   round that does not pass is kept when it leaves fewer errors (the next
   round or the model continues from it), so a style fix is not thrown away
   because the budget is still off.
4. **Category-only size rules filter at retrieval.** Coffee table and side
   table heights depend only on the product, so `keep_slot` drops them in
   living rooms. Rules that depend on a pairing (rug and bed, light and
   table) stay in `_size_findings` and the repair.
5. **Hedge at 12 s.** Interpret's median is 7.8 s and select's 5.8 s, so 8 s
   doubles normal calls. 12 s is past select's p75 and interpret's p90; the
   37 s tail still gets a duplicate. One setting, default changed.
6. **A retry carries the errors and the previous uids**, not the report.
   The prompt is ordered so the part shared by the three designs (room,
   budget, intent, room facts, digest, selection guidance) comes first, the
   catalog second, and everything per design and per turn (direction, fit
   step guidance, TV facts, constraints, strategy, previous errors) last.
   Gemini's implicit cache then covers the shared prefix across designs and
   the catalog across a design's turns.
7. **Three turns once the repairs land.** With repairs, a fourth turn
   should be rare; the benchmark decides (`MAX_SELECTION_TURNS` 3 when fewer
   than 5% of designs reach turn 4).

## Phases

### 1. Hedge and retry prompt

- `MODEL_HEDGE_AFTER_S` 12 in `graph.py`, `.env.example`, the overview's
  Models table.
- `_selection_prompt`: the order in decision 6; the retry block becomes the
  previous uid list, `ERRORS:` one per line, and the over-budget hint.

### 2. Swap repair

- `_constraint_audit` returns the style answers by uid (it already builds
  them) so the caller knows which products failed and why.
- `_swap_repair(state, ctx, selected, validation, audit, reuse_rate)`:
  failing products from `audit["style_mismatches"]` and the `SIZE:` errors;
  candidates per decision 2 using `_size_findings`' predicates (exposed as
  `size_passes(product, selected, pool, room_type)`); one `ctx.ask("check",
  ...)` with the style question for every candidate; then the budget walk of
  today's `_budget_repair` with the same candidate test. Returns the new
  selection and the swaps as text, or None.
- `select`: after `_checked`, up to two rounds of `_swap_repair` plus
  `_checked`; keep a round that passes or lowers the error count; note
  `swap repair: <old> -> <new> (style|size|budget)` and `swap repair
  rejected`. A still-failing selection goes to the model retry as today;
  `_drop_failing` on the last turn is unchanged.
- The run record notes stay comparable: `selection turn <n> failed` only when
  the turn ends failing.

### 3. Retrieval filters, turn cap, benchmark

- `keep_slot`: coffee table and side table height limits in living rooms.
- `scripts/benchmark.py`: `select_calls` and `failed_turns` per run; the
  summary line adds their medians per design.
- Run the benchmark; set `MAX_SELECTION_TURNS` 3 when decision 7 holds.

### 4. Benchmark cases

`scripts/benchmark_requests.json` holds 13 cases, 39 runs at 3 runs each.
One case per room type and size class is enough, with the recorded incident
cases filling their class. Size classes are #54's per-room cutoffs.

| Case | Room, size, class | Change | Why |
| --- | --- | --- | --- |
| living_room_small | living, 9 m², compact | keep | |
| living_room_scandi | living, 21 m², medium | keep | Also the attribute-check stress case. |
| living_room | living, 30 m², spacious | remove | Same class as living_room_large with a plainer brief. |
| living_room_large | living, 56 m², spacious | keep | Sectional, two armchairs, reading corner. |
| bedroom_small | bedroom, 9 m², compact | keep | |
| bedroom | bedroom, 18 m² | resize to 3.6 x 4.0 m (14.4 m², medium) | 18 m² is spacious under the bedroom cutoffs; the set needs a medium bedroom. |
| bedroom_large | bedroom, 33 m², spacious | keep | King, bench, reading chair. |
| dining_room_tight | dining, 13 m², medium | keep | The #45 fit case; dining for four. |
| dining_room | dining, 21 m², spacious | remove | Four seats again, same class as dining_room_large. |
| dining_room_large | dining, 36 m², spacious | keep | Eight seats, sideboard. |
| studio_small | studio, 18 m², compact | keep | Tests the optional dining group under 25 m². |
| studio | studio, 38 m², medium | keep | |
| studio_relaxed | studio, 32 m², medium | remove | Same class and brief as studio. |

Ten cases, 30 runs for a report. `benchmark.py` gets `--set quick|full`
(default `full`): `quick` is the medium case of each room (living_room_scandi,
bedroom, dining_room_tight, studio) at one run each, for everyday PR checks.
Each case carries `set: ["quick", "full"]` in the JSON. The docs' example
brief (the cream sofa living room) stays in the docs; it no longer needs a
benchmark case. The review script and `pipeline-details` references to 39
runs change to 30.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| [pipeline/app/variant_stages.py](../../pipeline/app/variant_stages.py) | Edit | `_swap_repair` replaces `_budget_repair`; `select` runs repair rounds and retries only on selection-level errors; `_constraint_audit` returns style answers; `_selection_prompt` reordered with the slim retry block. | Decisions 1, 2, 3, 6. |
| [pipeline/app/graph.py](../../pipeline/app/graph.py) | Edit | `MODEL_HEDGE_AFTER_S` 12; `MAX_SELECTION_TURNS` 3 after the benchmark. | Decisions 5, 7. |
| [pipeline/app/rules/selection/validation.py](../../pipeline/app/rules/selection/validation.py) | Edit | `_size_findings` built on `size_passes`, exported for the repair. | The repair tests candidates with the same rule. |
| [pipeline/app/shared_stages.py](../../pipeline/app/shared_stages.py) | Edit | `keep_slot` height filters for living-room coffee and side tables. | Decision 4. |
| [pipeline/scripts/benchmark.py](../../pipeline/scripts/benchmark.py) | Edit | `select_calls`, `failed_turns` columns and summary medians; `--set quick\|full`. | Measures the target; a cheap PR check. |
| [pipeline/scripts/benchmark_requests.json](../../pipeline/scripts/benchmark_requests.json) | Edit | Remove living_room, dining_room, studio_relaxed; bedroom at 3.6 x 4.0 m; `set` per case. | One case per room and size class. |
| [pipeline/scripts/review.py](../../pipeline/scripts/review.py) | Edit | Usage example at 30 runs. | Matches the set. |
| [pipeline/tests/test_stages.py](../../pipeline/tests/test_stages.py), [pipeline/tests/test_llm.py](../../pipeline/tests/test_llm.py) | Edit | Tests below. | Verify. |
| [.env.example](../../.env.example), [docs/pipeline/pipeline-overview.md](../pipeline/pipeline-overview.md), [docs/pipeline/pipeline-details.md](../pipeline/pipeline-details.md) | Edit | Hedge default; 4b: swap repair in place of the budget repair paragraph; the Limits table. | Docs describe `main`. |

## Verification

From `pipeline/` with `conda activate livinit`:

1. Jev rejects one product's style: the design makes one model call, the
   note says `swap repair: <old> -> <new> (style)`, and the delivered product
   is the next candidate in that slot.
2. A `SIZE` error with a passing alternative: swapped, no retry; with no
   alternative: a warning, no retry.
3. Over budget where the first cheaper candidate fails style (fake Jev
   answers): the repair takes the second and is kept.
4. A missing required item still retries the model; the retry prompt holds
   the previous uids and the errors, and no `metrics` or `status` text.
5. A 9 s call gets no duplicate; a 13 s call does.
6. `keep_slot` drops a 0.30 m-high coffee table in a living room.

```bash
python -m pytest tests/test_stages.py tests/test_llm.py tests/test_rules_parity.py
```

Then the benchmark, 3 runs per case, against the latest report: selection
calls per design at most 1.5 median, failed turns per design under 0.5,
duplicates under 3% of calls, selection cost per run down by half, every
design delivered, review scores within chance (0.5). Then decide the turn cap.

## Changes during implementation

- **Size tolerance.** Catalog sizes come from 3D bounding boxes, so a product
  passes a size rule within `SIZE_TOLERANCE_M` (5 cm) of its limit, in the
  retrieval filter and in the selection size check. Prompts state the limits.
- **Style repair checks the whole slot.** Three candidates were often all
  rejected, and the model then alternated between rejected products. The
  repair asks Jev about every eligible candidate in the slot in one request.
  When none matches the anchor, the product stays with a `STYLE KEPT` warning
  for the rest of the design's selection. Retries name the products Jev
  rejected.
- **Brief-wide attributes are checked for the room.** A phrase with no
  category ("boucle textures", "Scandinavian") passes when at least one
  selected piece meets it; it fails only when none does. Category-scoped
  phrases stay per product.
- **Turn cap stays at 4.** In the final benchmark 25 of 90 designs (28%)
  reached turn 4, mostly small rooms the solver cannot fit; the cap needs
  under 5%.

Result, 10 cases x 3 runs against `main` run with the same set: selection
calls per design 3.00 -> 2.23, failed turns 2.22 -> 1.34, cost per run
$0.145 -> $0.114, median run 43.7 -> 37.2 s, review scores unchanged
(overall 3.13 -> 3.10). The 1.5 target is not reached: the remaining retries
are small-room layout fit (left as is) and budget.

## Unresolved questions

None.
