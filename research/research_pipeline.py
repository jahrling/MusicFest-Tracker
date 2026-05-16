"""
Orchestrates band research for a list of band IDs.
Prioritizes bands below RESEARCH_RANK_THRESHOLD, respects polite scraping delay.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from config import RESEARCH_RANK_THRESHOLD, SCRAPE_DELAY_SECONDS
from graph import queries
from research.band_agent import research_band
from research.concert_finder import persist_shows
from research.bubble_scorer import score_band


def run_research_pipeline(
    band_ids: list[str],
    festival_id: str | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict[str, bool]:
    """
    Research a list of bands and persist results.

    Args:
        band_ids: list of Band node IDs to research.
        festival_id: optional — used to look up their rank for prioritization.
        progress_cb: optional callback(band_name, status) for UI progress.

    Returns:
        dict mapping band_id -> success bool.
    """
    # Build priority order: bands with lower rank first
    ordered = _prioritize(band_ids, festival_id)
    results: dict[str, bool] = {}

    for band_id in ordered:
        band = queries.get_band(band_id)
        if not band:
            results[band_id] = False
            continue

        name = band["name"]
        if progress_cb:
            progress_cb(name, "researching")

        try:
            research = research_band(name)

            # Persist shows
            persist_shows(band_id, research)

            # Compute bubble status from graph data + agent signal
            bubble = score_band(band_id, agent_status=research.bubble_status)

            # Update Band node
            queries.upsert_band(
                name=name,
                genres=json.dumps(research.genres),
                influences=json.dumps(research.influences),
                youtube_links=json.dumps(research.youtube_links),
                notes=research.positioning,
                bubble_status=bubble,
            )

            results[band_id] = True
            if progress_cb:
                progress_cb(name, f"done ({bubble})")

        except Exception as exc:
            results[band_id] = False
            if progress_cb:
                progress_cb(name, f"error: {exc}")

        time.sleep(SCRAPE_DELAY_SECONDS)

    return results


def _prioritize(band_ids: list[str], festival_id: str | None) -> list[str]:
    """Sort band_ids: lower-ranked bands first, then the rest."""
    if not festival_id:
        return band_ids

    timeline = queries.get_full_timeline()
    rank_map: dict[str, float] = {}
    for row in timeline:
        if row.get("f.id") == festival_id:
            bid = row.get("b.id", "")
            rank = row.get("r.rank") or 999
            rank_map[bid] = rank

    below = [b for b in band_ids if rank_map.get(b, 999) <= RESEARCH_RANK_THRESHOLD]
    above = [b for b in band_ids if rank_map.get(b, 999) > RESEARCH_RANK_THRESHOLD]
    return below + above
