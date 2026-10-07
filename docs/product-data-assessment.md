# Product data acceptance checklist

Status: proposed acceptance criteria. These controls are not yet implemented.

A product passes when its metadata accurately describes the item, supports relevant
queries, and matches its embedding. Apply these controls to the searchable catalog,
including furniture and decor.

## Required controls

| Control | Pass condition | Example failure |
|---|---|---|
| Product identity | A stable asset ID, readable product title, and correct brand or source identify one product variant. | A record mixes different colors or sizes. |
| Category | The category matches the actual product and maps to the supported taxonomy. | Dinnerware classified as `dining_table`; the typo `dinning`. |
| Product purpose | Furniture, decor, accessories, and replacement parts are explicitly distinguished. | A sofa cover appears as a furnishing candidate. |
| Search description | `asset_description` identifies the product type and its distinguishing appearance, materials, and supported features. | “Beautiful furniture for every home.” |
| Factual accuracy | Product information, images, or measurements support the claims. | Invented seating capacity or material. |
| Color | Known colors use consistent names and agree with the selected variant and image. | `Default`, or “black” for a white product. |
| Materials | Materials use consistent names and distinguish upholstery, frame, and finish where known. | Wood-look laminate described as solid wood. |
| Style | Supported style labels describe the product. Uncertain style remains unknown. | `August25`, `Dropship SKU`, or a campaign name. |
| Shape | Shape describes the relevant product geometry and agrees with the model when present. | A round tabletop labeled rectangular. |
| Dimensions | Placement-ready products have verified width, depth, and height in metres, with a measurement source. | Inches stored as metres; package dimensions used. |
| Product image | The image downloads, shows the correct variant, and depicts the product clearly. | A broken URL, unrelated image, or wrong variant. |
| Consistency | Category, description, attributes, image, and model describe the same item. | Description says “bench,” but category says “ottoman.” |
| Duplicates | Duplicate imports are resolved. Meaningful variants remain distinct. | Several records represent the same product and variant. |
| Embedding | The vector exists, uses the intended embedding model, and matches current search content. | Metadata changed without regenerating the vector. |

Unknown values must remain unknown. Do not invent colors, materials, or styles to
satisfy completeness checks. An unknown attribute can pass when it is explicitly
recorded and omitted from the embedding. The product cannot qualify for a strict
query requiring that attribute.

Field presence does not establish accuracy. A populated URL does not prove that a
file works, and positive dimensions do not prove that measurements are correct.

## Controls by product use

| Use | Additional pass conditions |
|---|---|
| Room placement | The optimized model loads correctly. Its scale, orientation, and geometry match the product. |
| Shopping recommendation | Numeric price, currency, price basis, product URL, and purchase availability are verified. |
| Non-shoppable decor | Explicit decor status prevents purchase claims. The image, dimensions, and model support placement. |

Price and availability require structured filters. Exact dimensions and category
exclusions also require explicit checks after retrieval. Vector similarity alone
does not enforce those constraints.

## Assessment result

Record one result per product and intended use:

- **Pass:** All applicable required checks pass.
- **Review:** Evidence is missing or conflicting. Keep the product out of the
  approved pool for that use until resolved.
- **Blocked:** The product has a wrong identity or category, broken required
  assets, invalid measurements, or an unresolved duplicate record.

Store the failed checks, supporting evidence, reviewer, and assessment date.
Also record the embedding model, content hash, and whether an image was actually
included. Reassess products when relevant source data changes, including changes
to an image served at an unchanged URL.

## Description example

> Three-seat sofa with grey corduroy upholstery, tapered wooden legs, and a
> rectangular silhouette. Measures 2.10 m wide, 0.90 m deep, and 0.80 m high.

This is an illustrative example. Every detail in a real product description must
be supported by that product's evidence.

## Search quality validation

Validate the approved catalog against representative searches such as:

- “Cream bouclé sofa.”
- “Round oak dining table.”
- “Black metal floor lamp.”
- “Dining table under $1,000, no wider than 1.5 m.”

Record the expected matching products and relevant attributes for each query.
Review the first results for relevance, incorrect categories, duplicates, and
violations of strict constraints. Check that suitable products are retrieved when
the approved catalog contains them.

Passing data checks makes a product eligible for search. Query evaluation confirms
whether retrieval quality improves. Define the result count and acceptance
threshold before using this evaluation as a release requirement.

## Unresolved decisions

- Which category and attribute vocabularies are approved?
- What evidence and freshness requirements apply to dimensions, prices, and
  purchase availability?
- How many search results must be evaluated, and what relevance threshold must
  pass?
