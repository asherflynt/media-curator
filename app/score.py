"""Candidate selection and ranking for demotion.

Two distinct mechanisms, deliberately not blended:

  * HARD FILTERS decide eligibility. Nothing outweighs them.
  * WEIGHTS decide order among the eligible.

The new-release window is a hard filter rather than a weight because a 90 GB
new remux scores so high on impact that any survivable penalty would still let
it reach the top of the queue.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

GB = 1024 ** 3
MONTH_SECONDS = 30.44 * 24 * 3600


def _parse_date(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def home_release_ts(movie: dict) -> tuple[float | None, str]:
    """When the film became watchable at home.

    'Hasn't got to it yet' depends on availability, not a calendar year, so the
    earliest home release wins; cinema date and year are fallbacks only.
    Radarr populates digitalRelease on 639/649 and physicalRelease on 634/649
    of the remux pool, so the fallbacks are rare.
    """
    digital = _parse_date(movie.get("digitalRelease"))
    physical = _parse_date(movie.get("physicalRelease"))
    candidates = [t for t in (digital, physical) if t]
    if candidates:
        return min(candidates), "home"
    cinema = _parse_date(movie.get("inCinemas"))
    if cinema:
        return cinema, "cinema"
    year = movie.get("year")
    if year:
        try:
            return datetime(int(year), 1, 1, tzinfo=timezone.utc).timestamp(), "year"
        except (ValueError, TypeError):
            pass
    return None, "unknown"


@dataclass
class Candidate:
    movie: dict
    size: int
    tier: str
    tmdb: str | None
    release_ts: float | None
    release_basis: str
    reclaim: int
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0

    @property
    def id(self) -> int:
        return int(self.movie["id"])

    @property
    def title(self) -> str:
        return str(self.movie.get("title", "?"))


def eligible(movies: list[dict], settings: dict,
             exclusion_tag_id: int | None,
             expected_target_bytes: int,
             blocklist_ids: set[int] | None = None,
             archive_profile_id: int | None = None,
             managed_ids: set[int] | None = None
             ) -> tuple[list[Candidate], dict[str, int]]:
    """Apply hard filters. Returns (candidates, rejection counts)."""
    source_tiers = set(settings.get("source_tiers") or ["Remux-2160p"])
    window_months = float(settings.get("new_release_window_months", 24))
    window_seconds = window_months * MONTH_SECONDS
    blocklist_ids = blocklist_ids or set()
    managed_ids = managed_ids or set()
    now = time.time()

    rejected = {"no_file": 0, "wrong_tier": 0, "rule_managed": 0,
                "already_archived": 0, "blocklisted": 0, "excluded_tag": 0,
                "inside_new_release_window": 0, "no_gain": 0}
    out: list[Candidate] = []

    for m in movies:
        mf = m.get("movieFile")
        if not mf:
            rejected["no_file"] += 1
            continue

        tier = ((mf.get("quality") or {}).get("quality") or {}).get("name") or "?"
        if tier not in source_tiers:
            rejected["wrong_tier"] += 1
            continue

        # Claimed by a profile-assignment rule or the HD track -- not the space
        # loop's to touch.
        if int(m.get("id", -1)) in managed_ids:
            rejected["rule_managed"] += 1
            continue

        # Already on the archive profile: either imported (and would be caught
        # by the tier check anyway) or mid-flight/failed with the remux still on
        # disk. Excluding it stops the loop re-grabbing a title it's already
        # working -- the source of duplicate downloads on slow torrents.
        if archive_profile_id is not None and \
                m.get("qualityProfileId") == archive_profile_id:
            rejected["already_archived"] += 1
            continue

        # Blocklist: an explicit "never demote this", managed in the app UI.
        if int(m.get("id", -1)) in blocklist_ids:
            rejected["blocklisted"] += 1
            continue

        if exclusion_tag_id is not None and exclusion_tag_id in (m.get("tags") or []):
            rejected["excluded_tag"] += 1
            continue

        rel_ts, basis = home_release_ts(m)
        # Unknown release date is treated as inside the window: refusing to
        # demote something we can't date is the conservative direction.
        if rel_ts is None or (now - rel_ts) < window_seconds:
            rejected["inside_new_release_window"] += 1
            continue

        size = int(mf.get("size") or 0)
        reclaim = size - expected_target_bytes
        if reclaim <= 0:
            rejected["no_gain"] += 1
            continue

        tmdb = str(m.get("tmdbId")) if m.get("tmdbId") else None
        out.append(Candidate(
            movie=m, size=size, tier=tier, tmdb=tmdb,
            release_ts=rel_ts, release_basis=basis, reclaim=reclaim,
        ))
    return out, rejected


def _norm(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank(candidates: list[Candidate], settings: dict) -> list[Candidate]:
    if not candidates:
        return []
    now = time.time()

    w_impact = float(settings.get("w_impact", 1.0))
    w_age = float(settings.get("w_age", 0.5))

    impact = _norm([float(c.reclaim) for c in candidates])
    age = _norm([now - (c.release_ts or now) for c in candidates])

    total_w = w_impact + w_age or 1.0
    for i, c in enumerate(candidates):
        c.components = {
            "impact": impact[i],
            "age": age[i],
        }
        c.score = (w_impact * impact[i] + w_age * age[i]) / total_w

    return sorted(candidates, key=lambda c: -c.score)


def select_for_deficit(ranked: list[Candidate], deficit_bytes: int,
                       batch_size: int) -> list[Candidate]:
    """Take just enough candidates to cover the deficit.

    Never queue more than needed: the Recycle Bin means reclaimed bytes don't
    appear as free space immediately, and a loop that keeps going until the
    number moves would drain the whole remux library chasing a target it had
    already met.
    """
    chosen: list[Candidate] = []
    projected = 0
    for c in ranked:
        if projected >= deficit_bytes or len(chosen) >= batch_size:
            break
        chosen.append(c)
        projected += c.reclaim
    return chosen
