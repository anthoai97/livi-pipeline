# Product preparation v2: run report

Run on October 8, 2026, with `product-data/src/prepare_assets.py` and model `gpt-6-luna`.
The local table `pipeline.pipeline_assets_v2` holds **5,240 products**: 4,920
catalog products and 320 decor items. This is every product that passes the
current conditions.

The records come only from the raw sources `catalog.assets` and
`pipeline.decor_items`. Nothing is read from the legacy `pipeline.pipeline_assets`.

## Source conditions

A row is selected only when it passes every condition, in this order.

### `catalog.assets`

| Condition | Live rows left | Deleted rows (not used) |
|---|---:|---:|
| All rows | 36,765 | 8,379 |
| `is_deleted` is false | 36,765 | — |
| `model_url` is not empty | 4,995 | 7,493 |
| `link_status` is not `dead` | 4,925 | 7,491 |
| `metadata.assetMetadata.boundingBox` has `x`, `y`, `z` above 0 | **4,920** | 7,394 |

- **Metadata stored as a string:** 8,311 live rows store `metadata` as a JSON
  string instead of a JSON object. The query decodes it first. Without this,
  2,299 products with valid dimensions were wrongly excluded.
- **Dimensions:** the bounding box gives the dimensions in metres. `x` is width,
  `y` is depth, and `z` is height. A comparison with the 3D model size measured
  by the old pipeline matched all three axes for 91% of products.
- **Image:** an image URL is not required. 12 prepared products have no image.

### `pipeline.decor_items`

| Condition | Rows left |
|---|---:|
| All rows | 367 |
| `is_deleted` is false | 320 |
| `glb_url` is not empty | 320 |
| `width`, `depth`, `height` all above 0 | **320** |

### Checks during preparation

- **Dimensions:** a row without all three dimensions is skipped. No rows were skipped.
- **Links:** files on the `livinit-storage-prod` S3 bucket are trusted. Other
  image and model URLs must respond, or they are set to null. A product page is
  set to null only on 404, 410, or a network failure, because retailers often
  block automated checks.
- **Images sent to the LLM:** AVIF images are stored but not sent, because the
  model rejects them. Those 20 products are prepared from text only.
- **Price:** parsed from text such as `$1,299.00`. A price is kept only with a
  three-letter currency code. "Set of 4" and "4-pack" prices are divided into
  a single-item price. Other bundles ("set", "2 piece") have their price removed.
- **Purchase status:** catalog products are purchasable when they have a price
  or a product link. Decor items are design-only.
- **Attributes:** colors, styles, and materials must come from fixed lists in
  the script (27 colors, 17 styles, 28 materials).

## Results

| Field | Catalog (4,920) | Decor (320) |
|---|---:|---:|
| Category | 4,919 | 319 |
| Placement | 4,914 | 314 |
| Colors | 4,916 | 320 |
| Styles | 4,566 | 155 |
| Materials | 4,863 | 227 |
| Brand | 379 | 1 |
| Price and currency | 4,614 | 0 |
| Purchasable | 4,919 | 0 |
| Image | 4,909 | 320 |
| 3D model | 4,920 | 320 |
| Product link | 4,532 | 0 |
| All three dimensions | 4,920 | 320 |
| Ready to embed (title, category, description, image) | 4,908 | 319 |

- **Categories:** 180 in use. The largest are coffee_table (628), accent_chair
  (426), dining_chair (380), dining_table (224), and sofa (193).
- **Placement:** floor 4,614, surface 447, wall 124, ceiling 43, unknown 12.
- **Prices:** all USD, from 1.49 to 40,498.20, with a median of 1,000.

### LLM usage

| Item | Value |
|---|---:|
| Rows | 5,240 |
| LLM calls | 6,515 |
| Rows with a second call (fields filled) | 1,275 (762) |
| Input tokens | 55.1M |
| Output tokens | 0.71M |
| Fallback rows (no LLM) | 0 |
| Errors | 0 |

Calls go through the Codex Pro subscription, so there is no per-token charge.

## Why the total stops at 5,240

The 3D-model condition is the limit. Of 36,765 live catalog products, only 4,995
have a model.

| Change | Extra products |
|---|---:|
| Include deleted rows (most last updated on May 9, 2026, in a bulk change) | 7,394 |
| Allow live products without a 3D model (searchable and purchasable, but not placeable in a room) | 29,813 |
| Include links marked `dead` | 70 |

Five live products have a model but no dimensions. They stay out either way.

## Open questions

1. Why were the deleted rows with models removed, and are any safe to restore?
2. Should products without a 3D model be prepared for search only?
3. Accessories such as IKEA armchair and sofa covers are in the catalog with
   floor placement. Should they be excluded from room design?
