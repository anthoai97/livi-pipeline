# Product embedding text

Build one embedding per product from `pipeline.pipeline_assets_v2`, using the
[prepared product record](product-preparation-v2.md).
Furniture and decor share this format and the `pipeline.asset_embeddings_v2`
index. Their raw sources remain separate during preparation.

## Objectives

1. **Match the user's context.** Retrieve furniture and decor that match what the user asks
   for, including product type, intended use, style, color, materials, and preferences.
2. **Support the room-design pipeline.** Retrieve catalog furniture and decor that
   the model can use with the user's prompt, room geometry, budget, and preferences.
   The model chooses products and quantities, then decides where each piece goes.

For the second objective, pass the retrieved products and their full prepared
records to selection and placement. Validation uses those same product facts to
check budget, counts, requested attributes, and fit constraints. Failures return
to the model with specific reasons so it can correct the selection or placement.

Embeddings supply relevant candidates. Prices, dimensions, and room geometry
support exact checks. Keep each user's room and budget in the request rather than
in the reusable product embedding.

See [expected outcomes](product-preparation-v2.md#objectives-and-expected-outcomes)
for how to measure improvement.

## Prepare the input

The extraction LLM fills descriptions and supported attributes from raw text and
matching images. Build embedding text directly from that prepared output. Preserve
its facts and unknown values instead of adding another round of guesses.

## Build the text

Use these fields in this order:

| Text label | Prepared field |
|---|---|
| Product | `title` |
| Category | `category` |
| Brand | `brand` |
| Description | `description` |
| Color | `colors`, joined with commas |
| Style | `styles`, joined with commas |
| Materials | `materials`, joined with commas |
| Placement | `placement_type` |
| Dimensions | Known values from `width_m`, `depth_m`, and `height_m`, with axis labels and units |

Require a useful title, correct category, factual description, and working matching
image. Omit optional lines whose values are null or empty. If only width is known,
write only that width; do not fill depth or height with estimates or zeros.

Use the normalized labels and list order produced during extraction. Include the
full prepared description, including supported appearance, use, and features.
It can be LLM-written even when the raw description was missing, provided its
claims come from the supplied facts or visible details.

This example matches the fictional sofa in the preparation document:

```text
Product: Cream boucle sofa
Category: sofa
Description: Cream boucle sofa with curved arms and wooden legs, designed for living-room seating.
Color: cream
Style: modern
Materials: boucle fabric, wood
Placement: floor
Dimensions: width 2.100 m; depth 0.900 m; height 0.800 m.
```

Brand is omitted because it is unknown in this example. Store the exact text used
to generate the embedding. Include the matching product image as an image input;
retry if it cannot be read.

Keep `asset_id`, `source_table`, `source_id`, `is_purchasable`, price, currency,
and URLs outside the embedding text. They support record lookup, exact filters,
and asset loading. The image itself contributes to the embedding; its URL is not
descriptive text.

Decor uses the same labels. Include factual appearance and intended placement so
requests such as "a small sculpture for a bookshelf" can find surface decor.
Use dimensions and supporting-furniture geometry to check whether it fits.

## Use the user's context

For this user request:

> Find a modern cream sofa under USD 1,500, no wider than 2.5 metres.

Use this semantic query:

```text
Modern cream sofa.
```

Check the remaining constraints against the product fields:

- Category is `sofa`.
- `is_purchasable` is true for this purchase request.
- Price is below 1,500 and currency is `USD`.
- Width is at most 2.5 metres.
- Color and style meet the user's requirements.

Vector search finds relevant products. Field checks enforce exact requirements.
Unknown values do not satisfy strict requirements.

Room-design retrieval can include non-purchasable decor. Purchase retrieval
requires `is_purchasable = true`. Do not use a zero price or the decor category to
decide whether an item can be bought.

Store vectors in `pipeline.asset_embeddings_v2`, linked by `asset_id`. Use the
same compatible embedding model and settings for products and queries. Rebuild a
product's embedding when its descriptive content or image changes.

## Run the embedding job

The job uses `gemini-embedding-2` at 768 dimensions. It sends the embedding text
and the product image in one request, which returns one combined vector.
Gemini accepts PNG and JPEG images, so the job converts other formats, such as
WebP and AVIF, to JPEG on a white background.

```bash
# Small sample first, then the whole catalog.
.venv/bin/python product-data/src/embed_assets.py --assets 40 --decor 10
.venv/bin/python product-data/src/embed_assets.py --workers 16
```

- **Text:** the SQL function `pipeline.asset_embedding_text_v2` builds the text
  in the format above. The job stores the exact text it embedded.
- **Eligibility:** a product needs a title, category, description, and image URL.
  The run report lists each ineligible product and the missing field.
- **Image cache:** images are cached in `product-data/.data/images`. Each rerun
  sends a conditional request, so new image content at the same URL is detected.
- **Reruns:** the job hashes the text, image bytes, model, and dimensions. If the
  hash is unchanged, it skips the product and makes no embedding call. A price
  change alone does not change the hash.
- **Failures:** downloads and embedding calls are retried a few times. Products
  that still fail are listed in `product-data/logs/embed-assets-report.json`
  and retried on the next run.

## Search

Queries are text only. They use Google's retrieval format,
`task: search result | query: {query}`, with the same model and dimensions.

```bash
.venv/bin/python product-data/src/search_assets.py "modern cream sofa" \
  --category sofa --purchase --max-price 1500 --max-width 2.5 --color cream
```

`search_assets()` ranks by cosine distance with an exact scan and returns up to
1,000 full prepared records with a similarity score. Filters:

| Filter | Rule |
|---|---|
| Category, placement | The value must be in the requested list. |
| Purchase | With `purchase=True`, `is_purchasable` must be true. |
| Price | The price must be at most the limit, in the requested currency. Without `purchase=True`, design-only items also pass. |
| Dimensions | Each requested maximum must be met by a known value. |
| Colors, styles, materials | The product must have every requested value. |
| Placeable (default on) | The product needs a 3D model, a known placement, and all three dimensions. |
| Known price | With `known_price=True`, the product must be design-only or priced in the requested currency, so it can be counted against a budget. |

Unknown values never pass a filter. Search also skips a vector when its stored
text or image URL no longer matches the prepared record. The CLI prints query
embedding time and database lookup time separately.

Test cases and their expected behavior are listed in
[product retrieval test cases](product-embedding-v2-retrieval-tests.md).
