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
| `LLM_STAGE_MODELS` | Optional model and thinking level per stage, such as `select=gemini-3.5-flash-lite:minimal,correct=gemini-3.5-flash-lite:minimal`. Stages: `interpret`, `select`, `correct`. `correct_escalate` sets the model correction switches to after a proposal does not improve; without it, correction stops there. Unlisted stages use `LLM_DESIGN_MODEL` at `low`. |
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
