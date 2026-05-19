"""IIIF settings — configurable via environment variables."""

from __future__ import annotations

import os

IIIF_URL = os.environ.get("IIIF_URL", "https://iiifintern-ai.ra.se")
IIIF_TIMEOUT = float(os.environ.get("IIIF_TIMEOUT", "60"))
IIIF_QUERY_PARAMS = os.environ.get("IIIF_QUERY_PARAMS", "full/max/0/default.jpg")
