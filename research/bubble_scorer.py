"""
Bubble scorer — derives hot/bubbling/stagnant/declining from graph data.

Uses:
  1. Rank trend across festivals over time (ascending = hot)
  2. Count of upcoming shows
  3. Research agent's own assessment (as a signal)
"""

from __future__ import annotations

from graph import queries


def score_band(band_id: str, agent_status: str = "") -> str:
    """Return bubble_status string for a band based on graph evidence."""
    history = queries.get_band_festival_history(band_id)
    # Filter out alphabetical entries (rank=-1 sentinel)
    ranked = [h for h in history if not h.get("r.alphabetical") and (h.get("r.rank") or 0) > 0]

    if len(ranked) < 2:
        # Not enough data — defer to agent assessment or default
        return agent_status or "stagnant"

    # Sort by date
    ranked.sort(key=lambda x: x.get("f.start_date") or "")
    ranks = [r["r.rank"] for r in ranked]

    # Simple linear slope
    n = len(ranks)
    x_mean = (n - 1) / 2
    y_mean = sum(ranks) / n
    numerator = sum((i - x_mean) * (ranks[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n)) or 1
    slope = numerator / denominator

    if slope > 5:
        return "hot"
    elif slope > 1:
        return "bubbling"
    elif slope < -5:
        return "declining"
    else:
        return "stagnant"
