"""
MusicFestivalWizard scraper — finds festivals where your ranked/liked artists appear.

Requires Playwright:
    pip install playwright
    playwright install chromium

URL patterns:
    Artist page  : https://www.musicfestivalwizard.com/artist/{slug}/
    Festival page: https://www.musicfestivalwizard.com/festivals/{slug}-{year}/

Flow (per artist):
    1. slugify artist name  →  /artist/{slug}/
    2. Parse festival links from the page (upcoming + past sections)
    3. For each festival URL not yet seen, fetch the festival page
    4. Extract poster img src + lineup band list
    5. Score the festival: sum(band score or 5 for liked-only) for all matching artists

Scoring formula:
    festival_score  = Σ (score if rated, else 5 for liked-only) for each matching artist
    max_possible    = Σ scores across the full search pool
    pct             = festival_score / max_possible * 100  (capped at 100)
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field


class MfwNotAvailable(RuntimeError):
    """Raised when Playwright is not installed."""


@dataclass
class MfwFestivalResult:
    festival_name: str
    festival_url: str
    year: str
    dates: str
    poster_url: str | None
    score: int                          # raw weighted sum of matching band scores
    pct: int                            # score as % of max possible (0–100)
    matching_bands: list[dict]          # [{name, score, liked, source}]
    total_lineup_bands: int = 0


def _slugify(name: str) -> str:
    """Convert an artist name to the URL slug MFW uses."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^\w\s-]", "", ascii_name).strip().lower()
    return re.sub(r"[\s_-]+", "-", slug)


def _max_score(search_pool: list[dict]) -> int:
    return sum(b.get("score") or 5 for b in search_pool)


async def _fetch_artist_festival_links(page, artist_slug: str) -> list[str]:
    """Return all /festivals/… URLs listed on the artist page."""
    url = f"https://www.musicfestivalwizard.com/artist/{artist_slug}/"
    await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    links = await page.eval_on_selector_all(
        "a[href*='/festivals/']",
        "els => els.map(e => e.href)",
    )
    return list(dict.fromkeys(links))  # deduplicate, preserve order


async def _fetch_festival_details(page, festival_url: str) -> dict:
    """Return {name, year, dates, poster_url, lineup_names} for one festival page."""
    await page.goto(festival_url, wait_until="domcontentloaded", timeout=20_000)

    # Festival name from <h1>
    name = await page.eval_on_selector("h1", "el => el.innerText.trim()") or festival_url

    # Date string — MFW usually has it in a .festival-dates or similar span
    dates = ""
    try:
        dates = await page.eval_on_selector(
            ".festival-dates, .event-dates, time",
            "el => el.innerText.trim()",
        )
    except Exception:
        pass

    # Year from URL or name
    m = re.search(r"(\d{4})", festival_url)
    year = m.group(1) if m else ""

    # Poster image — largest img near the top of the page
    poster_url: str | None = None
    try:
        poster_url = await page.eval_on_selector(
            ".festival-poster img, .lineup-poster img, .entry-content img",
            "el => el.src",
        )
    except Exception:
        pass

    # All lineup band names (linked artist names)
    lineup_links = await page.eval_on_selector_all(
        "a[href*='/artist/']",
        "els => els.map(e => e.innerText.trim().toLowerCase())",
    )
    lineup_names = list(dict.fromkeys(n for n in lineup_links if n))

    return {
        "name": name,
        "year": year,
        "dates": dates,
        "poster_url": poster_url,
        "lineup_names": lineup_names,
    }


def _score_festival(detail: dict, search_pool: list[dict]) -> tuple[int, int, list[dict]]:
    """
    Return (raw_score, pct, matching_bands) for a single festival.

    matching_bands: list of {name, score, liked} for pool artists in the lineup.
    """
    lineup_set = {n.lower() for n in detail["lineup_names"]}
    matching: list[dict] = []
    raw = 0
    for artist in search_pool:
        name_lower = artist["name"].lower()
        if name_lower in lineup_set:
            pts = artist.get("score") or 5
            raw += pts
            matching.append({
                "name": artist["name"],
                "score": artist.get("score"),
                "liked": artist["source"] == "liked",
            })
    mx = max(_max_score(search_pool), 1)
    pct = min(round(raw / mx * 100), 100)
    return raw, pct, matching


async def search_mfw(search_pool: list[dict]) -> list[dict]:
    """
    Search MusicFestivalWizard for festivals featuring artists in search_pool.

    Args:
        search_pool: list of {name, score, source} dicts — the artists to search for.

    Returns:
        List of MfwFestivalResult-shaped dicts, sorted by pct desc.

    Raises:
        MfwNotAvailable: if Playwright is not installed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise MfwNotAvailable(
            "Playwright is not installed. "
            "Run: pip install playwright && playwright install chromium"
        )

    festival_details: dict[str, dict] = {}  # url → detail dict

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # Step 1: collect festival URLs for each artist
        festival_urls: list[str] = []
        for artist in search_pool:
            slug = _slugify(artist["name"])
            try:
                links = await _fetch_artist_festival_links(page, slug)
                festival_urls.extend(links)
                await asyncio.sleep(1)  # polite crawl delay
            except Exception:
                pass  # artist not found on MFW — skip

        festival_urls = list(dict.fromkeys(festival_urls))  # global deduplicate

        # Step 2: fetch details for each festival
        for url in festival_urls:
            if url in festival_details:
                continue
            try:
                detail = await _fetch_festival_details(page, url)
                festival_details[url] = detail
                await asyncio.sleep(1)
            except Exception:
                pass

        await browser.close()

    # Step 3: score and sort
    results: list[dict] = []
    for url, detail in festival_details.items():
        raw, pct, matching = _score_festival(detail, search_pool)
        if not matching:
            continue  # only return festivals with at least one match
        results.append({
            "festival_name": detail["name"],
            "festival_url": url,
            "year": detail["year"],
            "dates": detail["dates"],
            "poster_url": detail["poster_url"],
            "score": raw,
            "pct": pct,
            "matching_bands": matching,
            "total_lineup_bands": len(detail["lineup_names"]),
        })

    results.sort(key=lambda r: r["pct"], reverse=True)
    return results
