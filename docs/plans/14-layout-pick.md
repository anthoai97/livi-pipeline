# Give every design a final review

Issue: [#14](https://github.com/anthoai97/livi-pipeline/issues/14).

## Problem

The code solver ends with up to 4 complete layouts (its beam) and keeps the one
with the lowest checker score. Layouts that tie on the score can still read very
differently as a room: a TV whose back faces the dining zone, a piece floating in
the middle. The checker cannot tell them apart; a designer looking at the plan can.
The repair, correct, and reselection steps changed nothing in 144 benchmark
designs.

## Approach

The variant flow becomes `select -> place -> finish -> END`.

1. `solve_layout` returns the other complete layouts in `report["alternatives"]`.
   `place` measures them and returns, as `layout_options`, the result and the
   ones with the same layout_issue_score and no more unplaceable items. It makes
   no model call.
2. `finish` makes one model call (stage model `finish`, low thinking by
   default) on every design, even with one option. It gets the request, the
   requested items and style hints, `build_rules_block` for the room type with
   definitions, `coordinate_system_block`, the room boundary, doors
   (`format_door_for_prompt`), windows, protected paths, each piece (instance
   key, category, W x D x H, mount type), and each option as a top-down plan
   labeled A to D with its poses as text. The model returns JSON: `choice`,
   `review` (what is wrong with it as a room), and `adjustments` (uid, x, y,
   rotation_z) that fix those problems.
3. Code rejects adjustments for unknown items, with non-finite values, or with
   turns that are not whole quarter turns. Moves of any length are allowed. The
   rest are applied (`_apply_poses`, `_measure`) and kept when the score is not
   worse; otherwise the picked layout stays as it is.
4. A failed call, an unknown label, or any other error in the review keeps the
   solver's best layout; the variant never fails because of it.
5. `final_layout_check` runs on the result. When it fails, `_drop_items`
   removes the failing items. Then finish builds the delivery.
6. `ctx.run.note` records the pick, review, and kept, reverted, or rejected
   adjustments. `node_complete` data for `finish` (node `render_scene`) adds
   `layout_pick`, `review`, and `dropped` to `valid` and `errors`; the web
   ignores the extra keys.

Removed: the `repair`, `correct`, `validate`, `drop`, and reselection steps,
placement feedback to select, `MAX_CORRECTION_PROPOSALS`, `MAX_RESELECTIONS`,
`correct_escalate`, and the `model_key` argument of `generate`. The web drops
its `layout_fix` node.

## Files

- `pipeline/app/graph.py`, `pipeline/app/contracts.py`: new topology and node names.
- `pipeline/app/variant_stages.py`: `place` returns the options; `finish`.
- `pipeline/app/llm.py`, `pipeline/app/run.py`: no `model_key`.
- `pipeline/tests/`: pick, kept and reverted adjustments, single option, failed
  call, removal after a failed check.
- `web/src/lib`: no `layout_fix`.
- `pipeline/README.md`, `docs/pipeline/pipeline-overview.md` (4c, 4d).

## Unresolved questions

- Should the prompt also list the checker's remaining findings for the picked layout?
- Low thinking kept the earlier pick call at 2-6 s (medium took 7-54 s). The
  review prompt is larger and now runs on every design; check its latency on
  the next benchmark.
