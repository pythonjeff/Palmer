"""Shared SerpAPI transport.

shopping.py (google_shopping engine) and amazon.py (amazon engine) each carried
their own copy of the key, base URL, timeout and request scaffolding. Only the
transport is shared here — the two engines return genuinely different payload
shapes, so each module still does its own parsing. Merging that part too would
be worse than the duplication it removed.
"""
from __future__ import annotations

import os
import urllib.parse

from agent import _http_get_json

API_KEY = os.environ.get("SERP_API_KEY", "")
BASE = "https://serpapi.com/search.json"
TIMEOUT = 12


def search(params: dict) -> dict | None:
    """Run a SerpAPI query. Returns the raw payload, or None if the key is
    missing or the request fails — callers treat None as 'no results'."""
    if not API_KEY:
        return None
    query = dict(params)
    query["api_key"] = API_KEY
    return _http_get_json(f"{BASE}?{urllib.parse.urlencode(query)}", timeout=TIMEOUT)
