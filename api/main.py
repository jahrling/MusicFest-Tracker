"""
FastAPI application — Festival Band Tracker.

Endpoints:
  POST /ingest/url          — ingest poster from URL
  POST /ingest/upload       — ingest uploaded image/PDF
  GET  /bands               — list all bands
  GET  /bands/{id}          — band detail + history
  POST /bands/{id}/rate     — save a rating
  POST /bands/{id}/research — trigger research agent for one band
  GET  /festivals           — list festivals
  GET  /timeline            — full band-festival timeline
  GET  /timeline.csv        — CSV export
  POST /playlist            — build Spotify playlist
  GET  /                    — main UI
"""

from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import threading
from dataclasses import dataclass, field as _dc_field

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from config import BASE_DIR, POSTERS_DIR, RESEARCH_RANK_THRESHOLD, ANTHROPIC_API_KEY, VISION_BACKEND

# ── Background ingest job store ───────────────────────────────────────────────

@dataclass
class _IngestJob:
    id: str
    status: str           # queued | processing | done
    displays: list[str]   # user-visible names (filename / URL)
    paths: list[str]      # actual file paths or URLs to process
    results: list[dict] = _dc_field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

_jobs: dict[str, _IngestJob] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 600  # seconds to retain completed jobs in memory


def _new_job(displays: list[str], paths: list[str]) -> _IngestJob:
    job = _IngestJob(
        id=str(uuid.uuid4())[:8],
        status="queued",
        displays=displays,
        paths=paths,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    with _jobs_lock:
        _jobs[job.id] = job
        cutoff = datetime.now(timezone.utc).timestamp() - _JOB_TTL
        stale = [jid for jid, j in _jobs.items()
                 if datetime.fromisoformat(j.created_at).timestamp() < cutoff]
        for jid in stale:
            del _jobs[jid]
    return job


def _run_ingest_job(job_id: str) -> None:
    """Background worker — runs in a thread pool via BackgroundTasks."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = "processing"
        job.updated_at = datetime.now(timezone.utc).isoformat()

    results = []
    for display, path in zip(job.displays, job.paths):
        try:
            is_url = path.startswith("http://") or path.startswith("https://")
            r = ingest_poster(path if is_url else Path(path),
                              source_url=path if is_url else "")
            results.append({
                "source": display,
                "festival_name": r.festival_name,
                "band_count": len(r.band_ids),
                "bands_added": r.bands_added,
                "bands_merged": r.bands_merged,
                "alphabetical": r.alphabetical,
                "reimport": r.reimport,
            })
        except Exception as exc:
            log.exception("Ingest job %s failed for %s", job_id, display)
            results.append({"source": display, "error": str(exc)})
        finally:
            # Delete temp file after processing an upload
            if not (path.startswith("http://") or path.startswith("https://")):
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.status = "done"
            job.results = results
            job.updated_at = datetime.now(timezone.utc).isoformat()
    log.info("Ingest job %s done — %d source(s)", job_id, len(results))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("festival_tracker")
from graph import queries
from ingestion.ingest import ingest_poster
from ratings.rating_service import VALID_INTEREST, rate_band
from research.research_pipeline import run_research_pipeline
from spotify.liked_songs import get_sync_status, sync_liked_songs
from spotify.playlist_builder import build_playlist, filter_band_ids

app = FastAPI(title="Festival Band Tracker", version="1.0.0")


@app.on_event("startup")
async def _startup_checks() -> None:
    if VISION_BACKEND == "claude" and not ANTHROPIC_API_KEY:
        log.warning(
            "ANTHROPIC_API_KEY is not set but VISION_BACKEND=claude. "
            "Poster ingestion will fail. Copy .env.example to .env and fill in your key."
        )
    else:
        log.info("Vision backend: %s", VISION_BACKEND)
    # Eagerly initialize the graph DB so schema errors surface at startup, not mid-request
    try:
        from graph.db import get_connection
        get_connection()
        log.info("Graph DB initialised OK")
    except Exception:
        log.exception("Graph DB failed to initialise — check data/graph.db permissions")


templates = Jinja2Templates(directory=str(BASE_DIR / "ui" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "ui" / "static")), name="static")
app.mount("/posters", StaticFiles(directory=str(POSTERS_DIR)), name="posters")


# ── UI routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from collections import defaultdict

    festivals = queries.list_festivals()  # sorted name ASC, date DESC
    bands = queries.list_bands_with_festivals()

    # Full rating data per band (latest entry wins, get_all_ratings is DESC by timestamp)
    rating_data: dict[str, dict] = {}
    for r in queries.get_all_ratings():
        bid = r.get("b.id", "")
        if bid and bid not in rating_data:
            rating_data[bid] = r

    global_ranks = queries.get_band_global_ranks()
    liked_names = queries.get_liked_artist_names()

    for band in bands:
        bid = band["id"]
        rating = rating_data.get(bid)
        score = int(rating["r.score"]) if rating and rating.get("r.score") is not None else None

        band["score"] = score
        band["interest"] = rating.get("r.interest", "") if rating else ""
        band["rating_notes"] = (rating.get("r.notes", "") or "") if rating else ""
        band["global_rank"] = global_ranks.get(bid, 0.0)
        band["is_liked"] = band.get("name_lower", "") in liked_names

        if score is not None:
            band["color_class"] = "rated-high" if score >= 7 else ("rated-mid" if score >= 4 else "rated-low")
        elif band["is_liked"]:
            band["color_class"] = "liked"
        elif band.get("bubble_status"):
            band["color_class"] = f"status-{band['bubble_status']}"
        else:
            band["color_class"] = ""

    # Default order: most recent festival rank descending
    bands.sort(key=lambda b: b.get("global_rank", 0.0), reverse=True)

    def _extract_city(location: str) -> str:
        """'Grant Park, Chicago, IL' → 'Chicago'  |  'Chicago, IL' → 'Chicago'"""
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 3:
            return parts[-2]   # skip venue prefix and state suffix
        if len(parts) == 2:
            return parts[0]    # "City, ST"
        return parts[0] if parts else ""

    # Group festivals by (name, city) so same festival in different cities stays separate
    festival_groups: dict[tuple, list] = defaultdict(list)
    for f in festivals:
        city = _extract_city(f.get("location", ""))
        festival_groups[(f.get("name", ""), city)].append(f)

    festivals_grouped = []
    for (name, city), entries in sorted(festival_groups.items(), key=lambda x: x[0][0].lower()):
        sorted_entries = sorted(entries, key=lambda f: f.get("start_date", ""), reverse=True)
        rep_location = sorted_entries[0].get("location", "") if sorted_entries else ""
        festivals_grouped.append({
            "name": name,
            "city": city,
            "display_name": f"{name} ({city})" if city else name,
            "location": rep_location,
            "entries": sorted_entries,
        })

    liked_count = queries.count_liked_artists()
    rated_high_count = sum(
        1 for r in rating_data.values()
        if r.get("r.score") is not None and int(r["r.score"]) >= 7
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "festivals": festivals,
            "festivals_grouped": festivals_grouped,
            "bands": bands,
            "liked_count": liked_count,
            "rated_high_count": rated_high_count,
        },
    )


@app.get("/bands/{band_id}/view", response_class=HTMLResponse)
async def band_detail_page(request: Request, band_id: str):
    band = queries.get_band(band_id)
    if not band:
        raise HTTPException(404, "Band not found")
    history = queries.get_band_festival_history(band_id)
    rating = queries.get_latest_rating(band_id)
    genres = _parse_json_list(band.get("genres", ""))
    influences = _parse_json_list(band.get("influences", ""))
    youtube_links = _parse_json_list(band.get("youtube_links", ""))
    return templates.TemplateResponse(
        request,
        "band_detail.html",
        {
            "band": band,
            "history": history,
            "rating": rating,
            "genres": genres,
            "influences": influences,
            "youtube_links": youtube_links,
        },
    )


@app.get("/timeline/view", response_class=HTMLResponse)
async def timeline_page(request: Request):
    timeline = queries.get_full_timeline()
    return templates.TemplateResponse(
        request, "timeline.html", {"timeline": timeline}
    )


@app.get("/playlist/view", response_class=HTMLResponse)
async def playlist_page(request: Request):
    festivals = queries.list_festivals()
    return templates.TemplateResponse(
        request, "playlist.html", {"festivals": festivals}
    )


# ── API routes ────────────────────────────────────────────────────────────────

@app.post("/ingest/url")
async def ingest_from_url(background_tasks: BackgroundTasks, urls: str = Form(...)):
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    if not url_list:
        raise HTTPException(400, "No URLs provided")
    job = _new_job(displays=url_list, paths=url_list)
    background_tasks.add_task(_run_ingest_job, job.id)
    return {"job_id": job.id, "status": "queued", "source_count": len(url_list), "displays": url_list}


@app.post("/ingest/upload")
async def ingest_from_upload(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    displays, paths = [], []
    for file in files:
        suffix = Path(file.filename or "poster.jpg").suffix or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=POSTERS_DIR) as tmp:
            tmp.write(await file.read())
            paths.append(tmp.name)
        displays.append(file.filename or Path(paths[-1]).name)
    job = _new_job(displays=displays, paths=paths)
    background_tasks.add_task(_run_ingest_job, job.id)
    return {"job_id": job.id, "status": "queued", "source_count": len(displays), "displays": displays}


@app.get("/ingest/jobs/{job_id}")
async def get_ingest_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired")
    return {
        "job_id": job.id,
        "status": job.status,
        "displays": job.displays,
        "results": job.results,
        "updated_at": job.updated_at,
    }


@app.get("/ingest/jobs")
async def list_ingest_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())
    return [
        {"job_id": j.id, "status": j.status, "source_count": len(j.displays), "updated_at": j.updated_at}
        for j in jobs
    ]


@app.get("/bands")
async def list_bands_api():
    return queries.list_bands()


@app.get("/bands/{band_id}")
async def get_band_api(band_id: str):
    band = queries.get_band(band_id)
    if not band:
        raise HTTPException(404, "Band not found")
    history = queries.get_band_festival_history(band_id)
    rating = queries.get_latest_rating(band_id)
    return {
        "band": band,
        "festival_history": history,
        "latest_rating": rating,
        "genres": _parse_json_list(band.get("genres", "")),
        "influences": _parse_json_list(band.get("influences", "")),
        "youtube_links": _parse_json_list(band.get("youtube_links", "")),
    }


@app.post("/bands/{band_id}/rate")
async def rate_band_api(
    band_id: str,
    score: int = Form(...),
    interest: str = Form(...),
    notes: str = Form(""),
):
    if not queries.get_band(band_id):
        raise HTTPException(404, "Band not found")
    try:
        rate_band(band_id=band_id, score=score, interest=interest, notes=notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok"}


@app.post("/bands/{band_id}/research")
async def research_band_api(band_id: str):
    band = queries.get_band(band_id)
    if not band:
        raise HTTPException(404, "Band not found")
    results = run_research_pipeline([band_id])
    success = results.get(band_id, False)
    updated = queries.get_band(band_id)
    return {"success": success, "band": updated}


@app.post("/bands/{band_id}/find-festivals", response_class=HTMLResponse)
async def find_band_festivals_api(band_id: str):
    band = queries.get_band(band_id)
    if not band:
        raise HTTPException(404, "Band not found")
    from research.band_agent import find_band_festivals

    band_name = band.get("name", band_id)
    try:
        result = find_band_festivals(band_name)
    except Exception as exc:
        log.exception("Find festivals failed for %s", band_name)
        return HTMLResponse(
            f'<div class="ingest-error">Find festivals failed: {exc}</div>'
        )

    festivals = result.get("festivals", [])
    notes = result.get("notes", "")

    if not festivals:
        msg = notes or "No festival appearances found in the last 5 years."
        return HTMLResponse(f'<p class="muted">{msg}</p>')

    rows = ""
    for f in festivals:
        name = (f.get("name") or "").strip() or "—"
        loc = (f.get("location") or "").strip() or "—"
        date = (f.get("date") or "").strip() or "—"
        status = (f.get("status") or "").strip()
        source = (f.get("source") or "").strip()
        status_tag = (
            f'<span class="tag">{status}</span>' if status else ""
        )
        source_html = (
            f'<br><span class="muted" style="font-size:0.75rem">source: {source}</span>'
            if source else ""
        )
        rows += (
            f'<tr>'
            f'<td><strong>{name}</strong>{source_html}</td>'
            f'<td>{date}</td>'
            f'<td>{loc}</td>'
            f'<td>{status_tag}</td>'
            f'</tr>'
        )
    notes_html = f'<p class="muted" style="font-size:0.8rem">{notes}</p>' if notes else ""
    return HTMLResponse(
        f'<div class="find-festivals-results">'
        f'<table class="data-table">'
        f'<thead><tr><th>Festival</th><th>Date</th><th>Location</th><th>Status</th></tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
        f'{notes_html}'
        f'</div>'
    )


@app.post("/research/batch")
async def research_batch_api(festival_id: str = Form(...)):
    timeline = queries.get_full_timeline()
    band_ids = list({
        row["b.id"] for row in timeline if row.get("f.id") == festival_id
    })
    results = run_research_pipeline(band_ids, festival_id=festival_id)
    return {
        "total": len(results),
        "succeeded": sum(1 for v in results.values() if v),
        "failed": sum(1 for v in results.values() if not v),
    }


@app.get("/festivals")
async def list_festivals_api():
    return queries.list_festivals()


@app.get("/timeline")
async def timeline_api():
    return queries.get_full_timeline()


@app.get("/timeline.csv")
async def timeline_csv():
    rows = queries.get_full_timeline()
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=timeline.csv"},
    )


@app.post("/playlist")
async def create_playlist_api(
    playlist_name: str = Form(""),
    min_rating: int = Form(0),
    interest_levels: str = Form(""),
    bubble_statuses: str = Form(""),
    festival_id: str = Form(""),
    tracks_per_band: int = Form(5),
):
    interest = [i.strip() for i in interest_levels.split(",") if i.strip()] or None
    bubble = [b.strip() for b in bubble_statuses.split(",") if b.strip()] or None
    fid = festival_id or None
    rating = min_rating if min_rating > 0 else None

    band_ids = filter_band_ids(
        rating_filter=rating,
        interest_filter=interest,
        bubble_filter=bubble,
        festival_id=fid,
    )
    if not band_ids:
        raise HTTPException(400, "No bands match the selected filters")

    try:
        url = build_playlist(
            band_ids=band_ids,
            playlist_name=playlist_name or None,
            tracks_per_band=tracks_per_band,
        )
    except Exception as exc:
        log.exception("Playlist build failed")
        raise HTTPException(500, str(exc))

    return {"playlist_url": url, "band_count": len(band_ids)}


# ── De-duplicate ─────────────────────────────────────────────────────────────

@app.post("/dedup")
async def run_dedup():
    try:
        result = queries.dedup_graph()
    except Exception as exc:
        log.exception("Dedup failed")
        raise HTTPException(500, str(exc))
    return result


# ── Festival playlist ──────────────────────────────────────────────────────────

@app.post("/playlist/festival/{festival_id}")
async def create_festival_playlist(festival_id: str):
    festival = queries.get_festival(festival_id)
    if not festival:
        raise HTTPException(404, "Festival not found")

    timeline = queries.get_full_timeline()
    band_ids = list({row["b.id"] for row in timeline if row.get("f.id") == festival_id})
    if not band_ids:
        raise HTTPException(400, "No bands found for this festival")

    year = (festival.get("start_date") or "")[:4] or str(datetime.now(timezone.utc).year)
    playlist_name = f"{festival['name']} {year}"

    try:
        url = build_playlist(band_ids=band_ids, playlist_name=playlist_name, tracks_per_band=1)
    except Exception as exc:
        log.exception("Festival playlist build failed")
        raise HTTPException(500, str(exc))

    return {"playlist_url": url, "band_count": len(band_ids), "playlist_name": playlist_name}


# ── Spotify OAuth ─────────────────────────────────────────────────────────────

def _spotify_oauth_manager():
    from spotipy.oauth2 import SpotifyOAuth
    from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI, SPOTIFY_SCOPE
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=".spotify_cache",
        open_browser=False,
    )


@app.get("/spotify/auth")
async def spotify_auth():
    """Redirect the user to Spotify's login page to complete OAuth."""
    auth = _spotify_oauth_manager()
    auth_url = auth.get_authorize_url()
    return RedirectResponse(auth_url)


@app.get("/callback")
async def spotify_callback(code: str = "", error: str = ""):
    """Spotify OAuth callback — exchanges the code for a token and caches it."""
    if error:
        return HTMLResponse(
            f"<p>Spotify auth failed: {error}. <a href='/'>Back</a></p>", status_code=400
        )
    if not code:
        return HTMLResponse("<p>No code received from Spotify.</p>", status_code=400)
    auth = _spotify_oauth_manager()
    auth.get_access_token(code, as_dict=False)
    return RedirectResponse("/?spotify=connected")


@app.get("/spotify/status")
async def spotify_status():
    """Return whether a valid Spotify token is cached."""
    try:
        auth = _spotify_oauth_manager()
        token = auth.get_cached_token()
        if token and not auth.is_token_expired(token):
            return {"connected": True}
    except Exception:
        pass
    return {"connected": False}


# ── Festival CRUD ────────────────────────────────────────────────────────────

@app.delete("/festivals/{festival_id}")
async def delete_festival(festival_id: str):
    found = queries.delete_festival(festival_id)
    if not found:
        raise HTTPException(404, "Festival not found")
    return {"status": "ok"}


# ── Festival attendance & rename ─────────────────────────────────────────────

@app.post("/festivals/{festival_id}/attended")
async def set_festival_attended(festival_id: str, attended: str = Form(...)):
    if not queries.get_festival(festival_id):
        raise HTTPException(404, "Festival not found")
    queries.set_festival_attended(festival_id, attended.lower() == "true")
    return {"status": "ok"}


@app.post("/festivals/rename")
async def rename_festival(old_name: str = Form(...), new_name: str = Form(...)):
    new_name = new_name.strip()
    if not new_name:
        raise HTTPException(400, "New name cannot be empty")
    updated = queries.rename_festival_group(old_name, new_name)
    return {"status": "ok", "updated": updated}


@app.post("/festivals/location")
async def set_festival_location(name: str = Form(...), location: str = Form(...)):
    updated = queries.set_festival_group_location(name, location.strip())
    return {"status": "ok", "updated": updated}


# ── Liked Songs ───────────────────────────────────────────────────────────────

@app.post("/spotify/liked/sync")
async def liked_sync(background_tasks: BackgroundTasks):
    # Check auth before kicking off background task
    try:
        auth = _spotify_oauth_manager()
        token = auth.get_cached_token()
        if not token or auth.is_token_expired(token):
            return JSONResponse({"status": "auth_required"}, status_code=401)
    except Exception:
        return JSONResponse({"status": "auth_required"}, status_code=401)
    background_tasks.add_task(sync_liked_songs, True)
    return {"status": "started"}


@app.get("/spotify/liked/status")
async def liked_status():
    return get_sync_status()


# ── Discover (MusicFestivalWizard search) ─────────────────────────────────────

@app.post("/discover/search")
async def discover_search(use_rated: str = Form("true"), use_liked: str = Form("true")):
    """
    Build the artist search pool from rated (≥7) and/or liked bands, then hand off
    to the MFW scraper.  Scraping requires Playwright; returns a stub response until
    it is installed.
    """
    from ingestion.mfw_scraper import search_mfw, MfwNotAvailable

    include_rated = use_rated.lower() == "true"
    include_liked = use_liked.lower() == "true"

    search_pool: list[dict] = []
    seen: set[str] = set()

    if include_rated:
        rating_data: dict[str, dict] = {}
        for r in queries.get_all_ratings():
            bid = r.get("b.id", "")
            if bid and bid not in rating_data:
                rating_data[bid] = r
        for band in queries.list_bands():
            bid = band["id"]
            rating = rating_data.get(bid)
            if rating and rating.get("r.score") is not None:
                score = int(rating["r.score"])
                if score >= 7:
                    name = band.get("name", "")
                    search_pool.append({"name": name, "score": score, "source": "rated"})
                    seen.add(name.lower())

    if include_liked:
        for name in queries.get_liked_artist_names():
            display = name.title()
            if name not in seen:
                search_pool.append({"name": display, "score": None, "source": "liked"})

    try:
        results = await search_mfw(search_pool)
        return {"status": "ok", "pool_size": len(search_pool), "results": results}
    except MfwNotAvailable as exc:
        return {
            "status": "not_implemented",
            "message": str(exc),
            "pool_size": len(search_pool),
            "search_pool": search_pool,
        }
    except Exception as exc:
        log.exception("MFW discover search failed")
        raise HTTPException(500, str(exc))


@app.post("/discover/ingest")
async def discover_ingest_poster(poster_url: str = Form(...), source_url: str = Form("")):
    """Ingest a poster image discovered from MusicFestivalWizard."""
    try:
        r = ingest_poster(poster_url, source_url=source_url or poster_url)
        return {
            "festival_name": r.festival_name,
            "band_count": len(r.band_ids),
            "bands_added": r.bands_added,
            "bands_merged": r.bands_merged,
            "reimport": r.reimport,
        }
    except Exception as exc:
        log.exception("MFW poster ingest failed for %s", poster_url)
        raise HTTPException(500, str(exc))


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_json_list(value: str) -> list:
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return [value] if value else []
