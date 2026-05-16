"""
Band research agent — uses Claude with web search to gather:
  - Positioning blurb (genre, vibe, sounds-like)
  - Musical influences
  - YouTube links
  - Upcoming local shows & notable festivals
  - Bubble status
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, HOME_CITY, CONCERT_RADIUS_MILES


RESEARCH_SYSTEM = f"""You are a music journalist and data researcher.
Your home city for local concert research is: {HOME_CITY} (radius: {CONCERT_RADIUS_MILES} miles).
Today's date context: use web search for current information.
Always return ONLY valid JSON with no markdown fences."""


RESEARCH_PROMPT = """Research the band/artist: {band_name}

Return a JSON object with this exact schema:
{{
  "genres": ["string"],
  "positioning": "2-3 sentence blurb: genre, vibe, sounds like whom",
  "influences": ["artist name"],
  "youtube_links": ["url"],
  "upcoming_local_shows": [
    {{"venue": "string", "city": "string", "date": "YYYY-MM-DD", "festival": false}}
  ],
  "upcoming_festivals": [
    {{"name": "string", "location": "string", "date": "YYYY-MM-DD"}}
  ],
  "bubble_status": "hot|bubbling|stagnant|declining",
  "bubble_reasoning": "one sentence explanation"
}}

Research steps:
1. Search for the band's official website, Bandcamp bio, AllMusic page, and recent interviews.
2. Look for member quotes about their influences.
3. Find official YouTube videos (prefer official channel, then live performances).
4. Search for upcoming shows within {radius} miles of {city} and at notable festivals.
5. Assess bubble_status based on:
   - Frequency of upcoming shows (more = hotter)
   - Festival booking tier trends
   - Any recent press or streaming trajectory mentions

If you cannot find reliable data for a field, use an empty array [] or empty string "".
"""


@dataclass
class BandResearch:
    band_name: str
    genres: list[str]
    positioning: str
    influences: list[str]
    youtube_links: list[str]
    upcoming_local_shows: list[dict]
    upcoming_festivals: list[dict]
    bubble_status: str
    bubble_reasoning: str
    raw_response: str = ""


def research_band(band_name: str) -> BandResearch:
    """Run the research agent for a single band."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = RESEARCH_PROMPT.format(
        band_name=band_name,
        radius=CONCERT_RADIUS_MILES,
        city=HOME_CITY,
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=RESEARCH_SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract the final text block (after any tool use)
    raw = ""
    for block in message.content:
        if block.type == "text":
            raw = block.text.strip()

    data = _safe_parse(raw)

    return BandResearch(
        band_name=band_name,
        genres=data.get("genres", []),
        positioning=data.get("positioning", ""),
        influences=data.get("influences", []),
        youtube_links=data.get("youtube_links", []),
        upcoming_local_shows=data.get("upcoming_local_shows", []),
        upcoming_festivals=data.get("upcoming_festivals", []),
        bubble_status=data.get("bubble_status", "stagnant"),
        bubble_reasoning=data.get("bubble_reasoning", ""),
        raw_response=raw,
    )


def _safe_parse(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}
