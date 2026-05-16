"""Graph read/write helpers for all node and relationship types."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import kuzu

from config import BAND_MERGE_SIMILARITY_THRESHOLD
from graph.db import get_connection


# ── helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _row_to_dict(result: kuzu.QueryResult) -> list[dict[str, Any]]:
    rows = []
    col_names = result.get_column_names()
    while result.has_next():
        row = result.get_next()
        rows.append(dict(zip(col_names, row)))
    return rows


# ── Band ─────────────────────────────────────────────────────────────────────

def find_band_by_name(name: str) -> dict | None:
    """Return existing Band if name matches with ≥ threshold similarity."""
    conn = get_connection()
    res = conn.execute("MATCH (b:Band) RETURN b.id, b.name, b.name_lower")
    rows = _row_to_dict(res)
    best: dict | None = None
    best_score = 0.0
    for row in rows:
        score = _similarity(name, row["b.name"])
        if score > best_score:
            best_score = score
            best = row
    if best and best_score >= BAND_MERGE_SIMILARITY_THRESHOLD:
        return get_band(best["b.id"])
    return None


def get_band(band_id: str) -> dict | None:
    conn = get_connection()
    res = conn.execute(
        "MATCH (b:Band {id: $id}) RETURN b.*",
        {"id": band_id},
    )
    rows = _row_to_dict(res)
    if not rows:
        return None
    row = rows[0]
    return {k.replace("b.", ""): v for k, v in row.items()}


def upsert_band(name: str, **kwargs) -> str:
    """Find or create a Band node. Returns band id."""
    existing = find_band_by_name(name)
    if existing:
        band_id = existing["id"]
        # Update mutable fields if provided
        updates = {k: v for k, v in kwargs.items() if v is not None}
        if updates:
            set_clause = ", ".join(f"b.{k} = ${k}" for k in updates)
            updates["id"] = band_id
            updates["updated_at"] = _now()
            set_clause += ", b.updated_at = $updated_at"
            conn = get_connection()
            conn.execute(f"MATCH (b:Band {{id: $id}}) SET {set_clause}", updates)
        return band_id

    conn = get_connection()
    band_id = _uid()
    now = _now()
    params = {
        "id": band_id,
        "name": name,
        "name_lower": name.lower(),
        "genres": kwargs.get("genres", ""),
        "influences": kwargs.get("influences", ""),
        "youtube_links": kwargs.get("youtube_links", ""),
        "spotify_id": kwargs.get("spotify_id", ""),
        "notes": kwargs.get("notes", ""),
        "bubble_status": kwargs.get("bubble_status", ""),
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        """CREATE (:Band {
            id: $id, name: $name, name_lower: $name_lower,
            genres: $genres, influences: $influences,
            youtube_links: $youtube_links, spotify_id: $spotify_id,
            notes: $notes, bubble_status: $bubble_status,
            created_at: $created_at, updated_at: $updated_at
        })""",
        params,
    )
    return band_id


def list_bands(limit: int = 500) -> list[dict]:
    conn = get_connection()
    res = conn.execute(f"MATCH (b:Band) RETURN b.* LIMIT {limit}")
    rows = _row_to_dict(res)
    return [{k.replace("b.", ""): v for k, v in r.items()} for r in rows]


# ── Festival ─────────────────────────────────────────────────────────────────

def upsert_festival(
    name: str,
    location: str = "",
    start_date: str = "",
    end_date: str = "",
    source_url: str = "",
    poster_path: str = "",
) -> str:
    conn = get_connection()
    # Unique by (name, start_date) so the same festival in different years gets separate nodes
    res = conn.execute(
        "MATCH (f:Festival) WHERE f.name = $name AND f.start_date = $date RETURN f.id",
        {"name": name, "date": start_date or ""},
    )
    rows = _row_to_dict(res)
    if rows:
        return rows[0]["f.id"]

    festival_id = _uid()
    conn.execute(
        """CREATE (:Festival {
            id: $id, name: $name, location: $location,
            start_date: $start_date, end_date: $end_date,
            source_url: $source_url, poster_path: $poster_path,
            created_at: $created_at
        })""",
        {
            "id": festival_id,
            "name": name,
            "location": location,
            "start_date": start_date,
            "end_date": end_date,
            "source_url": source_url,
            "poster_path": poster_path,
            "created_at": _now(),
        },
    )
    return festival_id


def get_festival(festival_id: str) -> dict | None:
    conn = get_connection()
    res = conn.execute(
        "MATCH (f:Festival {id: $id}) RETURN f.*",
        {"id": festival_id},
    )
    rows = _row_to_dict(res)
    if not rows:
        return None
    row = rows[0]
    return {k.replace("f.", ""): v for k, v in row.items()}


def list_festivals() -> list[dict]:
    conn = get_connection()
    res = conn.execute("MATCH (f:Festival) RETURN f.* ORDER BY f.name ASC, f.start_date DESC")
    rows = _row_to_dict(res)
    return [{k.replace("f.", ""): v for k, v in r.items()} for r in rows]


def set_festival_attended(festival_id: str, attended: bool) -> None:
    conn = get_connection()
    conn.execute(
        "MATCH (f:Festival {id: $id}) SET f.attended = $attended",
        {"id": festival_id, "attended": attended},
    )


def rename_festival_group(old_name: str, new_name: str) -> int:
    """Rename every Festival node whose name == old_name. Returns count updated."""
    conn = get_connection()
    res = conn.execute(
        "MATCH (f:Festival) WHERE f.name = $old RETURN f.id",
        {"old": old_name},
    )
    ids = [row["f.id"] for row in _row_to_dict(res)]
    for fid in ids:
        conn.execute(
            "MATCH (f:Festival {id: $id}) SET f.name = $name",
            {"id": fid, "name": new_name},
        )
    return len(ids)


def set_festival_group_location(name: str, location: str) -> int:
    """Set location on every Festival node whose name == name. Returns count updated."""
    conn = get_connection()
    res = conn.execute(
        "MATCH (f:Festival) WHERE f.name = $name RETURN f.id",
        {"name": name},
    )
    ids = [row["f.id"] for row in _row_to_dict(res)]
    for fid in ids:
        conn.execute(
            "MATCH (f:Festival {id: $id}) SET f.location = $location",
            {"id": fid, "location": location},
        )
    return len(ids)


def get_band_global_ranks() -> dict[str, float]:
    """
    For each band, return its normalized rank (0–100) from its most recent festival.

    Bands in alphabetical-order lineups get rank 50.0.
    Bands with no festival data get rank 0.0.
    """
    conn = get_connection()
    best: dict[str, tuple[str, float]] = {}  # band_id → (latest_date, rank)

    for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
        res = conn.execute(
            f"MATCH (b:Band)-[r:{rel_type}]->(f:Festival) "
            f"RETURN b.id AS bid, f.start_date AS dt, r.rank AS rnk, r.alphabetical AS alpha"
        )
        for row in _row_to_dict(res):
            bid = row.get("bid", "")
            if not bid:
                continue
            dt = row.get("dt") or ""
            existing = best.get(bid)
            if existing and existing[0] >= dt:
                continue  # Already have an equal or newer entry
            rnk = row.get("rnk")
            alpha = row.get("alpha")
            if alpha:
                rank_val = 50.0
            elif rnk is not None and float(rnk) > 0:
                rank_val = float(rnk)
            else:
                rank_val = 1.0
            best[bid] = (dt, rank_val)

    return {bid: v[1] for bid, v in best.items()}


# ── PLAYED_AT / SCHEDULED_FOR ────────────────────────────────────────────────

def link_band_to_festival(
    band_id: str,
    festival_id: str,
    rank: float | None,
    alphabetical: bool,
    rel_type: str = "PLAYED_AT",
    day: str = "",
) -> None:
    conn = get_connection()
    # Idempotency: skip if this exact (band, festival, rel_type) edge already exists
    check = conn.execute(
        f"MATCH (b:Band {{id: $bid}})-[:{rel_type}]->(f:Festival {{id: $fid}}) RETURN 1 LIMIT 1",
        {"bid": band_id, "fid": festival_id},
    )
    if _row_to_dict(check):
        return
    params = {
        "band_id": band_id,
        "festival_id": festival_id,
        "rank": rank if rank is not None else -1.0,
        "alphabetical": alphabetical,
        "timestamp": _now(),
        "day": day or "",
    }
    conn.execute(
        f"""MATCH (b:Band {{id: $band_id}}), (f:Festival {{id: $festival_id}})
            CREATE (b)-[:{rel_type} {{
                rank: $rank, alphabetical: $alphabetical,
                timestamp: $timestamp, day: $day
            }}]->(f)""",
        params,
    )


def dedup_graph() -> dict:
    """
    1. Merge festival nodes that share the same name (keep oldest, redirect edges).
    2. Remove duplicate band-festival edges for the same (band, festival) pair.
    Returns counts of what was fixed.
    """
    from collections import defaultdict

    conn = get_connection()
    results = {"festivals_merged": 0, "edges_removed": 0}

    # ── 1. Merge duplicate festival nodes ───────────────────────────────────
    festivals = list_festivals()
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in festivals:
        key = (f.get("name") or "").lower().strip()
        if key:
            groups[key].append(f)

    for group in groups.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda f: f.get("created_at") or "")
        canonical = group[0]
        for dup in group[1:]:
            for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
                res = conn.execute(
                    f"MATCH (b:Band)-[r:{rel_type}]->(f:Festival {{id: $fid}}) "
                    f"RETURN b.id AS bid, r.rank AS rank, r.alphabetical AS alpha, r.timestamp AS ts",
                    {"fid": dup["id"]},
                )
                for edge in _row_to_dict(res):
                    # link_band_to_festival is now idempotent — safe to call unconditionally
                    link_band_to_festival(
                        edge["bid"], canonical["id"],
                        edge.get("rank"), bool(edge.get("alpha")), rel_type,
                    )
                conn.execute(
                    f"MATCH (b:Band)-[r:{rel_type}]->(f:Festival {{id: $fid}}) DELETE r",
                    {"fid": dup["id"]},
                )
            conn.execute("MATCH (f:Festival {id: $fid}) DELETE f", {"fid": dup["id"]})
            results["festivals_merged"] += 1

    # ── 2. Remove duplicate band-festival edges ──────────────────────────────
    for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
        res = conn.execute(
            f"MATCH (b:Band)-[r:{rel_type}]->(f:Festival) "
            f"RETURN b.id AS bid, f.id AS fid, r.rank AS rank, r.alphabetical AS alpha, r.timestamp AS ts"
        )
        all_edges = _row_to_dict(res)

        seen: set[tuple[str, str]] = set()
        dup_pairs: set[tuple[str, str]] = set()
        for e in all_edges:
            pair = (e.get("bid", ""), e.get("fid", ""))
            if pair in seen:
                dup_pairs.add(pair)
            seen.add(pair)

        for (bid, fid) in dup_pairs:
            pair_edges = [e for e in all_edges if e.get("bid") == bid and e.get("fid") == fid]
            pair_edges.sort(key=lambda e: e.get("ts") or "", reverse=True)
            best = pair_edges[0]
            conn.execute(
                f"MATCH (b:Band {{id: $bid}})-[r:{rel_type}]->(f:Festival {{id: $fid}}) DELETE r",
                {"bid": bid, "fid": fid},
            )
            link_band_to_festival(bid, fid, best.get("rank"), bool(best.get("alpha")), rel_type)
            results["edges_removed"] += len(pair_edges) - 1

    return results


def count_festival_bands(festival_id: str) -> int:
    """Return how many bands are currently linked to a festival (any rel type)."""
    conn = get_connection()
    total = 0
    for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
        res = conn.execute(
            f"MATCH (b:Band)-[:{rel_type}]->(f:Festival {{id: $fid}}) RETURN b.id",
            {"fid": festival_id},
        )
        total += len(_row_to_dict(res))
    return total


def clear_festival_edges(festival_id: str) -> None:
    """Delete all PLAYED_AT and SCHEDULED_FOR edges pointing to this festival."""
    conn = get_connection()
    for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
        conn.execute(
            f"MATCH (b:Band)-[r:{rel_type}]->(f:Festival {{id: $fid}}) DELETE r",
            {"fid": festival_id},
        )


def get_band_festival_history(band_id: str) -> list[dict]:
    conn = get_connection()
    res = conn.execute(
        """MATCH (b:Band {id: $id})-[r:PLAYED_AT|SCHEDULED_FOR]->(f:Festival)
           RETURN f.name, f.start_date, f.location, r.rank, r.alphabetical,
                  r.timestamp, label(r) AS rel_type
           ORDER BY f.start_date""",
        {"id": band_id},
    )
    return _row_to_dict(res)


# ── Concert ───────────────────────────────────────────────────────────────────

def upsert_concert(venue: str, city: str, date: str) -> str:
    conn = get_connection()
    res = conn.execute(
        "MATCH (c:Concert) WHERE c.venue = $v AND c.city = $c AND c.date = $d RETURN c.id",
        {"v": venue, "c": city, "d": date},
    )
    rows = _row_to_dict(res)
    if rows:
        return rows[0]["c.id"]
    concert_id = _uid()
    conn.execute(
        "CREATE (:Concert {id: $id, venue: $v, city: $c, date: $d})",
        {"id": concert_id, "v": venue, "c": city, "d": date},
    )
    return concert_id


def link_band_to_concert(band_id: str, concert_id: str, date: str) -> None:
    conn = get_connection()
    conn.execute(
        """MATCH (b:Band {id: $bid}), (c:Concert {id: $cid})
           CREATE (b)-[:PERFORMED_AT {date: $date}]->(c)""",
        {"bid": band_id, "cid": concert_id, "date": date},
    )


# ── Ratings ───────────────────────────────────────────────────────────────────

def ensure_person(name: str = "default") -> str:
    conn = get_connection()
    res = conn.execute(
        "MATCH (p:Person {name: $name}) RETURN p.id",
        {"name": name},
    )
    rows = _row_to_dict(res)
    if rows:
        return rows[0]["p.id"]
    person_id = _uid()
    conn.execute(
        "CREATE (:Person {id: $id, name: $name})",
        {"id": person_id, "name": name},
    )
    return person_id


def upsert_rating(
    band_id: str,
    score: int,
    interest: str,
    notes: str = "",
    person_name: str = "default",
) -> None:
    person_id = ensure_person(person_name)
    conn = get_connection()
    conn.execute(
        """MATCH (p:Person {id: $pid}), (b:Band {id: $bid})
           CREATE (p)-[:RATED {
               score: $score, interest: $interest,
               notes: $notes, timestamp: $ts
           }]->(b)""",
        {
            "pid": person_id,
            "bid": band_id,
            "score": score,
            "interest": interest,
            "notes": notes,
            "ts": _now(),
        },
    )


def get_latest_rating(band_id: str, person_name: str = "default") -> dict | None:
    conn = get_connection()
    res = conn.execute(
        """MATCH (p:Person {name: $pname})-[r:RATED]->(b:Band {id: $bid})
           RETURN r.score, r.interest, r.notes, r.timestamp
           ORDER BY r.timestamp DESC LIMIT 1""",
        {"pname": person_name, "bid": band_id},
    )
    rows = _row_to_dict(res)
    return rows[0] if rows else None


def get_all_ratings(person_name: str = "default") -> list[dict]:
    conn = get_connection()
    res = conn.execute(
        """MATCH (p:Person {name: $pname})-[r:RATED]->(b:Band)
           RETURN b.id, b.name, r.score, r.interest, r.notes, r.timestamp
           ORDER BY r.timestamp DESC""",
        {"pname": person_name},
    )
    return _row_to_dict(res)


# ── Band + festival enrichment ────────────────────────────────────────────────

def list_bands_with_festivals(limit: int = 2000) -> list[dict]:
    """Return all bands annotated with their list of festival_ids."""
    bands = list_bands(limit=limit)
    if not bands:
        return bands

    conn = get_connection()
    fmap: dict[str, list[str]] = {}
    for rel_type in ("PLAYED_AT", "SCHEDULED_FOR"):
        res = conn.execute(
            f"MATCH (b:Band)-[:{rel_type}]->(f:Festival) RETURN b.id, f.id"
        )
        for row in _row_to_dict(res):
            bid = row["b.id"]
            fid = row["f.id"]
            lst = fmap.setdefault(bid, [])
            if fid not in lst:
                lst.append(fid)

    for band in bands:
        band["festival_ids"] = fmap.get(band["id"], [])
    return bands


# ── LikedArtist ───────────────────────────────────────────────────────────────

def upsert_liked_artist(spotify_artist_id: str, name: str) -> None:
    conn = get_connection()
    res = conn.execute(
        "MATCH (a:LikedArtist {spotify_artist_id: $sid}) RETURN a.spotify_artist_id",
        {"sid": spotify_artist_id},
    )
    if _row_to_dict(res):
        conn.execute(
            """MATCH (a:LikedArtist {spotify_artist_id: $sid})
               SET a.name = $name, a.name_lower = $nl, a.fetched_at = $ts""",
            {"sid": spotify_artist_id, "name": name, "nl": name.lower(), "ts": _now()},
        )
    else:
        conn.execute(
            """CREATE (:LikedArtist {
                spotify_artist_id: $sid, name: $name,
                name_lower: $nl, fetched_at: $ts
            })""",
            {"sid": spotify_artist_id, "name": name, "nl": name.lower(), "ts": _now()},
        )


def get_liked_artist_names() -> set[str]:
    """Return set of lowercase artist names from Liked Songs."""
    conn = get_connection()
    res = conn.execute("MATCH (a:LikedArtist) RETURN a.name_lower")
    rows = _row_to_dict(res)
    return {r["a.name_lower"] for r in rows}


def count_liked_artists() -> int:
    conn = get_connection()
    res = conn.execute("MATCH (a:LikedArtist) RETURN count(a) AS cnt")
    rows = _row_to_dict(res)
    return rows[0]["cnt"] if rows else 0


def clear_liked_artists() -> None:
    conn = get_connection()
    conn.execute("MATCH (a:LikedArtist) DELETE a")


# ── Timeline export ──────────────────────────────────────────────────────────

def get_full_timeline() -> list[dict]:
    """All band-festival edges with band and festival metadata, sorted by date."""
    conn = get_connection()
    res = conn.execute(
        """MATCH (b:Band)-[r:PLAYED_AT|SCHEDULED_FOR]->(f:Festival)
           RETURN b.id, b.name, b.genres, b.bubble_status,
                  f.id, f.name, f.start_date, f.location,
                  r.rank, r.alphabetical, r.timestamp, label(r) AS rel_type
           ORDER BY f.start_date, r.rank DESC"""
    )
    return _row_to_dict(res)
