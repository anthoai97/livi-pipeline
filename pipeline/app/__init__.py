"""LangGraph generation runtime.

Adds product-data/src to the import path so `search_assets` and `gemini_usage`
import as top-level modules, as product-data/src/prepare_assets.py does.
"""

import sys
from pathlib import Path

PRODUCT_DATA_SRC = Path(__file__).resolve().parents[2] / "product-data" / "src"
if str(PRODUCT_DATA_SRC) not in sys.path:
    sys.path.insert(0, str(PRODUCT_DATA_SRC))
