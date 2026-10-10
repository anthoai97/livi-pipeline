# Phase 5 report

Phase 5 ([#6](https://github.com/anthoai97/livi-pipeline/issues/6)) delivers
three variants in parallel to the browser. This report covers the delivery work,
the layout work that followed, and where quality stands. Dates: October 9 to 10,
2026. Durations are seconds; scores are 1 to 5.
Plans: [browser delivery](../plans/6-browser-delivery.md) and
[final review](../plans/14-layout-pick.md).

## What shipped

| PR | Change |
| --- | --- |
| [#15](https://github.com/anthoai97/livi-pipeline/pull/15) | Variants stream to the browser with a top-down preview and 3D models. A failed check removes the failing items instead of failing a design. Studio TV divider, clear sofa-to-TV sightline, floor lamp by lounge seats, dining off the room center. Generate screen: consistent progress rows, finished state that asks before opening the comparison view. Brief: 5.5 x 5.8 m default room and example prompts with sizes. |
| [#16](https://github.com/anthoai97/livi-pipeline/pull/16) | One final review step (`finish`) replaces repair, correction, and reselection. A Gemini call sees top-down images of the solver's tied layouts, picks one, reviews it, and adjusts it; code keeps only adjustments that do not make the checks worse. |
| [#17](https://github.com/anthoai97/livi-pipeline/pull/17) | Drops the zone spread cost, which did not help. Adds [adding a room type](adding-a-room-type.md). |
| [#18](https://github.com/anthoai97/livi-pipeline/pull/18) | Comfort and taste rules move to plain text (`DESIGN_RULES`) that the final review applies. |

The variant flow is now `select -> place -> finish`:

- **select:** up to 4 model turns; the last turn drops failing products.
- **place:** the code solver, with swaps and chair drops; it returns its tied best layouts.
- **finish:** one review call, then the final check, which removes failing items if needed.

A design fails only when a stage raises, for example a model call that used all
its attempts.

## Setup

- Requests: the six cases in `pipeline/scripts/benchmark_requests.json`.
  - Four cases are from phase 4.
  - `living_room_scandi` and `studio_relaxed` are recorded from web runs that looked wrong.
- Model `gemini-3.8-flash` at low thinking for every stage, including `finish`, with the local catalog.
- Commands:
  - `python scripts/benchmark.py --runs 3`
  - `python scripts/review.py --runs <ids>` (reviewer `gpt-6.1-sol`, medium effort)

## Speed and delivery

| Configuration | Runs | Median | Max | Designs delivered |
| --- | ---: | ---: | ---: | ---: |
| Start, before layout changes (5 cases) | 15 | 18.9 | 30.9 | 45 of 45 |
| Studio solver fixes | 18 | 19.8 | 33.1 | 54 of 54 |
| Layout pick at medium thinking | 18 | 48.3 | 75.9 | 54 of 54 |
| Final review step | 18 | 23.8 | 34.5 | 54 of 54 |
| Without the spread cost | 18 | 25.1 | 56.8 | 54 of 54 |
| Text design rules (final) | 18 | 29.9 | 39.2 | 54 of 54 |

- **Final review call:** median 4.3 s (3.0 to 8.5 s). At medium thinking it took 7 to 54 s, so it runs at low.
- **Removed steps:** repair, correction, and reselection changed nothing in 144 designs before they were removed.
- **Slow brief reading:** a few runs took 65 to 180 s while Gemini read the brief. That latency is on Gemini's side, not in our code.

## Layout quality

Reviewer layout scores, 9 designs per case, first review of the phase -> final:

| Case | Grouping | Focal point | Use of space | Circulation | Overall layout |
| --- | --- | --- | --- | --- | --- |
| Bedroom | 5.00 -> 5.00 | 5.00 -> 5.00 | 4.00 -> 4.22 | 4.56 -> 4.56 | 4.00 -> 4.00 |
| Dining room | 5.00 -> 5.00 | 5.00 -> 5.00 | 4.11 -> 4.00 | 4.67 -> 4.78 | 4.11 -> 4.00 |
| Living room | 3.67 -> 3.78 | 4.56 -> 4.11 | 3.33 -> 3.22 | 3.67 -> 3.56 | 3.33 -> 3.22 |
| Living room Scandi | 4.33 -> 4.11 | 4.56 -> 4.44 | 3.67 -> 3.33 | 3.22 -> 3.22 | 3.67 -> 3.33 |
| Studio | 2.44 -> 3.78 | 2.44 -> 3.67 | 2.67 -> 2.89 | 3.89 -> 4.44 | 3.00 -> 3.00 |
| Studio relaxed (new case) | n/a -> 4.00 | n/a -> 4.00 | n/a -> 3.33 | n/a -> 4.00 | n/a -> 3.22 |

- **Studio:** the TV divider and the sightline fixed the main problem, a TV 4 to 6 m from the sofa with dining in the view.
  - 9 of 19 studio designs had a blocked view before; 0 of 17 after.
  - Most TVs now stand 2.9 to 3.6 m from the sofa.
- **Bedroom and dining room:** unchanged and already good.
- **Living rooms:** flat. The text rules cut the reviewer's top complaints, but the scores moved little.

  | Complaint | Before | After |
  | --- | ---: | ---: |
  | Tall piece in front of a window | 7 | 4 |
  | Coffee table too close | 10 | 4 |
  | Lamp not at the seat | 8 | 6 |

- **Reading these numbers:** a 0.1 to 0.3 change is within noise at 9 designs per case.

## What did not work

- **Zone spread cost.** It pushed furniture to the walls, and the reviewer then
  flagged an empty center. Scores did not change, so #17 removed it.
- **Layout pick at medium thinking.** Too slow for no measurable gain.
- **Final review adjustments.** About half are reverted by the checks: 14 kept,
  15 reverted in the final run. The model places pieces without measured gaps.

## Known issues

- **Use of space** (2.9 to 3.3 in studios and living rooms) is limited by
  selection, not placement: rooms get too little furniture for their size.
- In about 1 in 9 studio designs, Design 2 ends with no TV. Selection alternates
  between a stand without a TV and a missing TV.
- Decor plants fail material checks and slow selection
  ([#13](https://github.com/anthoai97/livi-pipeline/issues/13)).
- **Adding a room type** still touches about 20 places, and several of them fall
  back to living-room rules ([adding a room type](adding-a-room-type.md)).

## Next

1. Size and count products to the room in selection (use of space).
2. Give the final review measured facts (gaps, blockers, remaining findings), so
   more of its adjustments survive the checks.
3. Room profiles: one data and text file per room type that selection, prompts,
   solver, and web read, so a new room needs one file instead of 20 edits.
4. Fix the missing TV in studio selection, and #13.
