# Pipeline report, 2026-10-13: room shapes, phase 1 (#53)

The six shaped rooms on the #53 phase 1 branch, scored once each. Phase 1
reads the real walls, openings, area and paths; placement still works in the
bounding box until phase 2 (pockets) and phase 3 (inner walls). Plan:
[53-room-shapes](../plans/53-room-shapes.md).

## Summary

- **Delivered:** every shaped room completes under 60 s with three designs,
  every piece inside the outline, no blocking findings.
- **Scored below the rectangles:** overall 3.11 against 3.25 for the
  rectangle cases in the [2026-10-12 report](report-2026-10-12.md); use of
  space 3.11 against 3.40. The angled room scores best (4.00); the U-room
  worst (2.67).
- **Why:** the reviewer's top issues are the ones phase 2 is for. The
  sitting group sits in the bounding box's bottom band, so an L's short leg,
  a T's stem and both legs of a U stay empty, the alcove bed is not in the
  alcove, and the Z-studio's zones land in the wrong halves.
- **One run per case.** With three designs each, a room's score moves by
  about half a point between runs; treat the per-room numbers as indicative.

## Setup

- **Requests:** the six `Room shapes` examples of the web form, added to
  `pipeline/scripts/benchmark_requests.json` as `living_room_l`,
  `bedroom_alcove`, `living_room_t`, `dining_room_angled`, `studio_z` and
  `living_room_u` (set `full`), one run each, 18 designs.
- **Branch:** `issue-53-furnish-any-room-shape` at f17cde8.
- **Model:** `gemini-3.8-flash` at low thinking; Jev rank and check on; local
  catalog. Reviewer `gpt-6.1-sol`, medium effort, scores 1 to 5. The Z-studio
  review was rerun after one proxy overload.

## Results

### Delivery, speed and cost

| Room | Walls | Floor | Run | Designs | Non-blocking findings | Distinct plans | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| L living room | 6 | 30.0 m² | 31.7 s | 3 | 2 | 3 | $0.09 |
| Alcove bedroom | 8 | 18.0 m² | 38.0 s | 3 | 1 | 3 | $0.13 |
| T living room | 8 | 26.1 m² | 22.4 s | 3 | 3 | 1 | $0.07 |
| Angled dining room | 5 | 23.5 m² | 37.0 s | 3 | 3 | 3 | $0.13 |
| Z studio | 8 | 29.9 m² | 42.7 s | 3 | 3 | 3 | $0.14 |
| U living room | 8 | 30.0 m² | 24.2 s | 3 | 0 | 3 | $0.08 |

The angled room's three findings are all the dining light alignment; the
T-room's are the media group and a floor lamp's reach.

### Reviewer scores

Means over the three designs. Selection averages prompt match, style, budget
use, completeness and scale; layout averages circulation, grouping, use of
space and focal point.

| Room | Selection | Layout | Use of space | Layout quality | Overall |
| --- | ---: | ---: | ---: | ---: | ---: |
| L living room | 3.80 | 3.58 | 3.00 | 3.00 | 3.00 |
| Alcove bedroom | 3.73 | 3.58 | 2.67 | 3.00 | 3.00 |
| T living room | 4.13 | 3.42 | 3.33 | 3.00 | 3.00 |
| Angled dining room | 4.73 | 4.83 | 4.67 | 4.33 | 4.00 |
| Z studio | 3.87 | 3.08 | 3.00 | 3.00 | 3.00 |
| U living room | 3.80 | 3.00 | 2.00 | 2.67 | 2.67 |
| **Shaped rooms, 18 designs** | **4.01** | **3.58** | **3.11** | **3.17** | **3.11** |
| Rectangles, 2026-10-12, 117 designs | | | 3.40 | 3.26 | 3.25 |

### What the reviewer saw

- **L living room:** the reading lamp stands beside the sofa instead of in
  the short leg, which is "largely unused".
- **Alcove bedroom:** the bed stands outside the alcove. The selected bed is
  1.63 to 1.66 m wide and the alcove 1.50 m, so it could not fit there; the
  selection prompt does not yet say the alcove's size.
- **T living room:** an armchair and the planter reach into the stem the
  brief asked to keep clear; chairs sit 0.15 to 0.35 m from the coffee table.
- **Angled dining room:** the planter crowds a chair; the chandelier is small
  for a 2.6 m table. The diagonal wall caused no finding.
- **Z studio:** the bed and sofa took the opposite halves from the brief, and
  the dining table interrupts the sofa-to-TV line.
- **U living room:** the sofa group sits in the bottom-left corner with both
  legs empty; the requested rug is missing in one design.

## What this means for the next phases

Phase 2 (pockets, one zone per pocket) addresses every layout issue above
except the chair-to-table gaps. The selection prompt should state the pockets
and their sizes so a bed is chosen to fit the alcove. Phase 3 lets wall pieces
use the inner walls, which the T and U rooms leave empty now.
