"""Embed prepared products into local pipeline.asset_embeddings_v2.

Reads pipeline.pipeline_assets_v2 from LOCAL_CONNECTION_STRING. Each product with
a title, category, description, and image gets one Gemini vector built from its
embedding text and image together. The text comes from the SQL function
pipeline.asset_embedding_text_v2, so search can detect stale vectors.

Images are cached under product-data/.data/images. A rerun revalidates each
cached image with a conditional request, hashes the text, image bytes, and model
settings, and skips products whose hash is unchanged. Failures are reported and
retried on the next run. Price changes need no new vector.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "sql" / "002_asset_embeddings_v2.sql"
IMAGE_CACHE = ROOT / ".data" / "images"
REPORT_PATH = ROOT / "logs" / "embed-assets-report.json"
MODEL = "gemini-embedding-2"
DIMENSIONS = 768
REQUIRED_FIELDS = ("title", "category", "description", "image_url")
USER_AGENT = "Mozilla/5.0 (compatible; livinit-pipeline/1.0)"


def gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required")
    return genai.Client(api_key=api_key)


def embed(client: genai.Client, contents: list) -> list[float]:
    """Return one vector for all parts together, with bounded retries."""
    last_error = None
    for attempt in range(4):
        try:
            result = client.models.embed_content(
                model=MODEL,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS),
            )
            embeddings = result.embeddings or []
            values = embeddings[0].values if len(embeddings) == 1 else None
            if not values or len(values) != DIMENSIONS:
                raise RuntimeError(f"expected one {DIMENSIONS}-dimension embedding")
            return values
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"embedding failed: {last_error}")


def fetch_image(url: str) -> bytes:
    """Return image bytes from the cache, revalidated with ETag or Last-Modified."""
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    path = IMAGE_CACHE / key
    meta_path = IMAGE_CACHE / f"{key}.json"
    headers = {"User-Agent": USER_AGENT}
    if path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                data = response.read()
                meta = {
                    "url": url,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
            # Products can share an image, so write through unique temp files.
            for target, content in ((path, data), (meta_path, json.dumps(meta).encode())):
                temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
                temp.write_bytes(content)
                temp.replace(target)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return path.read_bytes()
            if exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"image HTTP {exc.code}") from exc
            last_error = exc
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"image download failed: {last_error}")


def image_part(data: bytes) -> types.Part:
    """Gemini accepts PNG and JPEG. Convert other formats to JPEG on white."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.format in {"PNG", "JPEG"}:
            return types.Part.from_bytes(data=data, mime_type=f"image/{image.format.lower()}")
        rgba = image.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, "white")
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=90)
    return types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg")


def embed_product(client: genai.Client, row: dict, stored: dict | None) -> dict:
    result = {key: row[key] for key in ("asset_id", "source_table", "source_id", "image_url", "embedding_text")}
    try:
        data = fetch_image(row["image_url"])
        result["image_sha256"] = hashlib.sha256(data).hexdigest()
        result["input_hash"] = hashlib.sha256(
            json.dumps([MODEL, DIMENSIONS, row["embedding_text"], result["image_sha256"]]).encode()
        ).hexdigest()
        if stored and stored["input_hash"] == result["input_hash"] and stored["image_url"] == row["image_url"]:
            return {**result, "status": "skipped"}
        result["embedding"] = embed(client, [row["embedding_text"], image_part(data)])
    except Exception as exc:
        return {**result, "status": "failed", "reason": str(exc)[:300]}
    return {**result, "status": "embedded"}


def save_embedding(connection: psycopg.Connection, item: dict) -> None:
    connection.execute(
        """
        INSERT INTO pipeline.asset_embeddings_v2 (
            asset_id, embedding, embedding_text, image_url, image_sha256,
            input_hash, model, dimensions
        )
        VALUES (
            %(asset_id)s, %(embedding)s::vector, %(embedding_text)s, %(image_url)s,
            %(image_sha256)s, %(input_hash)s, %(model)s, %(dimensions)s
        )
        ON CONFLICT (asset_id) DO UPDATE SET
            embedding = EXCLUDED.embedding,
            embedding_text = EXCLUDED.embedding_text,
            image_url = EXCLUDED.image_url,
            image_sha256 = EXCLUDED.image_sha256,
            input_hash = EXCLUDED.input_hash,
            model = EXCLUDED.model,
            dimensions = EXCLUDED.dimensions,
            embedded_at = now()
        """,
        {**item, "model": MODEL, "dimensions": DIMENSIONS},
    )
    connection.commit()


def main() -> None:
    load_dotenv(ROOT.parent / ".env")
    parser = argparse.ArgumentParser(description="Embed prepared products into pipeline.asset_embeddings_v2")
    parser.add_argument("--assets", type=int, help="limit to the first N eligible catalog.assets products")
    parser.add_argument("--decor", type=int, help="limit to the first N eligible pipeline.decor_items products")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if (args.assets or 0) < 0 or (args.decor or 0) < 0 or args.workers < 1:
        raise SystemExit("limits must be non-negative and workers positive")

    local_dsn = os.environ.get("LOCAL_CONNECTION_STRING", "").strip()
    if not local_dsn:
        raise SystemExit("LOCAL_CONNECTION_STRING is required")
    client = gemini_client()
    started = time.perf_counter()

    with psycopg.connect(local_dsn, row_factory=dict_row) as connection:
        connection.execute(SCHEMA_PATH.read_text())
        connection.commit()
        rows = connection.execute(
            """
            SELECT asset_id, source_table, source_id, title, category, description,
                   image_url, pipeline.asset_embedding_text_v2(a) AS embedding_text
            FROM pipeline.pipeline_assets_v2 a
            ORDER BY source_table, asset_id
            """
        ).fetchall()
        stored = {
            row["asset_id"]: row
            for row in connection.execute(
                "SELECT asset_id, image_url, input_hash FROM pipeline.asset_embeddings_v2 WHERE model = %s",
                (MODEL,),
            )
        }

        ineligible = []
        eligible: dict[str, list[dict]] = {"catalog.assets": [], "pipeline.decor_items": []}
        for row in rows:
            missing = [field for field in REQUIRED_FIELDS if not (row[field] or "").strip()]
            if missing:
                ineligible.append({
                    "asset_id": str(row["asset_id"]),
                    "source_table": row["source_table"],
                    "source_id": str(row["source_id"]),
                    "reason": "missing " + ",".join(missing),
                })
            else:
                eligible[row["source_table"]].append(row)
        selected = eligible["catalog.assets"][:args.assets] + eligible["pipeline.decor_items"][:args.decor]
        connection.execute(
            "DELETE FROM pipeline.asset_embeddings_v2 WHERE asset_id = ANY(%s::uuid[]) OR model <> %s",
            ([item["asset_id"] for item in ineligible], MODEL),
        )
        connection.commit()

        counts = {"embedded": 0, "skipped": 0, "failed": 0}
        failed = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(embed_product, client, row, stored.get(row["asset_id"])) for row in selected]
            for index, future in enumerate(as_completed(futures), start=1):
                item = future.result()
                counts[item["status"]] += 1
                if item["status"] == "embedded":
                    save_embedding(connection, item)
                elif item["status"] == "failed":
                    previous = stored.get(item["asset_id"])
                    # Search detects changed text or URLs, but not new image bytes.
                    if previous and item.get("input_hash") and previous["input_hash"] != item["input_hash"]:
                        connection.execute("DELETE FROM pipeline.asset_embeddings_v2 WHERE asset_id = %s", (item["asset_id"],))
                        connection.commit()
                    failed.append({
                        "asset_id": str(item["asset_id"]),
                        "source_table": item["source_table"],
                        "source_id": str(item["source_id"]),
                        "reason": item["reason"],
                    })
                    print(f"failed {item['source_table']} {item['asset_id']}: {item['reason']}", file=sys.stderr)
                if item["status"] != "skipped" or index % 500 == 0 or index == len(futures):
                    print(f"[{index}/{len(futures)}] {item['status']} {item['source_table']} {item['asset_id']}")

        current = connection.execute(
            """
            SELECT count(*) AS current
            FROM pipeline.asset_embeddings_v2 e
            JOIN pipeline.pipeline_assets_v2 a USING (asset_id)
            WHERE e.embedding_text = pipeline.asset_embedding_text_v2(a)
              AND e.image_url = a.image_url
            """
        ).fetchone()["current"]

    elapsed = round(time.perf_counter() - started, 1)
    report = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "selected": len(selected),
        **counts,
        "ineligible": len(ineligible),
        "current_embeddings": current,
        "elapsed_seconds": elapsed,
        "failures": sorted(failed, key=lambda item: item["asset_id"]),
        "ineligible_products": ineligible,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"model={MODEL} dimensions={DIMENSIONS} selected={len(selected)}"
        f" embedded={counts['embedded']} skipped={counts['skipped']} failed={counts['failed']}"
        f" ineligible={len(ineligible)} current_embeddings={current}"
        f" elapsed={elapsed}s report={REPORT_PATH}"
    )


if __name__ == "__main__":
    main()
