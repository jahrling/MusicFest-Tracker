"""
High-level ingestion pipeline:
  parse poster → upsert festival + bands → create graph edges → return summary
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ingestion.poster_parser import ParsedPoster, parse_poster
from graph import queries

_TRAILING_YEAR_RE = re.compile(r"[\s,\-]+\d{4}\s*$")


def _normalize_festival_name(name: str, start_date: str) -> str:
    """Strip a trailing year from the festival name when it matches start_date."""
    year = (start_date or "")[:4]
    if len(year) == 4 and name.endswith(year):
        cleaned = _TRAILING_YEAR_RE.sub("", name).strip()
        return cleaned if cleaned else name
    return name


@dataclass
class IngestionResult:
    festival_id: str
    festival_name: str | None
    bands_added: int
    bands_merged: int
    alphabetical: bool
    band_ids: list[str]
    reimport: bool = False


def ingest_poster(source: str | Path, source_url: str = "") -> IngestionResult:
    """
    Full pipeline: parse → graph upsert.

    Args:
        source: URL or local path to poster image/PDF.
        source_url: original URL to record on the Festival node.

    Returns IngestionResult summary.
    """
    parsed = parse_poster(source, source_url=source_url)
    return ingest_parsed(parsed)


def ingest_parsed(parsed: ParsedPoster) -> IngestionResult:
    """
    Persist a pre-parsed ParsedPoster into the graph.

    Re-uploading a poster for the same festival (same name + date) clears the
    existing band→festival edges and rebuilds them from the new poster, while
    leaving Band nodes and all RATED data intact.
    """
    raw_name = parsed.festival_name or "Unknown Festival"
    festival_name = _normalize_festival_name(raw_name, parsed.start_date or "")

    festival_id = queries.upsert_festival(
        name=festival_name,
        location=parsed.location or "",
        start_date=parsed.start_date or "",
        end_date=parsed.end_date or "",
        source_url=parsed.source_url,
        poster_path=parsed.poster_path,
    )

    # Detect re-import: festival already had bands linked to it
    prior_band_count = queries.count_festival_bands(festival_id)
    is_reimport = prior_band_count > 0

    # Clear existing edges so ranks/lineup reflect the new poster exactly
    if is_reimport:
        queries.clear_festival_edges(festival_id)

    bands_added = 0
    bands_merged = 0
    band_ids: list[str] = []

    for entry in parsed.bands:
        existing = queries.find_band_by_name(entry.name)
        if existing:
            band_id = existing["id"]
            bands_merged += 1
            if entry.spotify_id and not existing.get("spotify_id"):
                queries.upsert_band(entry.name, spotify_id=entry.spotify_id)
        else:
            band_id = queries.upsert_band(entry.name, spotify_id=entry.spotify_id or "")
            bands_added += 1

        band_ids.append(band_id)

        queries.link_band_to_festival(
            band_id=band_id,
            festival_id=festival_id,
            rank=entry.rank,
            alphabetical=parsed.alphabetical,
            rel_type="PLAYED_AT",
            day=entry.day or "",
        )

    return IngestionResult(
        festival_id=festival_id,
        festival_name=festival_name,
        bands_added=bands_added,
        bands_merged=bands_merged,
        alphabetical=parsed.alphabetical,
        band_ids=band_ids,
        reimport=is_reimport,
    )
