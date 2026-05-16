"""
Kuzu graph schema definitions.

Node tables:
  Band, Festival, Concert, Person

Relationship tables:
  PLAYED_AT, SCHEDULED_FOR, PERFORMED_AT, INFLUENCED_BY, RATED
"""

SCHEMA_DDL = [
    # ── Node tables ──────────────────────────────────────────────
    """CREATE NODE TABLE IF NOT EXISTS Band (
        id          STRING PRIMARY KEY,
        name        STRING,
        name_lower  STRING,
        genres      STRING,
        influences  STRING,
        youtube_links STRING,
        spotify_id  STRING,
        notes       STRING,
        bubble_status STRING,
        created_at  STRING,
        updated_at  STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS Festival (
        id          STRING PRIMARY KEY,
        name        STRING,
        location    STRING,
        start_date  STRING,
        end_date    STRING,
        source_url  STRING,
        poster_path STRING,
        created_at  STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS Concert (
        id      STRING PRIMARY KEY,
        venue   STRING,
        city    STRING,
        date    STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS Person (
        id   STRING PRIMARY KEY,
        name STRING
    )""",

    # ── Relationship tables ──────────────────────────────────────
    """CREATE REL TABLE IF NOT EXISTS PLAYED_AT (
        FROM Band TO Festival,
        rank         DOUBLE,
        alphabetical BOOL,
        timestamp    STRING,
        day          STRING
    )""",

    """CREATE REL TABLE IF NOT EXISTS SCHEDULED_FOR (
        FROM Band TO Festival,
        rank         DOUBLE,
        alphabetical BOOL,
        timestamp    STRING,
        day          STRING
    )""",

    """CREATE REL TABLE IF NOT EXISTS PERFORMED_AT (
        FROM Band TO Concert,
        date STRING
    )""",

    """CREATE REL TABLE IF NOT EXISTS INFLUENCED_BY (
        FROM Band TO Band,
        label STRING
    )""",

    """CREATE REL TABLE IF NOT EXISTS RATED (
        FROM Person TO Band,
        score     INT64,
        interest  STRING,
        notes     STRING,
        timestamp STRING
    )""",

    # Liked Songs artists — persists across app restarts
    """CREATE NODE TABLE IF NOT EXISTS LikedArtist (
        spotify_artist_id STRING PRIMARY KEY,
        name              STRING,
        name_lower        STRING,
        fetched_at        STRING
    )""",

    # ── Migrations (fail silently on existing DBs) ───────────────────────
    "ALTER TABLE PLAYED_AT ADD day STRING",
    "ALTER TABLE SCHEDULED_FOR ADD day STRING",
    "ALTER TABLE Festival ADD attended BOOL",
]
