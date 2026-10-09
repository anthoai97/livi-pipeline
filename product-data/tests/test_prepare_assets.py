"""Offline checks for source metadata preservation during preparation."""

import json
from decimal import Decimal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import prepare_assets


@pytest.fixture
def source_row(monkeypatch):
    monkeypatch.setattr(prepare_assets, "live_url", lambda url, strict=True: url)
    return {
        "source_id": "7f71c938-e386-4d58-9c93-97f34484085d",
        "name": "Storage cabinet",
        "category": "cabinet",
        "width_m": "1.2",
        "depth_m": "0.4",
        "height_m": "0.9",
        "center": [-0.1, 0, 0.45],
        "mount": "wall-secured",
        "topdown_url": "https://example.com/cabinet-top.png",
        "retailer": "Example Home",
    }


@pytest.mark.parametrize(
    ("raw_brand", "extracted_brand", "expected"),
    [
        ("EXAMPLE HOME", "Example Home", "Example Home"),
        (None, "Example Home", "Example Home"),
        ("Cabinet Maker", "unknown", None),
        ("Cabinet Maker", "Cabinet Maker", "Cabinet Maker"),
    ],
)
def test_extracted_brand_is_not_discarded_when_it_matches_retailer(
    source_row, raw_brand, extracted_brand, expected
):
    source_row["brand"] = raw_brand
    record = prepare_assets.source_record(source_row, "catalog.assets")
    prepared = prepare_assets.apply_llm(record, {"brand": extracted_brand}, {"cabinet"})
    assert prepared["brand"] == expected


@pytest.mark.parametrize(
    "center",
    [None, [], [0, 1], [0, 1, 2, 3], ["0", 1, 2], [True, 1, 2],
     [None, 1, 2], [float("nan"), 1, 2], [0, float("inf"), 2],
     [0, 1, float("-inf")], {"x": 0, "y": 1, "z": 2}],
)
def test_invalid_center_is_unknown(source_row, center):
    source_row["center"] = center
    record = prepare_assets.source_record(source_row, "catalog.assets")
    assert record["center"] is None


def test_extraction_receives_richer_source_and_preserves_measured_facts(source_row, monkeypatch):
    source_row.update({
        "brand": "Cabinet Maker",
        "detailed_materials": ["solid oak", "brass hardware"],
        "materials_explained": "Solid oak frame with brass handles.",
        "features": ["Soft-close drawers", "Adjustable shelves"],
        "product_details": {"assembly": "Required"},
    })

    def extract(client, text, usage, *, step, record):
        assert step == "extract"
        payload = json.loads(text.split("Source:\n", 1)[1])
        assert payload["brand_hint"] == "Cabinet Maker"
        for field in ("detailed_materials", "materials_explained", "features", "product_details"):
            assert payload[field] == source_row[field]
        return {
            "title": "Oak storage cabinet",
            "category": "cabinet",
            "brand": "Cabinet Maker",
            "description": "Oak cabinet with brass handles and adjustable shelves.",
            "colors": ["brown"],
            "styles": ["modern"],
            "materials": ["wood", "metal"],
            "placement_type": "floor",
            "features": [" Soft-close drawers ", "ADJUSTABLE   SHELVES", "soft-close drawers", "unknown", ""],
            "center": [99, 99, 99],
            "mount_type": "ceiling_mounted",
            "topdown_url": "https://example.com/invented.png",
        }

    monkeypatch.setattr(prepare_assets, "call_json", extract)
    prepared = prepare_assets.extract_record(None, source_row, "catalog.assets", {"cabinet"}, None)

    assert prepared["features"] == ["adjustable shelves", "soft-close drawers"]
    assert prepared["center"] == [-0.1, 0, 0.45]
    assert prepared["mount_type"] == "wall_secured"
    assert prepared["topdown_url"] == source_row["topdown_url"]
    assert prepared["llm_used"] is True


def test_failed_extraction_keeps_features_unknown_and_geometry_intact(source_row, monkeypatch):
    source_row["features"] = ["Best cabinet ever"]

    def fail(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(prepare_assets, "call_json", fail)
    prepared = prepare_assets.extract_record(None, source_row, "catalog.assets", {"cabinet"}, None)

    assert prepared["features"] == []
    assert prepared["center"] == [-0.1, 0, 0.45]
    assert prepared["mount_type"] == "wall_secured"
    assert prepared["llm_used"] is False


def test_refresh_preserves_failed_record_saves_success_and_exits_nonzero(monkeypatch, capsys):
    failed = {
        "source_table": "catalog.assets", "source_id": "failed-product",
        "title": "Fallback title", "category": "cabinet", "llm_used": False,
    }
    successful = {
        "source_table": "catalog.assets", "source_id": "successful-product",
        "title": "Updated cabinet", "category": "cabinet", "llm_used": True,
    }
    connection = MagicMock()
    connection.__enter__.return_value = connection
    upsert = Mock()
    usage = Mock(log_path="unused.jsonl", summary_path="unused.json")
    usage.summary.return_value = {"totals": {
        "calls": 2, "input_tokens": 0, "output_tokens": 0,
        "thoughts_tokens": 0, "cost_usd": 0,
    }}
    monkeypatch.setattr(sys, "argv", [
        "prepare_assets.py", "--refresh", "--assets", "2", "--decor", "0", "--workers", "1",
    ])
    monkeypatch.setenv("REMOTE_CONNECTION_STRING", "unused-remote")
    monkeypatch.setenv("LOCAL_CONNECTION_STRING", "unused-local")
    monkeypatch.setattr(prepare_assets, "load_dotenv", Mock())
    monkeypatch.setattr(prepare_assets.psycopg, "connect", Mock(return_value=connection))
    monkeypatch.setattr(prepare_assets, "ProxyClient", Mock(return_value=SimpleNamespace(model="test")))
    monkeypatch.setattr(prepare_assets, "GeminiUsage", Mock(return_value=usage))
    monkeypatch.setattr(prepare_assets, "ensure_local_schema", Mock())
    monkeypatch.setattr(prepare_assets, "load_categories", Mock(return_value={"cabinet"}))
    monkeypatch.setattr(prepare_assets, "fetch_assets", Mock(return_value=[failed, successful]))
    monkeypatch.setattr(prepare_assets, "fetch_decor", Mock(return_value=[]))
    monkeypatch.setattr(prepare_assets, "extract_record", lambda client, row, *args: row)
    monkeypatch.setattr(prepare_assets, "upsert_records", upsert)
    monkeypatch.setattr(prepare_assets, "summarize", Mock())

    with pytest.raises(SystemExit) as exc:
        prepare_assets.main()

    assert exc.value.code == 1
    upsert.assert_called_once_with(connection, [successful])
    assert "failed-product: existing record preserved" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mount", "placement", "stored_mount"),
    [("freestanding", "wall", None), ("wall_secured", "floor", "wall_secured")],
)
def test_upsert_drops_contradictory_mount_and_keeps_wall_secured_floor_items(
    source_row, mount, placement, stored_mount
):
    source_row["mount"] = mount
    record = prepare_assets.source_record(source_row, "catalog.assets")
    prepared = prepare_assets.apply_llm(record, {"placement_type": placement}, {"cabinet"})
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value

    prepare_assets.upsert_records(connection, [prepared])

    cursor.execute.assert_called_once()
    parameters = cursor.execute.call_args.args[1]
    assert parameters["mount_type"] == stored_mount
    assert parameters["placement_type"] == placement
    connection.commit.assert_called_once()


def test_price_falls_back_to_cost_when_metadata_price_is_missing(source_row):
    source_row.update({"name": "24-inch HD 720p LED Smart TV", "price": None, "currency": None, "cost": "$278.00"})
    record = prepare_assets.source_record(source_row, "catalog.assets")
    assert record["price"] == Decimal("278.00")
    assert record["currency"] == "USD"
    assert record["is_purchasable"] is True


@pytest.mark.parametrize(
    ("category", "height", "expected"),
    [("planter", "1.66", "floor"), ("tv", "1.16", "surface"), ("vase", "0.4", "surface")],
)
def test_tall_surface_item_moves_to_floor_except_tv(source_row, monkeypatch, category, height, expected):
    source_row.update({"category": category, "height_m": height, "mount": None})
    monkeypatch.setattr(prepare_assets, "call_json", lambda *args, **kwargs: {
        "title": "Item", "category": category, "brand": None, "description": "An item.",
        "colors": ["green"], "styles": ["modern"], "materials": ["ceramic"],
        "features": [], "placement_type": "surface",
    })
    prepared = prepare_assets.extract_record(None, source_row, "catalog.assets", {category}, None)
    assert prepared["placement_type"] == expected
