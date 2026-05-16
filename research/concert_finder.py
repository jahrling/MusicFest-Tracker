"""
Concert finder — pulls upcoming shows from BandResearch and persists them
into the graph as Concert nodes + PERFORMED_AT edges.
"""

from __future__ import annotations

from research.band_agent import BandResearch
from graph import queries


def persist_shows(band_id: str, research: BandResearch) -> int:
    """Write upcoming local shows to the graph. Returns count added."""
    count = 0
    for show in research.upcoming_local_shows:
        venue = show.get("venue", "Unknown Venue")
        city = show.get("city", "")
        date = show.get("date", "")
        if not date:
            continue
        concert_id = queries.upsert_concert(venue=venue, city=city, date=date)
        queries.link_band_to_concert(band_id=band_id, concert_id=concert_id, date=date)
        count += 1
    return count
