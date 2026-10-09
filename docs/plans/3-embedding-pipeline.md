# Phase 2: build the embedding pipeline

Issue: [#3](https://github.com/anthoai97/livi-pipeline/issues/3). Parent: [#1](https://github.com/anthoai97/livi-pipeline/issues/1).
Status: implemented locally on October 9, 2026. All 5,227 eligible products have a
current embedding, with 0 failures. 13 products are ineligible: 11 have no image
and 2 have no category. A full rerun with no changes made 0 embedding calls.
Exact search takes about 30 ms with a category or placement filter and 87 to 121 ms
without one. Query embedding takes 0.55 to 0.9 s. No approximate index is needed yet.
See [the retrieval test report](../../reports/2026-10-09-product-retrieval-tests.md).

Build a searchable furniture and decor catalog before room generation starts.
The generation request then embeds its search query and retrieves prepared products.
The overall target remains three validated layouts in under 60 seconds.

## Starting point

The [preparation report](../data/product-preparation-v2-report.md) records 5,240 prepared
products, of which 5,227 have the fields required for embedding. This is a reported
count; image downloads must still succeed. Use `pipeline.pipeline_assets_v2` and
the existing [embedding text format](../data/product-embedding-v2.md).

Docker already includes pgvector. The repository has no embedding worker or
embedding table. Build the offline Python job under `product-data/`.
The Python runtime in `pipeline/` will consume the same vectors and prepared
records through LangGraph and Jev in later phases.

Storage, embedding generation, retrieval, and catalog population are all part of
this one phase. Phase 1 targets are recorded in [the comparison plan](2-generation-baseline.md).

## Build order

1. **Add storage.** Create `pipeline.asset_embeddings_v2`, keyed by the existing
   `asset_id`. Store the vector, exact embedding text, image fingerprint, input
   hash, model settings, and completion time. Reference the prepared record with
   a foreign key. Keep the existing table names and contract versions.
2. **Build product embeddings.** Follow the documented field order without another
   extraction pass. Download and cache the matching image under `.data/`, convert
   supported images to PNG or JPEG as needed, and embed text plus image together.
   Use the agreed `gemini-embedding-2` model. Start with 768 dimensions.
   It supports a combined text-and-image vector; use the same model and dimensions
   for queries. See [Google's embedding documentation](https://ai.google.dev/gemini-api/docs/embeddings).
3. **Support reruns.** Use limited concurrency, bounded retries, and per-product
   saves. Skip unchanged inputs. Hash the text, image content, and model settings;
   detect changed image content even when its URL stays the same. Report failed
   or ineligible asset IDs with reasons and retry them on the next run. Exclude
   stale or ineligible embeddings from search. Price changes alone need no new vector.
4. **Add retrieval.** Embed the semantic query, apply exact filters, and rank by
   cosine distance. Return complete prepared records with similarity scores.
   Start with exact pgvector search using the existing category index; add an
   approximate index only if measured lookup time warrants it.
   [pgvector documents this filtering approach](https://github.com/pgvector/pgvector#filtering).
5. **Check and populate.** Start with a small furniture and decor sample, verify
   retrieval and reruns, then process the remaining eligible prepared records.
   Report successes, skips, failures, and query time.

Retrieval checks category, purchase status, price and currency, dimensions, and
explicit required attributes against prepared fields. Unknown values cannot meet
strict requirements. Room generation also requires usable models and known
placement data. Design-only decor stays eligible without contributing to purchase
cost. Complete-room budget, counts, density, and geometry remain downstream checks.

## File changes

| File | Action | Planned change | Why |
| --- | --- | --- | --- |
| `product-data/src/embed_assets.py` (proposed) | Create | Text building, image cache, embedding calls, resumable CLI job. | Preparation currently stops at product records. |
| `product-data/sql/002_asset_embeddings_v2.sql` (proposed) | Create | Enable vector extension and create embedding storage. | The current SQL only defines prepared assets. |
| `product-data/src/search_assets.py` (proposed) | Create | Query embedding and filtered retrieval function with a small CLI. | Verify the retrieval behavior before runtime integration. |
| [product-data/requirements.txt](../../product-data/requirements.txt) | Edit | Add vector and image adapters; verify SDK support. | Store vectors and decode product images. |
| [docs/data/product-embedding-v2.md](../data/product-embedding-v2.md) | Edit | Document the selected model, commands, and rerun behavior. | Keep one embedding reference. |

## Completion checks

Each eligible product has one current embedding or an explicit failure reason.
An unchanged rerun makes no product embedding calls; changed text or images rebuild
only affected products. Check representative searches for all four room types and
confirm exact filters hold, including unknown prices and design-only decor.
Measure query embedding and database lookup time separately for later comparison.

The initial run targets the local database through `LOCAL_CONNECTION_STRING`.
Preview delivery to the running room service belongs to its later integration;
the image cache is prepared here.

## Unresolved questions

None.
