# Phase 4 benchmark

Benchmark of the phase 4 runtime ([#5](https://github.com/anthoai97/livi-pipeline/issues/5))
against phase 3 on the same requests, October 9, 2026. Durations are seconds.
Plan: [5-selection-placement.md](../plans/5-selection-placement.md).

## Setup

- Requests: the four phase 3 requests in `pipeline/scripts/benchmark_requests.json`.
  The phase 3 run records kept only door and window counts, so geometry is
  rebuilt: rectangular rooms, the dining room's door and window from the legacy
  fixture, and one door plus the recorded windows for the others. These results
  are therefore not comparable with [phase 3 local runs](phase-3-local-runs.md).
- Phase 3 baseline: `main` at `2f1ead4` on the same requests.
- Model `gemini-3.8-flash` at `LOW` thinking, Jev `jev-1.13.0`, local catalog.
- Runs are sequential, with `caffeinate -i`. One baseline run that overlapped
  a machine sleep was discarded and rerun.
- Command: `python scripts/benchmark.py --runs N`, with the switches set on the service.

## Results

"Valid runs" means all three layouts passed final validation.

| Configuration | Runs | Median | Max | Valid runs | Valid layouts | Under 60 s | Cost per run (USD) | Non-blocking findings per run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Phase 3 baseline | 12 | 89.5 | 273.4 | 9 | 27 of 36 | 3 | 0.214 | not recorded |
| Phase 4, `rank,check`, refinement `jev` | 12 | 68.7 | 213.3 | 10 | 34 of 36 | 3 | 0.124 | 0.42 |
| Refinement `off` | 4 | 69.3 | 77.2 | 4 | 12 of 12 | 1 | 0.107 | 0.75 |
| Refinement `always` | 4 | 70.9 | 217.4 | 3 | 11 of 12 | 1 | 0.148 | 0.75 |
| Jev rank off (`check`) | 4 | 73.4 | 122.8 | 2 | 10 of 12 | 1 | 0.138 | 0.75 |
| Jev check off (`rank`) | 7 | 50.3 | 92.7 | 6 | 20 of 21 | 4 | 0.115 | 0.29 |

The check-off row has 7 runs: 4 from a full rerun, plus 3 that finished before
an interrupted pass was stopped.

Studio: phase 3 produced no valid studio layout in 9 variants. Phase 4 defaults
produced 7 of 9.

## Where the time goes

Model call times in the phase 4 default runs, against the phase 3 baseline:

| Call | Phase 4 median | Phase 4 p90 | Phase 4 max | Phase 3 median | Phase 3 max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Interpretation | 9.4 | 12.1 | 14.1 | 8.6 | 19.6 |
| Selection | 5.6 | 19.0 | 86.3 | 14.1 | 43.8 |
| Placement | 5.2 | 30.8 | 65.1 | 6.6 | 10.1 |
| Correction | 29.7 | 52.0 | 182.2 | 14.6 | 159.5 |
| Refinement | 10.9 | 17.4 | 182.8 | - | - |
| Jev rank (per slot) | 0.6 | 0.6 | 1.1 | - | - |
| Jev check | 0.3 | 0.4 | 0.5 | - | - |

- Short outputs made the typical selection call 2.5 times faster. The slow calls
  now come from thinking: a selection, placement-edit, or correction call that
  thinks for 2,000 to 6,000 tokens takes 15 to 50 s. `LOW` is the lowest level
  this model accepts.
- Correction runs far less often (16 calls in 12 runs, against 94 in phase 3),
  but the calls that remain handle the hard cases and think longer.
- Both 213 s runs had a Gemini `504 DEADLINE_EXCEEDED` call retried 3 times at
  60 s each. A failed correction or refinement call now keeps the current layout
  (fixed after these runs). A failed placement or selection call still fails
  the variant.
- The seed, repair (about 0.1 s), and all Jev calls are small.

## Findings by option

| Decision | Result | Default |
| --- | --- | --- |
| 4a Jev rank | Rank off gave 2 of 4 valid runs, and the check then rejected 6 selections. Rank on gave no check rejections in 22 runs. | On |
| 4b, 4d Jev check | It rejected nothing when rank was on, so it costs about 0.3 s per selection. The faster check-off median comes from model-time variation, not from the check. | On |
| 7 Refinement | `jev` gave the fewest non-blocking findings (0.42 against 0.75). The target fails with every option, so the plan's rule sets the default to `off`. | `off` |
| 8 Thinking | `MINIMAL` is rejected, and budgets 0 and 512 did not cut thinking. | `LOW` |
| 1c Seed as the layout, 2b no code repair | Not run: no switch exists. | Not changed |

## Per-step models and distinct products

These runs came after the first results: refinement default `off`, `LLM_STAGE_MODELS`
for per-step models, and `PRODUCT_REUSE_RATE` for distinct products. 12 runs each.
"Lite" is `gemini-3.5-flash-lite` at `MINIMAL` thinking.

| Configuration | Median | Max | Valid runs | Valid layouts | Under 60 s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Phase 4 defaults (all `gemini-3.8-flash`, from above) | 68.7 | 213.3 | 10 | 34 of 36 | 3 |
| Lite placement | 74.1 | 300.0 | 11 | 35 of 36 | 5 |
| Lite placement and correction | 36.0 | 78.4 | 8 | 31 of 36 | 10 |
| Lite placement and correction, escalating to `gemini-3.8-flash`, reuse rate 0.5 | 68.7 | 220.3 | 10 | 34 of 36 | 5 |

- Lite placement: 33 calls, median 2.0 s, max 3.1 s, and none thought. It
  leaves more work for correction and more non-blocking findings (1.6 per run
  against 0.42).
- Lite correction: 29 calls, about 1.9 s each, no thinking. It could not fix
  bedroom and studio findings (`bed_headboard`, `bed_access`, `bedside`,
  `studio_tv_viewing`, `media_group`), and 5 of 36 variants failed.
- Escalation: 27 lite correction calls (1.9 s each), then 24 escalated
  `gemini-3.8-flash` calls (29 s each). Living and dining rooms took 24 to 70 s.
  Bedrooms took 57 to 87 s and studios 96 to 220 s.
- Reuse limit: on the 4 requests at defaults, every variant shared 0% of its
  products with the others. Its own dealt products rank first for each variant,
  so the 50% cap was never reached.

## Stage 2: code layout solver

`PLACEMENT=solver`: code places every group and scores candidates with the
checker. Correction is lite, escalating to `gemini-3.8-flash` as a fallback.
Slow calls get a duplicate after 8 s. Reuse rate 0.5. 12 runs.

| Configuration | Median | Max | Valid runs | Valid layouts | Under 60 s | Cost per run (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stage 1 defaults | 68.7 | 213.3 | 10 | 34 of 36 | 3 | 0.124 |
| Stage 1, lite escalating | 68.7 | 220.3 | 10 | 34 of 36 | 5 | 0.122 |
| **Stage 2 solver** | **31.1** | **47.9** | **11** | **35 of 36** | **12** | **0.062** |
| Stage 2 solver after polish and cleanup | 31.9 | 45.4 | 11 | 35 of 36 | 12 | 0.062 |

| Step | Median | p90 | Max | Share of critical path |
| --- | ---: | ---: | ---: | ---: |
| interpret | 8.4 | 11.9 | 18.2 | 30% |
| select | 5.6 | 14.3 | 25.7 | 62% |
| place (solver) | 0.40 | 0.86 | 1.18 | 1% |
| retrieve, rank, repair, validate | 2.1 | - | - | 7% |

- The solver cleared every layout it was given: no correction call, swap, or
  chair drop ran.
- The failed variant was a living-room selection that failed its fit check
  4 times. Placement was not the cause.
- Non-blocking findings rose to 2.5 per run, against 0.42 in stage 1:
  - `sofa_wall_gap`: sofas floated off the wall to bring the TV into range
  - `table_lamp_support`: bedroom table lamps placed on the desk, not the nightstands
  - `rug_composition`, `media_group`, and `floor_lamp_reach`
- Duplicate calls fired 16 times on select and 7 times on interpret. A normal
  interpretation takes 8 to 9 s, so the 8 s threshold triggers too often there.
- After the layout polish (lamps on nightstands, no sofa floated into a flagged
  gap, floor lamps by the reach rule, rug checks), non-blocking findings fell
  from 2.5 to 1.25 per run. The remaining ones are TV viewing distance
  (`media_group`: living x4, studio x8) and a few studio `sofa_wall_gap` (x3).
  In a 5.5 x 7 m studio, the selected 1.0 to 1.2 m TVs are too far from the sofa
  even across the short side. Lamp, floor-lamp, and rug findings are gone. The
  one failure was again a living-room selection that failed its fit check.

## Outcome

The 60-second target is not met. The best valid setup so far (escalation) has
5 of 12 runs under 60 s. Lite placement and correction reaches 10 of 12, but it
loses 5 of 36 layouts. Living and dining rooms are fast in every lite setup.
Bedrooms and studios stay slow because their relationship findings need a long
`gemini-3.8-flash` correction. Selection calls that think (14 of 55 in the
escalation runs) are the other tail. These are 4 to 12 runs per option, so treat
the differences between options as directional.
