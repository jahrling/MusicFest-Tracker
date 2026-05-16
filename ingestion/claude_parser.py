"""
Claude vision backend — sends the poster image to claude-sonnet-4-20250514
and receives structured JSON containing the lineup + rank data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from ingestion.image_utils import prepare_poster
from ingestion.poster_parser import BandEntry, ParsedPoster


EXTRACTION_PROMPT = """You are analyzing a music festival poster image.

STEP 1 — Detect if the lineup is organized by day.
Look for day headings like "Friday / Saturday / Sunday", "Day 1 / Day 2 / Day 3",
or date labels like "Sept 19 / Sept 20". Multi-day posters often have columns or
sections, each with its own heading and artist list beneath or beside it.

STEP 2 — Extract the lineup using the appropriate ranking method:

  A) If the poster IS organized by day:
     - Assign each band the "day" label of their section (lowercase, e.g. "friday", "saturday", "day 1").
     - Rank each band 1–100 WITHIN their day only:
         100 = headliner of that day (largest text, top billing in that day's column/section)
           1 = smallest/lowest act in that day's column/section
       Bands from different days are NOT compared to each other.
     - Set "alphabetical": false.
     - Set "day" on every band.

  B) If the poster is NOT organized by day (single combined lineup):
     - Detect alphabetical (A-Z) order: if true, set "alphabetical": true and rank = null for all.
     - If not alphabetical: rank 1–100 across the full poster (headliner = 100).
     - Leave "day": null for all bands.

STEP 3 — Return ONLY valid JSON, no markdown, no explanation:
{
  "festival_name": "string or null",
  "location": "string or null",
  "start_date": "YYYY-MM-DD or null",
  "end_date": "YYYY-MM-DD or null",
  "alphabetical": true/false,
  "bands": [
    {
      "name": "string",
      "rank": number or null,
      "day": "string or null",
      "stage": "string or null"
    }
  ]
}

Rules:
- Include every band/artist/DJ visible, even on small text.
- Clean names: trim whitespace, convert ALL-CAPS to Title Case where appropriate.
- Use null for unknown festival_name, location, or dates.
- Normalize "day" values to lowercase (e.g. "friday" not "FRIDAY", "day 1" not "DAY ONE").
"""


def parse_with_claude(source: str | Path, source_url: str = "") -> ParsedPoster:
    """Parse a festival poster using Claude's vision API."""
    b64_data, media_type, local_path = prepare_poster(source)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if media_type == "application/pdf":
        content: list[Any] = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64_data,
                },
            },
            {"type": "text", "text": EXTRACTION_PROMPT},
        ]
    else:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data,
                },
            },
            {"type": "text", "text": EXTRACTION_PROMPT},
        ]

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )

    raw = message.content[0].text.strip()
    data = _parse_json(raw)

    bands = [
        BandEntry(
            name=b["name"],
            rank=b.get("rank"),
            stage=b.get("stage") or "",
            day=(b.get("day") or "").lower().strip(),
        )
        for b in data.get("bands", [])
        if b.get("name")
    ]

    return ParsedPoster(
        festival_name=data.get("festival_name"),
        location=data.get("location"),
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        alphabetical=bool(data.get("alphabetical", False)),
        bands=bands,
        source_url=source_url or (str(source) if str(source).startswith("http") else ""),
        poster_path=str(local_path),
        raw_response=raw,
    )


def _parse_json(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude returned non-JSON:\n{raw}") from exc
