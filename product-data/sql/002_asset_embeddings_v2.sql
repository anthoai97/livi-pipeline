CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS pipeline.asset_embeddings_v2 (
    asset_id uuid PRIMARY KEY REFERENCES pipeline.pipeline_assets_v2 (asset_id) ON DELETE CASCADE,
    embedding vector(768) NOT NULL,
    embedding_text text NOT NULL,
    image_url text NOT NULL,
    image_sha256 text NOT NULL,
    input_hash text NOT NULL,
    model text NOT NULL,
    dimensions integer NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now()
);

-- Embedding text from docs/data/product-embedding-v2.md. The job embeds this text, and
-- search compares it with the stored text to exclude stale vectors.
CREATE OR REPLACE FUNCTION pipeline.asset_embedding_text_v2(a pipeline.pipeline_assets_v2)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT concat_ws(
        E'\n',
        'Product: ' || a.title,
        'Category: ' || a.category,
        'Brand: ' || a.brand,
        'Description: ' || a.description,
        'Color: ' || nullif(array_to_string(a.colors, ', '), ''),
        'Style: ' || nullif(array_to_string(a.styles, ', '), ''),
        'Materials: ' || nullif(array_to_string(a.materials, ', '), ''),
        'Features: ' || nullif(array_to_string(a.features, ', '), ''),
        'Placement: ' || a.placement_type,
        'Mount: ' || a.mount_type,
        'Dimensions: ' || nullif(concat_ws(
            '; ',
            'width ' || to_char(a.width_m, 'FM999990.000') || ' m',
            'depth ' || to_char(a.depth_m, 'FM999990.000') || ' m',
            'height ' || to_char(a.height_m, 'FM999990.000') || ' m'
        ), '') || '.'
    )
$$;
