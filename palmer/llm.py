"""Shared Anthropic client and model routing.

SONNET_MODEL is for anything the user reads; HAIKU_MODEL is for extraction,
scoring and classification. See CLAUDE.md "Model routing".
"""
import json
import os

import anthropic


HAIKU_MODEL = "claude-haiku-4-5-20251001"

SONNET_MODEL = "claude-sonnet-4-6"

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=45.0)

def _parse_json(text: str) -> dict | list | None:
    """Extract and parse the first JSON object or array from a string."""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch) + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None
