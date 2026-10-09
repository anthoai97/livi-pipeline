# Phase 3 local runs

First end-to-end check of the phase 3 runtime ([#4](https://github.com/anthoai97/livi-pipeline/issues/4)).
All durations are seconds.

Four requests to the phase 3 service (`pipeline/`) on October 9, 2026, one per
room type, with `gemini-3.8-flash` and the local prepared catalog. Labels use
request start time in Asia/Ho_Chi_Minh (UTC+7). Run records are in
`pipeline/.data/runs/<run_id>.json`, which is gitignored. There is no request
setup, preview download, refinement, or response synthesis in this runtime. The
web app rejects these variants until phase 5, so the runs were sent to the API
directly.

| Stage or milestone | Living room 16:49 | Bedroom 16:50 | Dining room 16:51 | Studio 16:52 |
| --- | ---: | ---: | ---: | ---: |
| Intent interpretation | 8.60 | 8.00 | 6.54 | 8.31 |
| Room context | 0.01 | 0.00 | 0.00 | 0.01 |
| Retrieval by slot | 1.33 | 1.10 | 1.13 | 1.34 |
| First variant ready | 39.37 | 28.57 | 24.09 | 61.05 |
| **Full request** | **67.44** | **44.37** | **88.47** | **154.28** |
| Variants ready | 3 of 3 | 3 of 3 | 3 of 3 | 2 of 3 |
| Model calls | 10 | 8 | 10 | 18 |
| Model cost (USD) | 0.137 | 0.112 | 0.128 | 0.260 |
| Run ID | `b5b6ce6e…` | `af3cb293…` | `56cb72b9…` | `b1005cb4…` |

Within each variant, selection and correction show seconds, with the number of
turns or proposals in parentheses. Variant elapsed time starts when retrieval
ends.

| Run | Variant | Selection | Placement | Correction | Final validation | Variant elapsed | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Living room | 0 | 28.67 (2) | 6.68 | 3.60 (1) | 0.01 | 38.98 | Ready |
| Living room | 1 | 20.96 (1) | 8.40 | 0 | 0.01 | 29.39 | Ready |
| Living room | 2 | 14.64 (1) | 6.08 | 36.71 (1) | 0.01 | 57.47 | Ready |
| Bedroom | 0 | 23.46 (2) | 4.95 | 0 | 0.01 | 28.44 | Ready |
| Bedroom | 1 | 28.94 (1) | 6.27 | 0 | 0.01 | 35.25 | Ready |
| Bedroom | 2 | 13.25 (1) | 6.17 | 0 | 0.01 | 19.45 | Ready |
| Dining room | 0 | 10.94 (1) | 5.44 | 0 | 0.01 | 16.39 | Ready |
| Dining room | 1 | 14.86 (1) | 5.45 | 0 | 0.01 | 20.33 | Ready |
| Dining room | 2 | 11.43 (1) | 5.93 | 63.37 (3) | 0.01 | 80.77 | Ready |
| Studio | 0 | 14.39 (1) | 7.36 | 122.80 (8) | 0.01 | 144.60 | Failed: `layout_validation_failed` |
| Studio | 1 | 14.83 (1) | 6.17 | 53.54 (1) | 0.01 | 74.57 | Ready |
| Studio | 2 | 13.67 (1) | 6.44 | 31.22 (2) | 0.01 | 51.37 | Ready |

The failed studio variant used all 8 correction proposals and still had one
overlap and one media-group finding. In bedroom variant 0, the first selection
overcrowded the room, so the second turn used capped counts and dropped the
nightstands and lamps. The slowest model calls were corrections of 36 to 54
seconds, when thinking grew to several thousand tokens. These four runs are a
first check of the new runtime, not a speed or quality benchmark.
