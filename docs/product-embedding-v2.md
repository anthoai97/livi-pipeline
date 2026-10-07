# Product embedding text

Build one embedding per product from the
[prepared product record](product-preparation-v2.md).
See [objectives and expected outcomes](product-preparation-v2.md#objectives-and-expected-outcomes)
for the intended effect on search and furniture selection.

Combine the descriptive fields in a consistent order. This example matches the
fictional sofa in that document:

```text
Product: Cream boucle sofa
Category: sofa
Description: Cream boucle sofa with curved arms and wooden legs, designed for living-room seating.
Color: cream
Style: modern
Materials: boucle fabric, wood
Dimensions: width 2.100 m; depth 0.900 m; height 0.800 m.
```

Include the brand when known. Omit unknown fields. Store the exact text used to
generate the embedding. Include the matching product image when generating the
product vector; retry if that image cannot be read.

For this user request:

> Find a modern cream sofa under USD 1,500, no wider than 2.5 metres.

Use this semantic query:

```text
Modern cream sofa.
```

Check the remaining constraints against the product fields:

- Category is `sofa`.
- Price is below 1,500 and currency is `USD`.
- Width is at most 2.5 metres.
- Color and style meet the user's requirements.

Vector search finds relevant products. Field checks enforce exact requirements.
Unknown values do not satisfy strict requirements.

Store vectors in `pipeline.asset_embeddings_v2`, linked by `asset_id`. Use the
same compatible embedding model and settings for products and queries. Rebuild a
product's embedding when its descriptive content or image changes.
