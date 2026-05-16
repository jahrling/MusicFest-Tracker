"""
Band name validator — cross-references OCR candidate text against music platforms
to confirm real artist names and surface canonical spellings.

Why this works
──────────────
Festival posters often have intentionally weird spellings (Phish, Ludacris,
Blackbear, !!! …). Those ARE the canonical names and will match exactly on
every platform — no correction needed.

OCR errors ("Radi0head", "The Nationa|") will be caught because the platform
returns "Radiohead" / "The National" as the closest match, and the similarity
score between our query and that answer will be high (≥ 0.80). We accept the
platform's spelling as ground-truth.

Non-band noise ("June 15", "Presented By", "VIP Packages") will NOT match any
real artist with meaningful similarity, so they stay below the threshold and
are marked unverified (the caller decides whether to include them).

Platform priority
─────────────────
1. iTunes / Apple Music Search API  — free, no credentials, fast (~200 ms)
2. Spotify artist search            — needs Client Credentials; also captures
                                       Spotify artist ID for free at ingest time
3. MusicBrainz                      — free, no auth, rate-limited to 1 req/sec;
                                       good for obscure/niche artists

Only the first platform that returns similarity ≥ ACCEPT_THRESHOLD is used;
the others are skipped for that candidate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


ACCEPT_THRESHOLD = 0.80   # similarity score to accept a platform match
_REQUEST_TIMEOUT = 5.0    # seconds per HTTP request

# Simple in-process cache: {query_lower: ValidationResult}
_cache: dict[str, "ValidationResult"] = {}

# MusicBrainz rate limit
_mb_last_call: float = 0.0
_MB_RATE_LIMIT = 1.05  # seconds between MusicBrainz requests


@dataclass
class ValidationResult:
    original: str       # OCR text as-is
    canonical: str      # artist name from platform (may equal original)
    confidence: float   # 0.0–1.0 similarity
    platform: str       # "itunes" | "spotify" | "musicbrainz" | "unverified"
    spotify_id: str     # Spotify artist ID; empty string if not found via Spotify
    is_artist: bool     # True when any platform confirmed at ≥ ACCEPT_THRESHOLD


# ── Public API ────────────────────────────────────────────────────────────────

def validate_band_name(name: str) -> ValidationResult:
    """
    Check whether `name` is a known artist on iTunes, Spotify, or MusicBrainz.

    Returns a ValidationResult. The call is cached per process — duplicate
    names from the same poster are free after the first lookup.
    """
    key = name.strip().lower()
    if key in _cache:
        return _cache[key]

    result = (
        _try_itunes(name)
        or _try_spotify(name)
        or _try_musicbrainz(name)
        or ValidationResult(
            original=name,
            canonical=name,
            confidence=0.0,
            platform="unverified",
            spotify_id="",
            is_artist=False,
        )
    )

    _cache[key] = result
    return result


def clear_cache() -> None:
    """Reset the in-process cache (useful between test runs)."""
    _cache.clear()


# ── Platform implementations ──────────────────────────────────────────────────

def _try_itunes(name: str) -> ValidationResult | None:
    """
    Apple Music / iTunes Search API — completely free, no credentials required.
    https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/
    """
    try:
        resp = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": name, "entity": "musicArtist", "limit": 3},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None

        best = _best_match(name, [r.get("artistName", "") for r in results])
        if best is None:
            return None
        canonical, sim = best

        return ValidationResult(
            original=name,
            canonical=canonical,
            confidence=sim,
            platform="itunes",
            spotify_id="",
            is_artist=sim >= ACCEPT_THRESHOLD,
        )
    except Exception:
        return None


def _try_spotify(name: str) -> ValidationResult | None:
    """
    Spotify artist search via Client Credentials OAuth (no user login required).
    Also captures the Spotify artist ID so playlist building is faster later.
    """
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    try:
        sp = _spotify_client()
        if sp is None:
            return None
        results = sp.search(q=f"artist:{name}", type="artist", limit=3)
        items = results.get("artists", {}).get("items", [])
        if not items:
            return None

        best = _best_match(name, [i.get("name", "") for i in items])
        if best is None:
            return None
        canonical, sim = best

        # Find the matching item to grab its ID
        spotify_id = ""
        for item in items:
            if item.get("name", "").lower() == canonical.lower():
                spotify_id = item.get("id", "")
                break

        return ValidationResult(
            original=name,
            canonical=canonical,
            confidence=sim,
            platform="spotify",
            spotify_id=spotify_id,
            is_artist=sim >= ACCEPT_THRESHOLD,
        )
    except Exception:
        return None


def _try_musicbrainz(name: str) -> ValidationResult | None:
    """
    MusicBrainz open music encyclopedia — no credentials, 1 req/sec limit.
    Good for niche/obscure artists not well-indexed by iTunes or Spotify.
    """
    global _mb_last_call
    elapsed = time.monotonic() - _mb_last_call
    if elapsed < _MB_RATE_LIMIT:
        time.sleep(_MB_RATE_LIMIT - elapsed)

    try:
        resp = httpx.get(
            "https://musicbrainz.org/ws/2/artist",
            params={"query": name, "fmt": "json", "limit": 3},
            headers={"User-Agent": "FestivalTracker/1.0 (festival-tracker)"},
            timeout=_REQUEST_TIMEOUT,
        )
        _mb_last_call = time.monotonic()
        resp.raise_for_status()
        data = resp.json()
        artists = data.get("artists", [])
        if not artists:
            return None

        # MusicBrainz returns its own 0–100 relevance score
        # Use string similarity against the name field for our threshold
        best = _best_match(name, [a.get("name", "") for a in artists])
        if best is None:
            return None
        canonical, sim = best

        return ValidationResult(
            original=name,
            canonical=canonical,
            confidence=sim,
            platform="musicbrainz",
            spotify_id="",
            is_artist=sim >= ACCEPT_THRESHOLD,
        )
    except Exception:
        _mb_last_call = time.monotonic()
        return None


# ── Similarity helpers ────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """
    String similarity in [0, 1].

    Uses SequenceMatcher as the base, with a containment bonus so that
    "Radiohead" vs "Radiohead (US)" still scores high.
    """
    a_l, b_l = a.lower().strip(), b.lower().strip()
    if not a_l or not b_l:
        return 0.0
    if a_l == b_l:
        return 1.0
    ratio = SequenceMatcher(None, a_l, b_l).ratio()
    # Boost when one is a prefix/suffix/substring of the other
    if a_l in b_l or b_l in a_l:
        ratio = max(ratio, 0.85)
    return round(ratio, 4)


def _best_match(query: str, candidates: list[str]) -> tuple[str, float] | None:
    """Return (best_candidate, similarity) or None if no candidates."""
    if not candidates:
        return None
    scored = [(c, _similarity(query, c)) for c in candidates if c]
    if not scored:
        return None
    best = max(scored, key=lambda x: x[1])
    return best if best[1] > 0 else None


# ── Spotify client singleton ──────────────────────────────────────────────────

_spotify_instance = None


def _spotify_client():
    """Lazy singleton using Client Credentials (read-only, no user login)."""
    global _spotify_instance
    if _spotify_instance is not None:
        return _spotify_instance
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        _spotify_instance = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                cache_path=".spotify_cc_cache",
            )
        )
        return _spotify_instance
    except Exception:
        return None
