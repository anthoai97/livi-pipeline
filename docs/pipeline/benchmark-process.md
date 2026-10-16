# Benchmark process with the LLM reviewer

To measure a pipeline change, we replay a fixed set of requests, score every
delivered design with an LLM reviewer (the judge), and compare the numbers
against `main`. The [2026-10-12 report](report-2026-10-12.md) is a finished
example.

## Overview

```
fixed request set        replay              run records
20 cases          --->   3 runs per case --->  timings, cost, products, layout
                            |                        |
                            v                        v
                   speed and delivery          judge
                   numbers                     plan image + rubric
                            |                        |
                            |                        v
                            |                  scores and reasons
                            |                  per design and per run
                            v                        |
                        report  <--------------------+
                        change vs main
```

A round is one replay plus one review of its records, on one code state. To
judge a change, compare its round against a `main` round made the same way.

## The request set

Twenty fixed cases: one medium room per room type (bedroom, studio, Scandi
living room, tight dining room), small and large variants, shaped rooms (L, T,
U, Z, alcove, angled), and four cases taken from the legacy pipeline's tests:
a compact dining room for two, a tight studio, a living room with a TV on its
console, and a dining room with two paintings. Each case has
a brief, a budget, the room geometry, doors and windows. The cases do not
change between rounds, so differences come from the code, not the input.

The quick subset is the four medium cases plus `dining_room_compact_two` and
`studio_tight`, once each, for everyday checks. The full set, three runs per
case, is the standard for a report. It gives 60 runs and 180 designs per round.

## Step 1: replay

The replay sends each case to the pipeline three times, because a single run
varies with the model. We compare the median of the three. The generation
model, pipeline switches and service setup stay fixed between rounds unless
one of them is what the change tests.

Each run yields a record and these measures:

| Measure | Meaning |
| --- | --- |
| Pass | Designs out of 3 that are ready with zero blocking findings (P0, P1, critical P2) at the final layout. The pass rate over all designs leads the summary. |
| Time without retrieve | Time to the last design minus the retrieve stage (`no_retrieve_s`), so a slow catalog search does not hide pipeline time. |
| Run time | Request to stream end (`full_run_s`). Also time to the first design. |
| Designs delivered | Out of 3. A run is valid only when all 3 are ready. |
| Model calls, selection calls, failed selection turns | How much the run had to retry. |
| Cost | Model spend of the run. |
| Distinct layouts | Whether the 3 designs use different solver plans. |
| Validator findings | Non-blocking layout findings over the designs. |

Run times are only comparable between rounds that were run the same way
(same number of services, same concurrency).

## Step 2: judge

The same judge model, at the same effort, reviews every ready design in the
round. The judge is a different model from the one that generates the designs,
so it does not grade its own work.

### What the judge sees

For each design, one call with:

- The user brief, the budget, the allowance (budget + 10%) and the total cost.
- Room type, size, wall height, each door and window with its wall and position.
- The room's core pieces, the groups selection requires. Lamps, nightstands,
  clothes storage, side tables, sideboards, overhead lights and accent seats
  are optional suggestions.
- The variant direction, or "none (the default proposal)".
- A product table: title, category, price, size, colors, materials, styles,
  position, facing direction, what it rests on.
- The pipeline's non-blocking validator findings.
- A top-down plan image with a legend for the colors, the facing line, doors
  and windows. Items whose title says round or oval are drawn as ellipses,
  the rest as rectangles.

The prompt tells the judge to use only this data and image. The answer has
a fixed structure, with an integer 1 to 5 per criterion, a one-sentence
reason for each, up to 3 top issues and up to 2 strengths.

For each run with 2 or more ready designs, one more call sees all of them
(directions, product lists, images) and scores how distinct they are.

### How the judge scores a design

Twelve factors, each scored 1 to 5. The anchors below are what the judge
receives for 1, 3 and 5, and 2 and 4 fall between. The last column says which
part of the input the score comes from.

**Selection** (what was chosen)

| Factor | 1 | 3 | 5 | Judged from |
| --- | --- | --- | --- | --- |
| Prompt match | Ignores or contradicts the brief | Covers the main request, misses some named details | Every explicit request met, mood matches | Brief vs product table |
| Style coherence | Clashing mix | Mostly coherent, one or two pieces off | One clear, consistent look | Colors, materials, styles vs variant direction |
| Budget use | Over the allowance, or so little spent the room is underfurnished | Within the allowance but poorly allocated (cheap anchor, costly accessories) | Within the allowance, spending on the anchor pieces | Budget, allowance, total cost, prices |
| Completeness | A core piece missing, or too few dining chairs | Core present, but an item the brief names is missing or items out of place | Core pieces and every named item, nothing out of place; a missing optional suggestion the brief does not name does not count | Core pieces and brief vs product table |
| Scale fit | Pieces clearly too big or too small | Workable, one or two pieces off-scale | All pieces well proportioned | Product sizes vs room size and each other |

**Layout** (where it was placed)

| Factor | 1 | 3 | 5 | Judged from |
| --- | --- | --- | --- | --- |
| Circulation | Door or main path blocked, items crammed | Passable but some paths tight (under about 0.6 m) | Clear paths of about 0.8 m or more from the door to every zone | Plan image, door positions |
| Functional grouping | Groups broken or pieces face walls | Main group works, some pieces misplaced or misoriented | Every group complete and correctly oriented | Positions and facing directions, plan image |
| Space use | Cramped into one area, or so sparse the room feels empty | Acceptable with a noticeable empty or crowded area | Balanced, each zone sized to its use | Plan image |
| Focal point | None | Present but the layout does not organize around it | Clear, and everything relates to it | Plan image, product roles |
| Layout quality | Looks random or broken | Workable but awkward in places | Looks professionally arranged | Plan image: alignment, spacing, balance, readable zones |

**Overall and run**

| Factor | 1 | 3 | 5 | Judged from |
| --- | --- | --- | --- | --- |
| Overall | Unusable | Acceptable but needs clear fixes | Ready to show a client | The whole design |
| Distinctness (per run) | Near copies: same products or same look | Some differences in products or palette, same overall impression | Clearly different looks and product sets, each a valid answer | All designs of one run, side by side |

With the scores the judge lists up to 3 top issues (most important first)
and up to 2 strengths per design, in one sentence each.

The rubric changes only between comparison cycles. After a rubric change,
review the `main` round again, because old scores are not comparable.

### Outputs

Each round's report starts with the pass rate per room type and over all
designs, read from the run records (no judge call). It then gives a score table with one row per design, averages over all
designs and per room type, and for each design its plan image, overall score,
top issues, strengths, and every criterion's score and reason. The review
lists failed designs without scoring them. A failed judge call shows up as an
error on its row and does not stop the review.

## Step 3: compare and report

1. Produce the `main` round and the change round the same way: same cases,
   same runs per case, same setup, same generation model, same judge.
2. Write `docs/pipeline/report-<date>.md` in the shape of
   [report-2026-10-12](report-2026-10-12.md): summary (better, worse,
   verdict), setup, what changed, results.
3. Report the results for `main` and the change side by side.
   - Pass rate, first.
   - Delivery and speed. Designs delivered, time without retrieve, median and p90/max run time, runs
     under 60 s, cost, selection turns per design, runs with 3 distinct layouts.
   - Reviewer scores. Averages per case and over all cases, at least
     completeness, circulation, space use, layout quality, overall and
     distinct. Add the full ten when they matter.
   - Any measure specific to the change, computed from the run records.
4. Read the judge's top issues and look at the plan images for the cases
   that moved most. The scores show which cases changed, and the images show
   what changed in them.
5. Give a verdict. Either the change is ready for a PR, or name what to fix
   first.

## Reading the numbers

- Nine designs per case is small. Treat a per-case move under about 0.5 as
  chance; trust moves that repeat across cases or show in the average over
  all 180 designs.
- Compare times only between rounds run the same way.
- The judge sees a plan, not a render. It cannot judge product looks beyond
  the listed colors, materials and styles, and it scores circulation from the
  drawn footprints.
- A full round costs about $8 to $10 of generation plus the judge calls. Use
  the quick subset while iterating and the full set for the report.

## Report example

These are the tables a report carries, with numbers from the
[2026-10-12 report](report-2026-10-12.md) (`main` against the change, 3 runs
per case). The review also gives, per design, the plan image, the overall
reason, top issues, strengths and every factor's reason.

### Delivery, speed and cost

| | main | change |
| --- | ---: | ---: |
| Designs delivered | 117 | 117 |
| Designs missing the bed, sofa or table | 0 | 0 |
| Median run | 33.1 s | 40.5 s |
| First design ready, median | 21.1 s | 27.2 s |
| p90 / max run | 45.3 / 69.0 s | 51.4 / 54.4 s |
| Runs under 60 s | 37 of 39 | 39 of 39 |
| Cost, 39 runs | $4.70 | $5.15 |
| Selection turns per design | 2.33 | 2.53 |
| Runs with three different layouts | 34 of 39 | 34 of 39 |

### Reviewer scores by case

Average of 9 designs per case, main / change. Distinct is per run.

| Case | Completeness | Circulation | Space use | Layout quality | Overall | Distinct |
| --- | --- | --- | --- | --- | --- | --- |
| Living room | 4.44 / 4.44 | 4.00 / 3.67 | 3.56 / 3.33 | 3.67 / 3.33 | 3.67 / 3.33 | 4.00 / 3.33 |
| Bedroom | 4.56 / 4.67 | 4.56 / 4.89 | 4.44 / 4.44 | 4.22 / 4.22 | 3.78 / 4.00 | 4.00 / 4.00 |
| Dining room | 3.00 / 3.78 | 4.44 / 4.22 | 4.67 / 4.44 | 4.44 / 4.00 | 3.67 / 3.89 | 3.67 / 3.67 |
| Tight dining room | 1.78 / 4.11 | 3.78 / 2.78 | 2.78 / 3.11 | 2.89 / 2.89 | 2.22 / 3.11 | 2.67 / 3.00 |
| Small bedroom | 3.00 / 3.00 | 2.33 / 2.56 | 3.00 / 3.00 | 2.89 / 3.00 | 2.78 / 2.89 | 3.67 / 3.33 |
| ... | | | | | | |
| **All cases** | **3.29 / 3.74** | **3.65 / 3.54** | **3.38 / 3.40** | **3.27 / 3.26** | **3.13 / 3.25** | **3.77 / 3.72** |

### One design, as the judge reports it

Tight dining room, variant 1, overall 3: "A complete, coherent dining set
within budget, but the chairs on the door side leave under 0.6 m to pass."

| Factor | Score | Reason |
| --- | --- | --- |
| Prompt match | 4 | Walnut table and upholstered chairs as asked; the requested bench is missing. |
| Style coherence | 4 | Walnut and beige textiles read as one mid-century set. |
| Budget use | 4 | 96% of budget, most on the table and chairs. |
| Completeness | 4 | Table, six chairs, sideboard and pendant; no wall art. |
| Scale fit | 3 | The 2.2 m table is long for a 3.0 m room. |
| Circulation | 2 | The chairs on the door side leave about 0.5 m to the wall. |
| Functional grouping | 4 | Chairs face the table, pendant centered over it. |
| Space use | 3 | The table fills the room; the sideboard wall is tight. |
| Focal point | 4 | Table under the pendant. |
| Layout quality | 3 | Aligned and centered, but crowded at the door. |

Top issues: path beside the door under 0.6 m; table too long for the room;
no bench. Strengths: one coherent material palette; pendant centered on the
table.
