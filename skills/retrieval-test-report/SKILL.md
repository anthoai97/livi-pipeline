---
name: retrieval-test-report
description: "Run the product retrieval tests against the local embedded catalog (basic search, the pipeline's slot searches, and speed) and write a report to reports/ with each case's input, expected result, actual result, and verdict, plus reviewed findings. Use after changing embedding, search, or prepared product data, or when asked to test or report on retrieval."
---

# Retrieval test report

Run the retrieval tests and write a dated report that someone else can check
without rerunning anything. The cases and their rules are in
`docs/data/product-embedding-v2-retrieval-tests.md`. That document is the
source of truth.

The report checks two things:

- **Basic search (R, S, A):** each filter, outdated vectors, results, and
  arguments.
- **Slot search (L):** one search per product slot of a living-room request,
  the way the room pipeline searches. Each must return exactly the nearest products
  that pass its filters, and all slots must work when searched at the same time.

## Check prerequisites

- The local Postgres container is running.
- `.env` has `LOCAL_CONNECTION_STRING` and `GEMINI_API_KEY`.
- Embeddings are current. If prepared products or the embedding code changed
  since the last job run, run `.venv/bin/python product-data/src/embed_assets.py`
  first. In the report's Catalog row, the embedded and current counts must be
  equal.

## Generate the report

```bash
.venv/bin/python product-data/tests/retrieval_report.py
```

The script runs pytest and repeats every case. It writes
`reports/<YYYY-MM-DD>-product-retrieval-tests.md`, replacing any report from the
same day. It takes about 10 seconds. Data edits are rolled back. The only API
calls embed the slot search texts, which costs well under one cent.

## Review the results

- Results are deterministic. Any FAIL is a bug in the search code, a data
  change, or a test that no longer matches how the pipeline searches. Rerunning will not
  change it.
- For each FAIL, compare the case's Input, Expected, and Actual rows, and
  confirm that pytest agrees. If pytest and the report disagree, the two have
  drifted apart; fix the drift.
- For a slot case that returns fewer products than its keep size, check the
  catalog: count the products that pass the slot's filters. Few matches is a
  catalog gap, not a search bug.
- Compare speed (T1, T2) with the previous report in `reports/` when one exists.
- Do not loosen an expectation or remove a case to make it pass. Report the
  failure and propose the change.

## Write the findings

Replace the comment under `## Findings` in the generated report. Write for a
reader who has not seen the test code. Keep it short:

1. One or two sentences with the overall result.
2. Each failure or change since the previous report, in plain words: what
   was searched, what was expected, what came back, and where the fix belongs
   (search code, product preparation, or the test).
3. Speed, with the numbers.

If everything passes and nothing changed, say so in one sentence. Run the
`unslop` skill on the result. Do not edit the generated sections.

## Add or change cases

1. Add or edit the case in `docs/data/product-embedding-v2-retrieval-tests.md`.
2. Add the pytest test to `product-data/tests/test_search_assets.py`. Slot
   definitions live in `LIVING_ROOM_SLOTS` there; keep them matching the slots
   the pipeline plans.
3. Add the matching case to `product-data/tests/retrieval_report.py`.
4. Regenerate the report, and confirm the new case appears with its verdict.

## Finish

Tell the user the report path, the pass counts, and any failures in a few
lines. Do not commit unless asked.
