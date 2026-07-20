"""Rule-based quality-profile assignment.

The user describes which movies belong on which archive profile as standing
rules (genre / collection / tag -> profile). The curator keeps Radarr in sync so
a new kids movie lands on Archive-HD without a manual bulk edit -- and the same
rules tell the free-space loop to keep its hands off those titles, so the two
never fight over the same movie.
"""
from __future__ import annotations

from . import db
from .arr import Radarr, radarr_from_env


def _profile_ids_by_name(radarr: Radarr) -> dict[str, int]:
    return {p["name"].lower(): int(p["id"]) for p in radarr.quality_profiles()}


def _tag_ids_by_label(radarr: Radarr) -> dict[str, int]:
    try:
        return {t["label"].lower(): int(t["id"]) for t in radarr.tags()}
    except Exception:  # noqa: BLE001
        return {}


def movie_matches(movie: dict, match_type: str, match_value: str,
                  tag_ids: dict[str, int]) -> bool:
    mv = str(match_value).strip().lower()
    if match_type == "genre":
        return any(str(g).lower() == mv for g in (movie.get("genres") or []))
    if match_type == "collection":
        col = (movie.get("collection") or {}).get("title") or ""
        return col.strip().lower() == mv
    if match_type == "tag":
        tid = tag_ids.get(mv)
        return tid is not None and tid in (movie.get("tags") or [])
    return False


def rule_target_profile_ids(radarr: Radarr) -> set[int]:
    """Profile ids that any enabled rule assigns to (the grab-managed set)."""
    names = _profile_ids_by_name(radarr)
    out: set[int] = set()
    for r in db.rules_enabled():
        pid = names.get(str(r["profile_name"]).lower())
        if pid is not None:
            out.add(pid)
    return out


def rule_managed_movie_ids(radarr: Radarr, movies: list[dict]) -> set[int]:
    """Movie ids that match any enabled rule -- excluded from the 4K loop even
    if not yet reassigned, so run order can never let the loop grab them."""
    rules = db.rules_enabled()
    if not rules:
        return set()
    tag_ids = _tag_ids_by_label(radarr)
    managed: set[int] = set()
    for m in movies:
        for r in rules:
            if movie_matches(m, r["match_type"], r["match_value"], tag_ids):
                managed.add(int(m["id"]))
                break
    return managed


def apply_profile_rules(radarr: Radarr | None = None, dry: bool = True,
                        protected_ids: set[int] | None = None) -> dict:
    """Switch every movie to the profile its first matching rule names.

    First matching rule wins. Movies already on the target profile are left
    alone (no churn). Blocklisted / keep-tagged titles are never reassigned --
    a classic that is also a kids movie stays protected. Returns counts per rule.
    """
    radarr = radarr or radarr_from_env()
    protected_ids = protected_ids or set()
    rules = db.rules_enabled()
    if not rules:
        return {"assigned": 0, "rules": 0, "by_rule": {}, "unknown_profiles": []}

    prof_ids = _profile_ids_by_name(radarr)
    tag_ids = _tag_ids_by_label(radarr)
    unknown = sorted({r["profile_name"] for r in rules
                      if str(r["profile_name"]).lower() not in prof_ids})

    assigned = 0
    by_rule: dict[str, int] = {}
    for m in radarr.movies():
        if int(m.get("id", -1)) in protected_ids:
            continue
        for r in rules:
            target = prof_ids.get(str(r["profile_name"]).lower())
            if target is None:
                continue
            if m.get("qualityProfileId") == target:
                break  # already correct; first-match-wins stops here
            if movie_matches(m, r["match_type"], r["match_value"], tag_ids):
                if not dry:
                    m["qualityProfileId"] = target
                    try:
                        radarr.update_movie(m)
                    except Exception:  # noqa: BLE001
                        break
                assigned += 1
                key = f"{r['match_type']}={r['match_value']} -> {r['profile_name']}"
                by_rule[key] = by_rule.get(key, 0) + 1
                break

    db.log_run("assign", True,
               f"{'DRY: ' if dry else ''}assigned {assigned} movies via "
               f"{len(rules)} rule(s)")
    return {"assigned": assigned, "rules": len(rules), "by_rule": by_rule,
            "unknown_profiles": unknown}
