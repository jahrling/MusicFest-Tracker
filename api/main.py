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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from config import BASE_DIR, POSTERS_DIR, RESEARCH_RANK_THRESHOLD, ANTHROPIC_API_KEY, VISION_BACKEND

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
    bands = queries.list_bands_with_festivals(limit=2000)

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

    # Group festivals by name for the panel display
    festival_groups: dict[str, list] = defaultdict(list)
    for f in festivals:
        festival_groups[f.get("name", "")].append(f)
    festivals_grouped = [
        {
            "name": name,
            "entries": sorted(entries, key=lambda f: f.get("start_date", ""), reverse=True),
        }
        for name, entries in sorted(festival_groups.items(), key=lambda x: x[0].lower())
    ]

    liked_count = queries.count_liked_artists()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "festivals": festivals,
            "festivals_grouped": festivals_grouped,
            "bands": bands,
            "liked_count": liked_count,
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
async def ingest_from_url(urls: str = Form(...)):
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    if not url_list:
        raise HTTPException(400, "No URLs provided")
    results = []
    for url in url_list:
        try:
            r = ingest_poster(url, source_url=url)
            results.append({
                "source": url,
                "festival_name": r.festival_name,
                "band_count": len(r.band_ids),
                "bands_added": r.bands_added,
                "bands_merged": r.bands_merged,
                "alphabetical": r.alphabetical,
                "reimport": r.reimport,
            })
        except Exception as exc:
            log.exception("Ingestion failed for URL %s", url)
            results.append({"source": url, "error": str(exc)})
    return {"results": results}


@app.post("/ingest/upload")
async def ingest_from_upload(files: list[UploadFile] = File(...)):
    results = []
    for file in files:
        suffix = Path(file.filename or "poster.jpg").suffix or ".jpg"
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, dir=POSTERS_DIR
        ) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        try:
            r = ingest_poster(tmp_path)
            results.append({
                "source": file.filename,
                "festival_name": r.festival_name,
                "band_count": len(r.band_ids),
                "bands_added": r.bands_added,
                "bands_merged": r.bands_merged,
                "alphabetical": r.alphabetical,
                "reimport": r.reimport,
            })
        except Exception as exc:
            log.exception("Ingestion failed for %s", file.filename)
            tmp_path.unlink(missing_ok=True)
            results.append({"source": file.filename, "error": str(exc)})
    return {"results": results}


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


# ── Liked Songs ───────────────────────────────────────────────────────────────

@app.post("/spotify/liked/sync")
async def liked_sync(background_tasks: BackgroundTasks):
    background_tasks.add_task(sync_liked_songs, True)
    return {"status": "started"}


@app.get("/spotify/liked/status")
async def liked_status():
    return get_sync_status()


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_json_list(value: str) -> list:
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return [value] if value else []
