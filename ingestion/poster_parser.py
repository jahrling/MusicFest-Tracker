"""
Festival poster parser — shared dataclasses + backend router.

Set VISION_BACKEND in .env:
  claude      → Claude vision API (default, no GPU needed)
  paddleocr   → PaddleOCR running locally (GPU recommended)

When VISION_BACKEND=paddleocr and ANTHROPIC_API_KEY is also set, PaddleOCR
handles text extraction + rank computation while a cheap Claude *text* call
cleans up band identification.  If no API key is present the parser falls
back to regex heuristics only — fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config import VISION_BACKEND


@dataclass
class BandEntry:
    name: str
    rank: float | None = None
    stage: str | None = None
    spotify_id: str = ""  # populated by band_validator when using paddleocr backend
    day: str = ""


@dataclass
class ParsedPoster:
    festival_name: str | None
    location: str | None
    start_date: str | None
    end_date: str | None
    alphabetical: bool
    bands: list[BandEntry] = field(default_factory=list)
    source_url: str = ""
    poster_path: str = ""
    raw_response: str = ""


def parse_poster(source: str | Path, source_url: str = "") -> ParsedPoster:
    """
    Route to the configured vision backend.

    Args:
        source: URL string or local Path to an image/PDF.
        source_url: original URL recorded on the Festival node.
    """
    if VISION_BACKEND == "paddleocr":
        from ingestion.paddleocr_parser import parse_with_paddleocr
        return parse_with_paddleocr(source, source_url=source_url)

    # Default: Claude vision API
    from ingestion.claude_parser import parse_with_claude
    return parse_with_claude(source, source_url=source_url)
