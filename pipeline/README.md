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
| `LLM_STAGE_MODELS` | Optional model and thinking level per stage, such as `place=gemini-3.5-flash-lite:minimal,correct=gemini-3.5-flash-lite:minimal`. Stages: `interpret`, `select`, `place`, `correct`, `refine`. `correct_escalate` sets the model correction switches to after a proposal does not improve; without it, correction stops there. Unlisted stages use `LLM_DESIGN_MODEL` at `low`. |
| `JEV_API_KEY` | Jev (typesafe-sdk) yes/no questions. |
| `JEV_MODEL` | Jev model. Defaults to the pinned `jev-1.13.0`. |
| `JEV_USES` | Comma list of Jev uses: `rank` (order each slot's candidates per variant direction before they are dealt) and `check` (style and attribute checks of a selection). Defaults to `rank,check`. Leave it empty to turn both off. |
| `PRODUCT_REUSE_RATE` | Largest share, from 0 to 1, of a variant's distinct products that other variants may also use. `0` gives every variant different products; `1` sets no limit. Defaults to `0.5`. |
| `REFINEMENT` | Composition pass after correction: `off`, `always`, or `jev` (run when Jev says the layout needs it). Defaults to `off`; see the [phase 4 benchmark](../docs/pipeline/phase-4-benchmark.md). |

The service reads `JEV_USES`, `REFINEMENT`, and `PRODUCT_REUSE_RATE` once per request. A failed Jev
call never fails a variant: that use falls back to its Jev-off behavior.

Each request writes one run record, with stage timings, model and Jev calls,
tokens, cost, switches, notes, and variant outcomes, to
`pipeline/.data/runs/<run_id>.json`.

Run the tests with `python -m pytest` from `pipeline/`.

To compare options, start the service, then replay the benchmark requests and
summarize their run records from `pipeline/`:

```sh
python scripts/benchmark.py --runs 3
```

Set `JEV_USES`, `REFINEMENT`, or `PRODUCT_REUSE_RATE` on the service to benchmark another option.
