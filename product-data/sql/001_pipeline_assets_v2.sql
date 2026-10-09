CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE IF NOT EXISTS pipeline.pipeline_assets_v2 (
    asset_id uuid CONSTRAINT pipeline_assets_v2_asset_id_pkey PRIMARY KEY,
    source_table text NOT NULL CHECK (source_table IN ('catalog.assets', 'pipeline.decor_items')),
    source_id uuid NOT NULL,
    title text,
    category text,
    brand text,
    description text,
    colors text[] NOT NULL DEFAULT '{}',
    styles text[] NOT NULL DEFAULT '{}',
    materials text[] NOT NULL DEFAULT '{}',
    width_m double precision CHECK (width_m IS NULL OR width_m > 0),
    depth_m double precision CHECK (depth_m IS NULL OR depth_m > 0),
    height_m double precision CHECK (height_m IS NULL OR height_m > 0),
    front_view smallint,
    center jsonb,
    topdown_url text,
    mount_type text CHECK (mount_type IN ('freestanding', 'wall_secured', 'wall_mounted', 'ceiling_mounted')),
    features text[] NOT NULL DEFAULT '{}',
    placement_type text CHECK (
        placement_type IS NULL
        OR placement_type IN ('floor', 'surface', 'wall', 'ceiling')
    ),
    is_purchasable boolean,
    price numeric(12, 2) CHECK (price IS NULL OR price > 0),
    currency text,
    image_url text,
    product_url text,
    model_url text,
    prepared_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS pipeline_assets_v2_category_key_idx
    ON pipeline.pipeline_assets_v2 (category);
