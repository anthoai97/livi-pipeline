# Pipeline

Python LangGraph pipeline that turns a prompt, room geometry, and budget
into furnished room designs. See [Pipeline overview](../docs/pipeline/legacy-pipeline-overview.md)
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

Each request writes one run record, with stage timings, model calls, tokens,
cost, and variant outcomes, to `pipeline/.data/runs/<run_id>.json`.

Run the tests with `python -m pytest` from `pipeline/`.
