"""
Rating service — thin wrapper around graph queries for timestamped ratings.
"""

from __future__ import annotations

from graph import queries


VALID_INTEREST = {"skip", "curious", "interested", "must-see"}


def rate_band(
    band_id: str,
    score: int,
    interest: str,
    notes: str = "",
    person_name: str = "default",
) -> None:
    """
    Store a timestamped rating for a band.

    Args:
        band_id: Band node ID.
        score: 1–10.
        interest: one of skip / curious / interested / must-see.
        notes: optional free text.
        person_name: user identifier (default "default").
    """
    if not 1 <= score <= 10:
        raise ValueError(f"score must be 1-10, got {score}")
    if interest not in VALID_INTEREST:
        raise ValueError(f"interest must be one of {VALID_INTEREST}")
    queries.upsert_rating(
        band_id=band_id,
        score=score,
        interest=interest,
        notes=notes,
        person_name=person_name,
    )


def get_rating(band_id: str, person_name: str = "default") -> dict | None:
    return queries.get_latest_rating(band_id, person_name)


def all_ratings(person_name: str = "default") -> list[dict]:
    return queries.get_all_ratings(person_name)
