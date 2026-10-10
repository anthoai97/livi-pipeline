# Pipeline

Python LangGraph pipeline that turns a prompt, room geometry, and budget
into furnished room designs. See [Pipeline overview](../docs/pipeline/pipeline-overview.md)
and [Stack](../docs/Stack.md).

## Run

```sh
cd pipeline
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

`POST /pipeline` takes the geometry request and streams `data: {"type": ...}`
events. The service reads these variables from the repository `.env`:

| Variable | Use |
| --- | --- |
| `GEMINI_API_KEY` | Gemini model calls and query embeddings. |
| `LOCAL_CONNECTION_STRING` | Postgres with the prepared catalog and embeddings. |
| `LLM_DESIGN_MODEL` | Model for the design stages. Defaults to `gemini-3.8-flash`. |
| `LLM_STAGE_MODELS` | Optional model and thinking level per stage, such as `select=gemini-3.5-flash-lite:minimal,finish=gemini-3.8-flash:low`. Stages: `interpret`, `select`, `finish` (the final layout review). Unlisted stages use `LLM_DESIGN_MODEL` at `low`. |
| `MODEL_HEDGE_AFTER_S` | Seconds before a slow model call gets a duplicate; the first answer wins and the other call is cancelled. Defaults to `8`; `0` turns it off. |
| `JEV_API_KEY` | Jev (typesafe-sdk) yes/no questions. |
| `JEV_MODEL` | Jev model. Defaults to the pinned `jev-1.13.0`. |
| `JEV_USES` | Comma list of Jev uses: `rank` (order each slot's candidates per variant direction before they are dealt) and `check` (style and attribute checks of a selection). Defaults to `rank,check`. Leave it empty to turn both off. |
| `PRODUCT_REUSE_RATE` | Largest share, from 0 to 1, of a variant's distinct products that other variants may also use. `0` gives every variant different products; `1` sets no limit. Defaults to `0.5`. |

The service reads `JEV_USES` and `PRODUCT_REUSE_RATE` once per request. A failed Jev
call never fails a variant: that use falls back to its Jev-off behavior.

Each request writes one run record, with stage timings, model and Jev calls,
tokens, cost, switches, notes, and variant outcomes, to
`pipeline/.data/runs/<run_id>.json`. Each ready variant also keeps its
direction, total cost, products, and layout poses for review.

## Browser delivery

Every `node_complete` event includes `data` with facts from that stage:

| Stage | Display data |
| --- | --- |
| interpret | `style_hints`, `requested_categories` |
| room | `room_area`, `wall_height`, door and window counts, `floor_area_sqm`, `usable_area_sqm`, protected path count, `fit`, `fit_message` |
| retrieve | Slot and candidate counts, gap labels, and `preview` with the top product per nonempty slot |
| rank | `ranked_slots`, `shared_products` (distinct products held by multiple variants, including small shared slots) |
| select | `turn`, `valid`, error count, `fit_step`, `total_cost`, and unique selected `items`, capped at the slot count |
| place | `placed`, finding and blocking counts, kept `swaps` and `drops`, `layout_options` (tied layouts offered to the review) |
| finish | `valid` and `errors` (blocking findings left), `dropped` item count, `layout_pick` (A to D, or null when the review failed), `review` (text, or null) |

After finish builds the delivery, `app/preview.py` draws the layout in a worker thread.
The PNG is saved to `<runs_dir>/<run_id>/variant_<index>.png`, where `runs_dir`
defaults to `pipeline/.data/runs`. The ready variant includes
`preview_url: "/runs/<run_id>/previews/<index>.png"`. A render or image write
failure leaves the variant ready with `preview_url: null`. The record's
`previews` list stores each variant index, elapsed seconds, and error or null.

`GET /runs/{run_id}/previews/{index}.png` serves the PNG. Malformed IDs, indices
outside 0 to 2, and missing images return 404. Run IDs are 32 lowercase hex characters.

`POST /runs/{run_id}/client-timing` accepts the following JSON fields and returns 204:

| Field | Allowed value |
| --- | --- |
| `variant_index` | Integer, 0 to 2 |
| `received_ms`, `loaded_ms`, `displayed_ms` | Finite numbers, 0 to 86,400,000, measured since request submission and in that order |
| `models`, `failed_models` | Integers, 0 to 10,000; failures cannot exceed the model count |
| `screen` | `generate`, `chooser`, or `studio` |

Unknown fields and invalid values return 422. Unknown runs return 404.
Entries append to the record's `client_timing` list, during or after generation.
A shared lock coordinates timing submissions with the final record write.
Disconnecting cancels unfinished variants and writes a `cancelled` record that
retains ready variants and timing entries. These routes run in one service process.

## Checks and benchmarks

Run the tests with `python -m pytest` from `pipeline/`.

To compare options, start the service, then replay the benchmark requests and
summarize their run records from `pipeline/`:

```sh
python scripts/benchmark.py --runs 3
```

Set `JEV_USES` or `PRODUCT_REUSE_RATE` on the service to benchmark another option.

To score the output, review run records with an LLM reviewer from `pipeline/`:

```sh
python scripts/review.py --latest 12        # or --runs <run_id> ...
```

For each ready variant, the reviewer gets the prompt, budget, room, product
table, and a top-down plan. It returns 1-5 scores for selection (prompt match,
style, budget use, completeness, scale) and layout (circulation, grouping,
space use, focal point), an overall score, and top issues. One more call per
run scores how distinct its variants are. The review writes `report.md`,
`review.json`, and the plan images to `pipeline/.data/reviews/<UTC timestamp>/`.
Failed variants, and records written before variants kept their products, are
listed but not reviewed.

| Variable | Use |
| --- | --- |
| `AI_PROXY_BASE_URL`, `AI_PROXY_API_KEY` | The local cli-proxy-api that serves the reviewer model. Required. |
| `REVIEW_MODEL` | Reviewer model. Defaults to `gpt-6.1-sol`; `--model` overrides it. |
| `REVIEW_EFFORT` | Reviewer reasoning effort. Defaults to `medium`; `--effort` overrides it. |

`--workers` sets how many reviewer calls run at once (default 3).
