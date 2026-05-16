"""Sync Spotify Liked Songs artists into the graph's LikedArtist table."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPE,
    SYNC_META_PATH,
)
from graph import queries

log = logging.getLogger("festival_tracker.liked_songs")


def _get_client() -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=".spotify_cache",
    )
    return spotipy.Spotify(auth_manager=auth)


def _read_meta() -> dict:
    try:
        return json.loads(Path(SYNC_META_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_meta(data: dict) -> None:
    Path(SYNC_META_PATH).write_text(json.dumps(data, indent=2))


def get_sync_status() -> dict:
    meta = _read_meta()
    return {
        "count": queries.count_liked_artists(),
        "last_sync": meta.get("liked_last_sync"),
        "is_syncing": meta.get("liked_is_syncing", False),
    }


def sync_liked_songs(full_sync: bool = True) -> dict:
    """
    Paginate through Spotify Liked Songs and store unique artists in the graph.

    full_sync=True clears existing LikedArtist nodes before importing.
    Returns {"success": bool, "count": int, "last_sync": str|None}.
    """
    meta = _read_meta()
    meta["liked_is_syncing"] = True
    _write_meta(meta)

    try:
        sp = _get_client()

        if full_sync:
            queries.clear_liked_artists()

        seen: set[str] = set()
        offset = 0

        while True:
            result = sp.current_user_saved_tracks(limit=50, offset=offset)
            items = result.get("items", [])
            if not items:
                break

            for item in items:
                track = (item or {}).get("track")
                if not track:
                    continue
                artists = track.get("artists", [])
                if not artists:
                    continue
                artist = artists[0]
                artist_id = artist.get("id")
                artist_name = artist.get("name", "")
                if artist_id and artist_id not in seen:
                    seen.add(artist_id)
                    queries.upsert_liked_artist(artist_id, artist_name)

            if not result.get("next"):
                break
            offset += 50

        now = datetime.now(timezone.utc).isoformat()
        meta["liked_last_sync"] = now
        meta["liked_is_syncing"] = False
        _write_meta(meta)
        log.info("Liked songs sync complete — %d unique artists", len(seen))
        return {"success": True, "count": len(seen), "last_sync": now}

    except Exception:
        log.exception("Liked songs sync failed")
        meta["liked_is_syncing"] = False
        _write_meta(meta)
        return {"success": False, "count": 0, "last_sync": None}
