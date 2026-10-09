# Product data before embedding

Target: `pipeline.pipeline_assets_v2`.

Prepare furniture and decor in this one table. Keep their raw sources separate:

- `catalog.assets`: raw product data.
- `pipeline.decor_items`: raw decor data.

Create one clean record for each item or product variant. The record describes
the item, provides search filters, and links to its image and 3D model. Track its
raw source using `source_table` and `source_id`.

Use an LLM to extract, normalize, and complete this record from raw product text
and matching images. It can write descriptions and infer visible attributes.
Missing facts stay unknown when the input does not support them.

## Objectives and expected outcomes

The prepared data serves two objectives:

1. Retrieve furniture and decor that match the user's context and preferences.
2. Support room design: the model uses the user's prompt, room geometry, budget,
   preferences, and retrieved product facts to choose catalog furniture and decor,
   decide quantities, and place each piece.

Validation checks budget, counts, requested attributes, and fit constraints using
the same product data. Failures return to the model for correction. Better product
facts should improve search relevance and the model's first selection and placement.

| Area | Expected improvement |
|---|---|
| Product search | Fewer wrong categories and accessories in furniture results. |
| Product selection | Better candidates and clearer descriptions help the model choose suitable items. |
| Requested attributes | Consistent colors, styles, materials, and features improve matching and validation. |
| Budget | Verified prices and currencies make cost calculations and budget checks reliable. |
| Fit | Accurate dimensions improve size checks. Final placement still requires room geometry and clearance checks. |
| Quantities | Verified seating capacity or set quantity helps when relevant. Embeddings alone do not determine the right counts. |
| Corrections | Clear product facts can reduce avoidable retries and help the model fix validation failures. |

For example, a dining setup for six people under USD 2,000 requires a suitable
table, enough seating, dimensions, and prices. Selection must know whether a chair
price covers one chair or a set. Add these category-specific facts when needed.

Retrieval, selection, and validation must use the same prepared product data.
Changing embedding text alone is unlikely to produce a large improvement.

Measure the result with a small pilot: one category, 20 representative requests,
and the first 10 search results per request. Compare relevant results, missed
suitable products, constraint violations, and selection retries. Count a selection
as successful only when it also satisfies mandatory requested items and quantities.
These are expected benefits; the pilot must establish the actual improvement.

## Product record

This is a fictional example:

```json
{
  "asset_id": "11111111-1111-4111-8111-111111111111",
  "source_table": "catalog.assets",
  "source_id": "22222222-2222-4222-8222-222222222222",
  "title": "Cream boucle sofa",
  "category": "sofa",
  "brand": null,
  "description": "Cream boucle sofa with curved arms and wooden legs, designed for living-room seating.",
  "colors": ["cream"],
  "styles": ["modern"],
  "materials": ["boucle fabric", "wood"],
  "width_m": 2.1,
  "depth_m": 0.9,
  "height_m": 0.8,
  "placement_type": "floor",
  "is_purchasable": true,
  "price": 999.00,
  "currency": "USD",
  "image_url": "https://example.com/sofa.jpg",
  "product_url": "https://example.com/sofa",
  "model_url": null,
  "front_view": null,
  "center": null,
  "topdown_url": null,
  "mount_type": null,
  "features": ["curved arms"]
}
```

## Instructions for the extraction LLM

The importer copies source facts and the LLM supplies descriptive fields. The
stored record includes all fields in the example. Use JSON numbers
for dimensions, price, and `front_view`. Use a numeric array for `center` and
string arrays for colors, styles, materials, and features. Use a boolean for
`is_purchasable` and strings for other populated fields. Use null for unknown
scalar values and empty arrays for unknown lists. Do not include commentary in
the output.

| Field | What the LLM must produce |
|---|---|
| `asset_id` | Copy the ID supplied for this record. The importing system supplies it; do not invent or change it. |
| `source_table`, `source_id` | Copy the raw source table and row ID supplied by the importer. The source table is `catalog.assets` or `pipeline.decor_items`. Do not infer these from the item name. |
| `title` | A short, readable product name. Clean the source title. If absent, create a factual title from the type and supported attributes, such as "Cream boucle sofa." Do not invent a model or collection name. |
| `category` | The actual product type in the [catalog vocabulary](product-categories.md), such as `sofa` or `dining_table`. Correct a raw label when the product text or image clearly establishes another type. Return null if uncertain. |
| `brand` | The explicitly named product brand. Do not assume the retailer is the brand. Return null if absent. |
| `description` | One to three factual sentences describing what the item is, its appearance, and its practical use. Include distinguishing details such as curved arms, drawers, or an extendable top when supported. Write this even if the raw description is missing, using available facts and visible details. Avoid promotional claims, keyword lists, and unsupported capacities or features. |
| `colors` | A deduplicated list of normalized colors for the selected variant, such as `["cream", "black"]`. Extract from text or infer from a clearly visible matching image. Do not include other color options. |
| `styles` | A short list of supported design styles, such as `["modern"]`. Infer from the design when clear. Exclude campaign names and seller tags. Return an empty list when uncertain. |
| `materials` | A deduplicated list of materials supported by the source. From images, use only clear broad observations, such as "fabric." Do not guess fiber composition, wood species, or hidden construction. |
| `width_m`, `depth_m`, `height_m` | Extract labeled product measurements and convert them to metres. Width is left to right, depth is front to back, and height is bottom to top. Exclude packaging measurements. Keep an unknown axis null. Do not estimate scale from an image or typical furniture sizes. |
| `placement_type` | Use `floor`, `surface`, `wall`, or `ceiling` when the intended placement is supported. A shelf ornament uses `surface`; a wall mirror uses `wall`. Do not assume all decor belongs on the floor or decide mirror mounting from thickness alone. Return null if uncertain. The importer sets every `tv` to `surface`, since TVs stand on furniture, and changes any other `surface` item taller than 1 m to `floor`. |
| `is_purchasable` | Use true for an item with a supported purchase offer, false for an explicitly designated design-only asset, and null when unknown. Honor source information supplied by the importer. Category alone does not establish purchase status. |
| `price` | A numeric source price for one item in this variant, without currency symbols. Divide a set price only when the source explicitly states the number of identical items. Return null for an unknown price or unclear bundle. Do not estimate market value. The importer reads the metadata price, then the `catalog.assets.cost` column when metadata has no price. |
| `currency` | The currency code established by the source, such as `USD`. When the source has no currency code, the importer reads a `$` price as `USD`. Otherwise the price and currency stay null. |
| `image_url` | Copy the supplied image reference for the selected variant. Do not invent a URL or substitute an image of a different variant. |
| `product_url` | Copy the supplied product-page URL for the selected product. Return null if absent. |
| `model_url` | Copy a supplied 3D model reference for this exact variant. Return null if absent. The LLM cannot create a model by filling this field. |
| `front_view` | Copy the source front-view value. Keep missing values null. |
| `center` | Copy the model's three finite coordinates `[x, y, z]` without changing axes or units. Catalog records use the legacy row only when its model URL matches the raw source. Decor records use their source center. Invalid or missing values stay null. |
| `topdown_url` | Copy the matching model's top-down preview URL and normalize S3 URLs to HTTPS. Missing values stay null. |
| `mount_type` | Normalize explicit source mounting to `freestanding`, `wall_secured`, `wall_mounted`, or `ceiling_mounted`. Keep unsupported or missing values null. This supplements `placement_type`: a floor cabinet can require wall securing. |
| `features` | A deduplicated, sorted list of short lowercase functional facts supported by the source. Exclude promotional claims and unsupported capacities. Use an empty list when unknown. |

Use one supplied vocabulary for categories and attribute labels. Normalize case
and spelling, remove duplicates, and sort attribute lists consistently.

Keep one catalog record per product page. When several raw rows share a
product URL, they are the same product, so keep the row with the lowest
`asset_id`. A row without a product URL is never merged.

Reuse the prepared `asset_id` when importing the same `source_table` and
`source_id` again. These source fields identify where a record came from; they do
not determine its category or purchase status.

Only make visual inferences from images that were actually provided or read.
If reliable sources disagree about the selected variant, leave the affected field
unknown instead of combining conflicting facts. Omit unknown details from the
description rather than writing guesses or "unknown" into the prose.

For example, if the input gives a sofa image and a width of 210 cm but no
description, brand, or price, the LLM can write a description of visible details,
infer supported colors and style, and set `width_m` to `2.1`. Brand and price stay
null. Depth and height also stay null unless supplied.

Before embedding, require a useful title, correct category, factual description,
and working matching image. The importing system checks links, numeric values,
and required fields after extraction.

## Decor uses the same record

A vase can be purchasable or design-only. Give it the same descriptive fields as
furniture, its actual category, and a supported placement type. For example, a
design-only shelf sculpture uses `category: sculpture`, `placement_type: surface`,
and `is_purchasable: false`.

For non-purchasable decor, keep `price`, `currency`, and `product_url` null. It can
appear in a room design, but not in the shopping list or purchase total. Null
price does not mean free. Missing price alone also does not prove an item is
non-purchasable.

A missing model does not prevent search. Room placement requires a usable model,
checked dimensions, and a known placement type. Surface decor requires supporting
furniture with enough space. Purchase recommendations require
`is_purchasable: true`; unknown price cannot satisfy a strict budget filter.

Save the prepared record, then build its
[embedding text](product-embedding-v2.md).