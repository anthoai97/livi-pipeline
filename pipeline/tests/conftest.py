import pytest

from app import variant_stages


@pytest.fixture(autouse=True)
def no_budget_fill(monkeypatch):
    """Recorded selections spend under the budget floor; tests replay them unchanged unless they turn the fill on."""
    monkeypatch.setattr(variant_stages, "BUDGET_FLOOR_PCT", 0.0)
