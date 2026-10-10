# Let a model pick and fine-tune the final layout

Issue: [#14](https://github.com/anthoai97/livi-pipeline/issues/14).

## Problem

The code solver ends with up to 4 complete layouts (its beam) and keeps the one
with the lowest checker score. Layouts that tie on the score can still read very
differently as a room: a TV whose back faces the dining zone, a piece floating in
the middle. The checker cannot tell them apart; a designer looking at the plan can.

## Approach

1. `solve_layout` also returns the other complete layouts in
   `report["alternatives"]`, best first, each with `layout`, `score`
   (layout_issue_score), and `unplaceable`. Identical layouts are listed once.
2. `place`, after the solve and its swap/drop loop, measures the alternatives and
   keeps the ones whose layout_issue_score equals the best one and that have no
   more unplaceable items. A worse layout is never offered.
3. With 2 or more options, one model call (model key `arrange`) gets each option
   drawn top-down (`app.preview.render_plan`, titled "variant A", "variant B", ...)
   and a short text: room type and size, doors and windows, the request, each
   piece (instance key, category, size), and each option's poses. The model acts
   as an interior designer and returns JSON: `choice` (label), `reason` (one
   sentence), and `adjustments` (uid, x, y, rotation_z) for the chosen layout.
4. Code rejects an adjustment for an unknown item, a move over 0.5 m, or a turn
   that is not a whole number of quarter turns. The rest are applied with
   `_apply_poses` and measured with `_measure`; they are kept when the score is not
   worse (ties keep them), else reverted.
5. One option makes no call. A failed call or any error in this step keeps the
   solver's best layout and is noted; the variant never fails because of it.
6. `ctx.run.note` records the pick, reason, and kept, rejected, or reverted
   adjustments. `node_complete` data gets `layout_options` and `layout_pick`.

Model: `GeminiModel.generate` accepts a list of text parts and PNG bytes (sent as
inline image parts). The call is recorded under stage `place`. Without an
`arrange` entry in `LLM_STAGE_MODELS`, it uses `LLM_DESIGN_MODEL` at low thinking;
low keeps it at 2-6 s (benchmark: medium took 7-54 s, median 17 s). No contract or stage name
changes.

## Files

- `pipeline/app/rules/layout/solver.py`: `alternatives` in the report.
- `pipeline/app/variant_stages.py`: `_solve` returns the tied options; `place`
  runs `_arrange`; `Arrangement` schema and prompt.
- `pipeline/app/llm.py`, `pipeline/app/run.py`: image parts in `contents`.
- `pipeline/tests/test_stages.py`: pick used, large or worsening adjustment
  reverted, failed call keeps the best, one option makes no call.
- `pipeline/README.md`, `docs/pipeline/pipeline-overview.md` (4c).

## Unresolved questions

- Should a 180-degree turn count as small, or only one quarter turn?
