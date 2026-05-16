"""
PaddleOCR local vision backend.

Pipeline
────────
1. PaddleOCR extracts every text block from the poster image, returning 4-corner
   bounding boxes and confidence scores.  No network call — fully local.

2. Rank (1–100) is computed from bounding-box height: taller text = larger font
   = higher billing.  Heights are linearly normalized across all blocks.

3. Noise filter removes obvious non-band text (dates, URLs, venue words, etc.)
   via regex heuristics, leaving a set of candidates.

4. Music platform cross-reference (band_validator):
     • iTunes Search API  — free, no credentials, always tried first
     • Spotify artist search — tried if SPOTIFY_CLIENT_ID/SECRET are set
       (Client Credentials flow — no user login required)
     • MusicBrainz          — free fallback, 1 req/sec
   Each candidate is looked up on the platforms.  If the platform returns a
   name with ≥ 0.80 similarity to our query:
     - The platform's canonical name is used (fixing OCR errors)
     - The Spotify artist ID is stored on the BandEntry for free
     - The candidate is confirmed as a real artist
   Candidates with no platform match are still included but marked unverified.

   NOTE: No ANTHROPIC_API_KEY is ever used in the PaddleOCR path — the
   validator is purely based on music platform data.

5. Alphabetical detection: if ≥ 75% of confirmed names are in A–Z order when
   read top-to-bottom, the poster is flagged alphabetical and ranks are nulled.

Dependencies
────────────
  pip install paddleocr paddlepaddle          # CPU (works on any machine)
  pip install paddleocr paddlepaddle-gpu      # GPU / CUDA (much faster)
"""

from __future__ import annotations

import re
import statistics
import string
from pathlib import Path

from ingestion.image_utils import prepare_poster
from ingestion.poster_parser import BandEntry, ParsedPoster
from ingestion.band_validator import validate_band_name


_DAY_HEADER_RE = re.compile(
    r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday'
    r'|mon|tue|wed|thu|fri|sat|sun)\b'
    r'|\bday\s*\d+\b',
    re.IGNORECASE,
)

_DAY_NAME_NORMALIZE = {
    'monday': 'monday', 'mon': 'monday',
    'tuesday': 'tuesday', 'tue': 'tuesday',
    'wednesday': 'wednesday', 'wed': 'wednesday',
    'thursday': 'thursday', 'thu': 'thursday',
    'friday': 'friday', 'fri': 'friday',
    'saturday': 'saturday', 'sat': 'saturday',
    'sunday': 'sunday', 'sun': 'sunday',
}


def _bbox_x_center(bbox: list[list[float]]) -> float:
    xs = [pt[0] for pt in bbox]
    return (min(xs) + max(xs)) / 2


def _normalize_day(text: str) -> str:
    t = text.lower().strip()
    for short, full in _DAY_NAME_NORMALIZE.items():
        if re.search(r'\b' + short + r'\b', t):
            return full
    m = re.search(r'day\s*(\d+)', t)
    if m:
        return f'day {m.group(1)}'
    return t


def _detect_day_groups(
    blocks: list[tuple[list, str, float]]
) -> dict[str, tuple[str, float]] | None:
    """
    Detect day-section headers in the poster and return a map of
    text → (day_label, within_day_rank).

    Returns None if no multi-day structure is found.
    """
    if not blocks:
        return None

    heights = [_bbox_height(b[0]) for b in blocks]
    median_h = statistics.median(heights)

    # Find candidate headers: match day pattern AND reasonably large text
    headers: list[tuple[float, float, str]] = []  # (xc, yc, day_label)
    header_texts: set[str] = set()
    for bbox, text, _conf in blocks:
        if _DAY_HEADER_RE.search(text) and _bbox_height(bbox) >= median_h * 0.5:
            xc = _bbox_x_center(bbox)
            yc = _bbox_y_center(bbox)
            label = _normalize_day(text)
            headers.append((xc, yc, label))
            header_texts.add(text)

    if len(headers) < 2:
        return None  # Not a multi-day poster

    # Detect layout: column (headers spread horizontally) vs row (spread vertically)
    x_spread = max(h[0] for h in headers) - min(h[0] for h in headers)
    y_spread = max(h[1] for h in headers) - min(h[1] for h in headers)
    column_layout = x_spread > y_spread

    # Assign non-header blocks to a day group
    day_blocks: dict[str, list[tuple[str, float]]] = {h[2]: [] for h in headers}

    for bbox, text, _conf in blocks:
        if text in header_texts:
            continue
        xc = _bbox_x_center(bbox)
        yc = _bbox_y_center(bbox)
        h = _bbox_height(bbox)

        if column_layout:
            # Assign to the nearest header by x-distance
            day = min(headers, key=lambda hdr: abs(hdr[0] - xc))[2]
        else:
            # Assign to the nearest header that is ABOVE this block (lower yc)
            above = [(hdr[1], hdr[2]) for hdr in headers if hdr[1] < yc]
            if above:
                day = max(above, key=lambda t: t[0])[1]  # closest above
            else:
                day = headers[0][2]  # fallback: first header

        day_blocks[day].append((text, h))

    # Compute normalized rank within each day group
    result: dict[str, tuple[str, float]] = {}
    for day, text_heights in day_blocks.items():
        if not text_heights:
            continue
        texts = [th[0] for th in text_heights]
        hs = [th[1] for th in text_heights]
        ranks = _normalize_ranks(hs)
        for text, rank in zip(texts, ranks):
            result[text] = (day, rank)

    return result if result else None


# ── Non-band text filters ─────────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r"""
    # URLs / domains
    https?://|www\.|\.com\b|\.net\b|\.org\b|\.fm\b|\.io\b
    # Month names
    |\b(january|february|march|april|may|june|july|august
        |september|october|november|december)\b
    |\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b
    # Day names
    |\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b
    |\b(mon|tue|wed|thu|fri|sat|sun)\b
    # Times
    |\b\d{1,2}:\d{2}\s*(am|pm)\b
    # Venue / ticket boilerplate
    |\b(tickets?|presented\s+by|sponsored\s+by|featuring
        |general\s+admission|vip\b|18\+|21\+|doors|stage
        |lineup|festival|presents|free\s+show|all\s+ages
        |advance|day\s+of)\b
    # Standalone year or date fraction
    |\b20\d{2}\b
    |\b\d{1,2}/\d{1,2}\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_SHORT_OR_PUNCT = re.compile(r"^[\W\d]{0,3}$")


def _is_noise(text: str) -> bool:
    t = text.strip()
    if _SHORT_OR_PUNCT.match(t):
        return True
    if _NOISE_RE.search(t):
        return True
    if len(t) > 60:      # descriptions / addresses
        return True
    return False


# ── Bounding-box geometry ─────────────────────────────────────────────────────

def _bbox_height(bbox: list[list[float]]) -> float:
    ys = [pt[1] for pt in bbox]
    return max(ys) - min(ys)


def _bbox_y_center(bbox: list[list[float]]) -> float:
    ys = [pt[1] for pt in bbox]
    return (min(ys) + max(ys)) / 2


def _normalize_ranks(heights: list[float]) -> list[float]:
    lo, hi = min(heights), max(heights)
    span = hi - lo or 1.0
    return [round(1.0 + 99.0 * (h - lo) / span, 1) for h in heights]


# ── Alphabetical detection ────────────────────────────────────────────────────

def _is_alphabetical(names: list[str], y_centers: list[float]) -> bool:
    """True when names read top-to-bottom are ≥ 75% in A–Z order."""
    if len(names) < 3:
        return False
    ordered = [n for _, n in sorted(zip(y_centers, names))]
    matches = sum(a.lower() <= b.lower() for a, b in zip(ordered, ordered[1:]))
    return matches / (len(ordered) - 1) >= 0.75


# ── Festival metadata heuristics ─────────────────────────────────────────────

_MONTH_LIST = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep oct nov dec"
).split()

_DATE_RE = re.compile(
    r"\b(\d{1,2})[/.\-](\d{1,2})([/.\-]\d{2,4})?\b"
    r"|\b(\d{1,2})\s+(" + "|".join(_MONTH_LIST[:12]) + r")\b"
    r"|\b(" + "|".join(_MONTH_LIST) + r")\s+\d{1,2}\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_CITY_STATE_RE = re.compile(r"[A-Za-z\s]{3,},\s*[A-Z]{2}\b")


def _extract_metadata(
    blocks: list[tuple[list, str, float]]
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (festival_name, location, start_date, end_date)."""
    if not blocks:
        return None, None, None, None

    # Largest text block → festival name
    by_size = sorted(blocks, key=lambda b: _bbox_height(b[0]), reverse=True)
    festival_name: str | None = None
    for _, text, _ in by_size:
        t = text.strip()
        if not _is_noise(t) and 1 <= len(t.split()) <= 7:
            festival_name = t
            break

    # City, ST pattern
    location: str | None = None
    for _, text, _ in blocks:
        m = _CITY_STATE_RE.search(text)
        if m:
            location = m.group(0).strip()
            break

    # Dates
    all_text = " ".join(t for _, t, _ in blocks)
    date_hits = _DATE_RE.findall(all_text)
    years = _YEAR_RE.findall(all_text)
    start_date = end_date = None
    if date_hits:
        raw = date_hits[0][0] or date_hits[0][3] or date_hits[0][5] or None
        if raw and years:
            start_date = f"{years[0]}-{raw}"
        elif raw:
            start_date = raw
        if len(date_hits) > 1:
            raw2 = date_hits[-1][0] or date_hits[-1][3] or date_hits[-1][5] or None
            end_date = raw2 or None

    return festival_name, location, start_date, end_date


# ── Rank lookup (by original OCR text, before canonicalization) ───────────────

def _rank_for(ocr_text: str, rank_map: dict[str, float]) -> float | None:
    if ocr_text in rank_map:
        return rank_map[ocr_text]
    lo = ocr_text.lower()
    for k, r in rank_map.items():
        if k.lower() == lo or lo in k.lower() or k.lower() in lo:
            return r
    return None


# ── Title-case fix ────────────────────────────────────────────────────────────

def _maybe_title_case(text: str) -> str:
    """Convert ALL-CAPS text to Title Case; leave mixed-case alone."""
    if text == text.upper() and len(text) > 2:
        return string.capwords(text)
    return text


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_with_paddleocr(source: str | Path, source_url: str = "") -> ParsedPoster:
    """
    Parse a festival poster using PaddleOCR (local) + music platform validation.

    No LLM is called at any point in this path.
    """
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise ImportError(
            "PaddleOCR is not installed.\n"
            "  CPU:  pip install paddleocr paddlepaddle\n"
            "  GPU:  pip install paddleocr paddlepaddle-gpu\n"
        ) from exc

    _b64, media_type, local_path = prepare_poster(source)

    if media_type == "application/pdf":
        raise ValueError(
            "PaddleOCR backend does not support PDFs. "
            "Convert to PNG first, or set VISION_BACKEND=claude."
        )

    # ── Step 1: OCR ───────────────────────────────────────────────────────
    ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    result = ocr.ocr(str(local_path), cls=True)

    page = result[0] if result else []
    blocks: list[tuple[list, str, float]] = []
    for item in (page or []):
        if not item:
            continue
        bbox, (text, confidence) = item
        text = text.strip()
        if text:
            blocks.append((bbox, text, float(confidence)))

    if not blocks:
        return _empty(source, local_path, source_url)

    # ── Step 2: Rank from bbox height ─────────────────────────────────────
    heights = [_bbox_height(b[0]) for b in blocks]
    ranks = _normalize_ranks(heights)
    y_centers = [_bbox_y_center(b[0]) for b in blocks]
    rank_map = {b[1]: r for b, r in zip(blocks, ranks)}

    # ── Step 2b: detect day groups and compute per-day ranks ─────────────
    day_group_map = _detect_day_groups(blocks)  # text → (day, within_day_rank) or None

    # ── Step 3: Noise filter → candidate list ─────────────────────────────
    candidates: list[tuple[str, float, float]] = [  # (ocr_text, rank, y_center)
        (text, rank, yc)
        for (_, text, _), rank, yc in zip(blocks, ranks, y_centers)
        if not _is_noise(text)
    ]

    # ── Step 4: Music platform cross-reference ────────────────────────────
    band_entries: list[BandEntry] = []
    confirmed_names: list[str] = []
    confirmed_ycenters: list[float] = []

    for ocr_text, rank, yc in candidates:
        vr = validate_band_name(ocr_text)

        if vr.is_artist:
            # Platform confirmed → use canonical name (fixes OCR errors)
            name = vr.canonical
        else:
            # No platform match — apply local title-case fix, include anyway
            name = _maybe_title_case(ocr_text)

        entry_rank = None
        entry_day = ""
        if day_group_map and ocr_text in day_group_map:
            entry_day, entry_rank = day_group_map[ocr_text]
        else:
            entry_rank = _rank_for(ocr_text, rank_map)

        band_entries.append(BandEntry(
            name=name,
            rank=entry_rank,
            spotify_id=vr.spotify_id,
            day=entry_day,
        ))
        confirmed_names.append(name)
        confirmed_ycenters.append(yc)

    # ── Step 5: Alphabetical detection on confirmed names ─────────────────
    alphabetical = _is_alphabetical(confirmed_names, confirmed_ycenters)
    if alphabetical:
        for e in band_entries:
            e.rank = None

    # ── Step 6: Festival metadata heuristics ──────────────────────────────
    festival_name, location, start_date, end_date = _extract_metadata(blocks)

    source_str = str(source)
    return ParsedPoster(
        festival_name=festival_name,
        location=location,
        start_date=start_date,
        end_date=end_date,
        alphabetical=alphabetical,
        bands=band_entries,
        source_url=source_url or (source_str if source_str.startswith("http") else ""),
        poster_path=str(local_path),
        raw_response="\n".join(b[1] for b in blocks),
    )


def _empty(source, local_path: Path, source_url: str) -> ParsedPoster:
    source_str = str(source)
    return ParsedPoster(
        festival_name=None, location=None,
        start_date=None, end_date=None,
        alphabetical=False, bands=[],
        source_url=source_url or (source_str if source_str.startswith("http") else ""),
        poster_path=str(local_path),
        raw_response="",
    )
