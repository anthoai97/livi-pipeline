# Product retrieval test cases

Test cases for the [embedding pipeline](product-embedding-v2.md) and
`search_assets()`. Each case has an ID so later work can refer to it. When you
change retrieval, keep these cases passing. Add new cases here first.

The cases check two things:

- **Basic search (R, S, A):** each filter, outdated vectors, results, and
  arguments.
- **Slot search (L):** the room pipeline's search pattern. It runs one
  search for each product slot of a request, all at the same time.

The embedding job (J) and speed (T) cases are listed at the end.

Status:

- **Automated:** covered by `product-data/tests/test_search_assets.py`.
- **Report:** checked by the report script only, not by pytest.
- **Manual:** checked by hand on October 9, 2026. Not automated yet.
- **Planned:** not tested yet.

## Run the tests

```bash
.venv/bin/python -m pytest product-data/tests
.venv/bin/python product-data/tests/retrieval_report.py
```

The tests read the local database through `LOCAL_CONNECTION_STRING`; run
`embed_assets.py` first. Basic search tests use stored product vectors as
queries, so they make no API calls. Slot tests embed each slot's search text with
Gemini and need `GEMINI_API_KEY`. Tests that change data run inside a transaction
and roll it back.

Each test compares search results with a separate Python check of the same
rules. Slot tests also compute similarity by brute force over every stored
vector, so they check ranking as well as filters.

The report script runs pytest, repeats each case, and writes a dated report to
`reports/`. Agents can use the `retrieval-test-report` skill.

## Slot search

The slot cases use one living-room request: "a cozy cream boucle sofa under
USD 1,500, no rug", with a total budget of USD 4,000. The pipeline plans the
slots in code; the tests fix them in `LIVING_ROOM_SLOTS` in `test_search_assets.py`:

| Slot | Kind | Search text | Categories | Keep |
|---|---|---|---|---:|
| sofa | requested | cream boucle sofa, cozy, modern | sofa and related categories | 10 |
| plant | required | indoor plant, cozy, modern | planter, plant_stand (design-only kept) | 10 |
| coffee table, side table, lamp, accent chair, storage, media | optional | "<slot>, cozy, modern" | that slot's categories | 6 each |
| decor | decor | decorative accent, cozy, modern | planter, sculpture, floor_mirror, wall_mirror | 4 |

There is no rug slot, because the user excluded rugs. Every slot searches with
its categories, a price no higher than the item's limit and the USD 4,400
allowance (110% of the budget), placeable, and `known_price=True`. Each fetches
30 products.

| ID | Case | Expected behavior | Status |
|---|---|---|---|
| L1 to L9 | Each slot's search | Returns the 30 products nearest to the search text among those that pass the filters, in the same order as the brute-force check. After the plant slot keeps only design-only products, each slot still has its keep size. | Automated |
| L10 | All slots at the same time | Each slot, on its own connection, returns the same products in the same order as when searched alone. | Automated |
| L11 | No match | A slot no product can meet (dresser at most USD 5) returns an empty list and no error, so the run can report a gap. | Automated |

Checks after the search that need room geometry, such as fitting the floor and
minimum sizes, belong to the pipeline and are not tested here.

## Exact filters

| ID | Case | Input | Expected behavior | Status |
|---|---|---|---|---|
| R1 | Purchase search | Category `sofa`, purchase only, price at most 1,500 USD, width at most 2.5 m, color `cream` | Returns exactly the purchasable cream sofas within price and width, and nothing else. | Automated |
| R2 | Room-design price limit | Placement `surface`, price at most 50 USD, purchase not required | Returns design-only items regardless of price, plus purchasable items at most 50 USD. Both kinds appear. | Automated |
| R3 | Unknown price | A purchasable product with no price | Found without a price limit. Excluded when any price limit is set, in purchase and room-design searches. | Automated |
| R4 | Unknown purchase status | A product with `is_purchasable` null | Excluded from purchase search. Excluded under a room-design price limit; only known design-only items skip it. | Automated |
| R5 | Unknown placement | A product with no placement type | Excluded by default (room design). Found when unplaceable products are allowed. | Automated |
| R6 | Unknown dimension | A product with a null axis and a size limit on that axis | Excluded. A null value never meets a size limit. | Planned (no local product lacks a dimension) |
| R7 | Required attributes | Several colors, styles, or materials | The product must have every requested value. | Automated in R1 (color only) |
| R8 | Currency | Price limit in a currency the product does not use | Excluded. Prices are not converted. | Planned |
| R9 | Known-price pool filter | Category `side_table`, `known_price=True`, limit 1,000 | Returns exactly the placeable design-only side tables and those priced in USD. Purchasable side tables with no price are left out. | Automated |

## Stale and ineligible vectors

| ID | Case | Change to the prepared product | Expected behavior | Status |
|---|---|---|---|---|
| S1 | Text changed | Description edited | The old vector is excluded from search until the job rebuilds it. | Automated |
| S2 | Image URL changed | New image URL | The old vector is excluded until rebuilt. | Automated |
| S3 | Became ineligible | Category set to null | The vector is excluded from search. The next job run deletes it and lists the product as ineligible. | Automated (search); Manual (job) |
| S4 | Price changed | Price edited | The vector stays current and searchable. No rebuild. | Automated |

## Results and arguments

| ID | Case | Expected behavior | Status |
|---|---|---|---|
| A1 | Ranking | Results are ordered by similarity, highest first. A product's own vector returns it first with similarity 1.0. | Automated |
| A2 | Complete records | Each result has every prepared field plus `similarity`. | Automated |
| A3 | Invalid arguments | Limit outside 1 to 1,000, lowercase currency, zero or negative price, or negative size raises `ValueError`. | Automated |

## Embedding job

| ID | Case | Expected behavior | Status |
|---|---|---|---|
| J1 | Unchanged rerun | No embedding calls. Every product is reported as skipped. | Manual (5,227 skipped, 0 calls) |
| J2 | Price-only change | The product is skipped. | Manual |
| J3 | Text change | Only that product is rebuilt. | Manual |
| J4 | New image content at the same URL | The conditional request downloads new bytes, the hash changes, and only that product is rebuilt. | Manual (simulated with a changed stored hash) |
| J5 | Ineligible product | Listed in the report with the missing field. Any old vector is deleted. | Manual (13 listed) |
| J6 | Failed product | Listed in the report with a reason and retried on the next run. If its inputs changed, the old vector is deleted. | Planned |
| J7 | Image formats | WebP and AVIF images are converted to JPEG and embedded. PNG and JPEG are sent as they are. | Manual (full run, 0 failures) |
| J8 | Text format | Stored text matches [the documented format](product-embedding-v2.md#build-the-text). Empty fields are omitted, and only known dimensions are listed. | Manual |

## Speed

| ID | Case | Expected behavior | Status |
|---|---|---|---|
| T1 | Slot search time | Database time for all slots of one request, searched at the same time, is under 500 ms, below 1% of the 60-second request target. On October 9, 2026: 55 ms for 9 slots; 3 to 13 ms per slot. | Report |
| T2 | Search text embedding time | Recorded separately from search time, with no limit. On October 9, 2026: 0.85 s for 9 texts at the same time. | Report |
