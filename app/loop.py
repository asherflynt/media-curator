"""The free-space control loop.

Reads free space from the filesystem, and if it's short of target, demotes down
the ranked candidate list until PROJECTED free space reaches target+hysteresis.

Projection is the whole game. Demoting doesn't free bytes immediately -- the old
remux sits in Radarr's Recycle Bin until retention expires, and the replacement
download occupies space in the meantime. A loop that waited for the raw statvfs
number to move would conclude nothing was happening and keep demoting, blowing
past the target and draining the remux library. So we count in-flight and
pending-reclaim bytes toward the projection and never queue more than the
deficit needs.
"""
from __future__ import annotations

import time

from . import assign, db, score, space
from .arr import Radarr, radarr_from_env
from .audit import archive_tier_median_bytes, cached_movies, inventory

GB = 1024 ** 3


class LoopAbort(RuntimeError):
    pass


def find_archive_profile(radarr: Radarr, name: str) -> dict | None:
    for p in radarr.quality_profiles():
        if p.get("name", "").lower() == name.lower():
            return p
    return None


def validate_archive_profile(profile: dict, archive_tier: str) -> dict:
    """Confirm the archive tier is the TOP-RANKED allowed quality.

    This is the assumption the entire design rests on. Radarr decides upgrades
    by profile ORDERING, not by which boxes are ticked: it rejects a lower
    quality when a higher-ranked one exists even if that one is unchecked. With
    the archive tier ranked top, nothing outranks it, so no release is ever an
    upgrade over it -- which is what stops Mediastarr re-grabbing the remux --
    and the WEB-DL *is* an upgrade over the existing remux, so Radarr's normal
    replace path applies.

    If anything allowed outranks the archive tier, demoted titles get
    re-upgraded straight back to remux. Fail loudly rather than discover that
    after 400 downloads.
    """
    buckets = _profile_buckets(profile)
    allowed = _profile_allowed(profile)

    if not allowed:
        return {"ok": False, "reason": "profile has no allowed qualities",
                "top": None, "allowed": allowed}

    # The highest-ranked ALLOWED item may be a quality group (Radarr's default
    # "WEB 2160p" holds WEBDL-2160p and WEBRip-2160p at equal rank). The archive
    # tier only has to be a member of that top bucket -- nothing outranks it.
    top_bucket = [b for b in buckets if b["allowed"]][-1]
    top = allowed[-1]  # Radarr orders ascending; last allowed ranks highest.
    if archive_tier.lower() not in {n.lower() for n in top_bucket["names"]}:
        top_label = top_bucket["label"] or top
        return {
            "ok": False,
            "top": top,
            "allowed": allowed,
            "reason": (
                f"'{top_label}' outranks '{archive_tier}' in the "
                f"{profile.get('name')} profile. Demoted titles will be "
                f"re-upgraded straight back. Drag {archive_tier} to the TOP of "
                f"the quality ordering."
            ),
        }

    # The cutoff is either a single quality's id or -- when it is a group -- the
    # group's own id (1000+), with no 'quality' object on the item.
    cutoff_ok = True
    cutoff_name = None
    cutoff = profile.get("cutoff")
    for b in buckets:
        if cutoff is not None and cutoff in b["ids"]:
            cutoff_name = b["label"]
            if archive_tier.lower() not in {n.lower() for n in b["names"]}:
                cutoff_ok = False
            break

    return {"ok": True, "top": top, "allowed": allowed, "cutoff": cutoff_name,
            "cutoff_ok": cutoff_ok, "reason": ""}


def recycle_bin_bytes(radarr: Radarr) -> int:
    """Bytes waiting in the Recycle Bin -- reclaimed but not yet free."""
    try:
        cfg = radarr.media_management()
        path = cfg.get("recycleBin")
        if not path:
            return 0
        local = space.arr_path_to_local(path)
        import os
        if not os.path.isdir(local):
            return 0
        return space.dir_size(local)
    except Exception:  # noqa: BLE001 - best effort; in-flight tracking is the
        return 0        # primary guard, this is a refinement.


def in_flight_bytes(radarr: Radarr) -> int:
    """Reclaim already committed but not yet reflected on disk.

    A manifest row flips to 'grabbed' the moment Radarr accepts the grab and
    is never revisited -- nothing marks it 'imported' once the swap lands.
    Restricting to movies still in Radarr's queue keeps this self-clearing:
    present while downloading or stuck on the upgrade rejection, gone the
    moment it imports or the grab fails -- the same signal _queued_movie_ids()
    uses for the rules/HD track.
    """
    queued = _queued_movie_ids(radarr)
    if not queued:
        return 0
    rows = db.conn().execute(
        "SELECT movie_id, old_size FROM manifest "
        "WHERE dry_run=0 AND status IN ('grabbed','pending')"
    ).fetchall()
    return sum(int(r["old_size"] or 0) for r in rows if r["movie_id"] in queued)


def _queued_movie_ids(radarr: Radarr) -> set[int]:
    """Movies with something in Radarr's download queue right now.

    The accurate 'in flight' signal: self-clearing when a download completes or
    fails, so a title isn't re-grabbed while it's already downloading, but a
    failed one becomes eligible again on a later pass.
    """
    try:
        return {int(r["movieId"]) for r in radarr.queue(include_movie=False)
                if r.get("movieId") is not None}
    except Exception:  # noqa: BLE001
        return set()


def status() -> dict:
    settings = db.all_settings()
    reading = space.read_space(space.media_path())
    target = float(settings["free_space_target_gb"]) * GB
    hyst = float(settings["hysteresis_gb"]) * GB

    pending = 0
    bin_bytes = 0
    try:
        radarr = radarr_from_env()
        pending = in_flight_bytes(radarr)
        bin_bytes = recycle_bin_bytes(radarr)
    except Exception:  # noqa: BLE001
        pass

    projected = reading.free_bytes + pending + bin_bytes
    deficit = max(0, int(target + hyst - projected))

    return {
        "free_bytes": reading.free_bytes,
        "total_bytes": reading.total_bytes,
        "free_gb": reading.free_gb,
        "total_gb": reading.total_gb,
        "target_gb": settings["free_space_target_gb"],
        "hysteresis_gb": settings["hysteresis_gb"],
        "in_flight_gb": pending / GB,
        "recycle_bin_gb": bin_bytes / GB,
        "projected_free_gb": projected / GB,
        "deficit_gb": deficit / GB,
        "below_target": reading.free_bytes < target,
        "action_needed": deficit > 0,
        "measured_from": space.media_path(),
    }


def build_candidates(settings: dict, radarr: Radarr) -> dict:
    """Rank candidates by impact and age. No watch signal is used."""
    # Movies only + cached: the ranking never needs TV, and re-pulling ~12k
    # episode files on every Candidates render was most of the page's latency.
    inv = inventory(radarr=radarr, use_cache=True, movies_only=True)
    target_bytes = archive_tier_median_bytes(inv["records"], settings["archive_tier"])

    excl_id = None
    try:
        for t in radarr.tags():
            if t["label"].lower() == str(settings["exclusion_tag"]).lower():
                excl_id = int(t["id"])
    except Exception:  # noqa: BLE001
        pass

    archive_profile = find_archive_profile(radarr, settings["archive_profile_name"])
    archive_profile_id = archive_profile["id"] if archive_profile else None

    # Hand off to the rules/HD track anything a rule claims (even if not yet
    # reassigned) or already sitting on a managed profile -- so the space loop
    # never grabs a title the kids track is meant to handle.
    managed_ids = assign.rule_managed_movie_ids(radarr, inv["movies"])
    managed_profile_ids = {p["id"] for p in managed_grab_profiles(radarr, settings)}
    for m in inv["movies"]:
        if m.get("qualityProfileId") in managed_profile_ids:
            managed_ids.add(int(m["id"]))

    cands, rejected = score.eligible(inv["movies"], settings, excl_id,
                                     target_bytes, db.blocklist_ids(),
                                     archive_profile_id, managed_ids)
    ranked = score.rank(cands, settings)
    return {"ranked": ranked, "rejected": rejected, "target_bytes": target_bytes}


# Rejection reasons that are PROFILE-RELATIVE: they hold only because the title
# is still on its original profile (where the existing remux meets cutoff / is
# preferred), and clear the moment it moves to Archive-4K. A release rejected
# ONLY for these is still a valid demotion target. Everything else (language,
# ignored terms, size, indexer flags) is a real reason to skip it.
_SOFT_REJECTION_MARKERS = (
    "meets cutoff",
    "not an upgrade",
    "not wanted in profile",
    "meets quality cutoff",
    "custom format cutoff",
    "existing file",
    "higher preference",  # "...has an equal or higher preference" (Radarr 5+)
    "higher preferred",   # older wording, kept for older Radarr builds
)


def _rejection_is_soft(reason: str) -> bool:
    r = reason.lower()
    return any(m in r for m in _SOFT_REJECTION_MARKERS)


def _tier_ladder(profile: dict, current_tier: str | None, depth: int) -> list[str]:
    """The tiers a demotion may target, best first.

    The top allowed quality is the goal, but insisting on it was the reason a
    title with no Bluray-1080p release simply stalled forever -- re-skipped on
    every pass, never demoted, never freeing a byte. So we walk DOWN the
    profile's own allowed list: every tier in it is one the user authored as
    acceptable for this archive profile, so a lower one is a worse outcome than
    the top tier but a far better one than keeping the 40 GB remux.

    Two guards. The ladder stops above the file's current tier, so a "demotion"
    can never grab something equal or better (a no-op that costs a download).
    And `depth` caps how far it falls -- depth 0 restores the old
    top-tier-or-nothing behaviour, so this is opt-out.
    """
    # One rung per allowed rank bucket, best first. A quality group is ONE rung
    # ("WEBDL-2160p|WEBRip-2160p"): Radarr ranks its members as equals, so the
    # ladder neither counts them as separate steps nor prefers one by position.
    rungs = [(i, b["names"]) for i, b in enumerate(_profile_buckets(profile))
             if b["allowed"] and b["names"]]
    rungs.reverse()
    if current_tier:
        cur = None
        for i, names in rungs:
            if current_tier.lower() in {n.lower() for n in names}:
                cur = i
        if cur is not None:
            # Strictly below the current file: an equal-rank peer is not a
            # demotion.
            rungs = [(i, names) for i, names in rungs if i < cur]
    return [TIER_SEP.join(names) for _, names in rungs][:max(1, int(depth) + 1)]


TIER_SEP = "|"


def rung_tiers(rung: str) -> list[str]:
    """The quality names on one ladder rung (one, or a group's members)."""
    return [t for t in rung.split(TIER_SEP) if t]


def _pick_release(radarr: Radarr, movie_id: int, tiers: list[str],
                  max_bytes: int | None = None) -> tuple[dict | None, str | None]:
    """Confirm a suitable release exists before touching anything.

    A release qualifies when its quality is one of `tiers` and its *only*
    rejections are profile-relative ones (the existing remux meeting cutoff),
    which is how every candidate looks while still on its original profile --
    that was the "no WEBDL-2160p release available" bug. Releases with a real
    rejection (wrong language, ignored terms, wrong size) are still skipped.

    `tiers` is ordered best-first and tried in order, so a lower tier is only
    reached when nothing better exists. Returns (release, tier_it_matched), or
    (None, None) when the whole ladder comes up empty -- then the title is
    skipped and nothing is touched.
    """
    try:
        releases = radarr.releases(movie_id)  # one indexer search for all tiers
    except Exception:  # noqa: BLE001
        return None, None

    for tier in tiers:
        # A grouped rung matches any member; Radarr's own scoring decides
        # between them below, as its DownloadDecisionComparer does.
        names = {t.lower() for t in rung_tiers(tier)}
        matches = []
        for r in releases:
            name = ((r.get("quality") or {}).get("quality") or {}).get("name", "")
            if name.lower() not in names:
                continue
            if all(_rejection_is_soft(x) for x in (r.get("rejections") or [])):
                # A replacement no smaller than what's on disk reclaims nothing;
                # dropping a tier is meant to save space, not just churn it.
                size = int(r.get("size") or 0)
                if max_bytes and size and size >= max_bytes:
                    continue
                matches.append(r)
        if matches:
            # Prefer Radarr's own scoring, then availability.
            matches.sort(key=lambda r: (-(r.get("customFormatScore") or 0),
                                        -(r.get("seeders") or 0)))
            best = matches[0]
            return best, ((best.get("quality") or {}).get("quality") or {}).get(
                "name") or tier
    return None, None


def _blocklist_on_repeated_failure(movie_id: int, title: str | None,
                                   limit: int) -> bool:
    """Circuit breaker for a grab that keeps failing. Most often the chosen
    release was pulled from the indexer (Newznab 300 "No such file"), so it can
    never succeed and would be re-selected every loop. After `limit` consecutive
    failed grabs, park the title on the blocklist -- reversible from the
    Blocklist page -- and log it so the loop stops hammering a dead release.
    Returns True when it blocklisted. `limit <= 0` disables the breaker."""
    if limit <= 0:
        return False
    streak = db.demote_failure_streak(movie_id)
    if streak < limit:
        return False
    db.blocklist_add(movie_id, title=title,
                     reason=f"auto: {streak} consecutive grab failures")
    db.record_action(movie_id=movie_id, title=title, action="blocklist",
                     dry_run=False, status="blocklisted",
                     detail=f"auto-blocklisted after {streak} consecutive "
                            f"failed grabs -- release likely pulled from indexer")
    return True


def run_once(force: bool = False) -> dict:
    settings = db.all_settings()
    dry = bool(settings.get("dry_run", True))
    radarr = radarr_from_env()

    st = status()
    if not st["action_needed"] and not force:
        db.log_run("loop", True, f"no action: projected free "
                                 f"{st['projected_free_gb']:.0f} GB >= target")
        return {"acted": False, "status": st, "reason": "at or above target"}

    profile = find_archive_profile(radarr, settings["archive_profile_name"])
    if not profile:
        msg = (f"quality profile '{settings['archive_profile_name']}' not found "
               "in Radarr -- create it first")
        db.log_run("loop", False, msg)
        raise LoopAbort(msg)

    check = validate_archive_profile(profile, settings["archive_tier"])
    if not check["ok"]:
        db.log_run("loop", False, check["reason"])
        raise LoopAbort(check["reason"])

    built = build_candidates(settings, radarr)
    ranked = built["ranked"]

    deficit = int(st["deficit_gb"] * GB)
    chosen = score.select_for_deficit(ranked, deficit, int(settings["batch_size"]))

    depth = int(settings.get("tier_fallback_depth", 0))
    acted = []
    for c in chosen:
        ladder = _tier_ladder(profile, c.tier, depth)
        rel, tier = _pick_release(radarr, c.id, ladder, max_bytes=c.size)
        if not rel:
            db.record_action(movie_id=c.id, title=c.title, action="skip",
                             old_tier=c.tier, old_size=c.size, dry_run=dry,
                             status="skipped",
                             detail=f"no release available in {'/'.join(ladder)}")
            continue

        fallback = ("" if tier.lower() in {t.lower() for t in rung_tiers(ladder[0])}
                    else f" (fell back from {ladder[0]})")
        aid = db.record_action(
            movie_id=c.id, title=c.title, action="demote", old_tier=c.tier,
            old_size=c.size, old_profile_id=c.movie.get("qualityProfileId"),
            new_profile_id=profile["id"], release_guid=rel.get("guid"),
            dry_run=dry, status="pending",
            detail=f"-> {tier}{fallback} score={c.score:.3f} "
                   f"reclaim={c.reclaim / GB:.1f}GB",
        )

        if dry:
            db.update_action(aid, "dry-run")
            acted.append({"title": c.title, "reclaim_gb": c.reclaim / GB,
                          "score": c.score, "dry_run": True})
            continue

        try:
            m = radarr.movie(c.id)
            m["qualityProfileId"] = profile["id"]
            tag_id = radarr.ensure_tag(str(settings["archived_tag"]))
            m["tags"] = sorted(set(m.get("tags") or []) | {tag_id})
            radarr.update_movie(m)

            radarr.grab(rel["guid"], rel.get("indexerId"))
            db.update_action(aid, "grabbed")
            acted.append({"title": c.title, "reclaim_gb": c.reclaim / GB,
                          "score": c.score, "dry_run": False})
        except Exception as e:  # noqa: BLE001
            db.update_action(aid, "failed", str(e)[:300])
            _blocklist_on_repeated_failure(
                c.id, c.title, int(settings.get("max_grab_failures", 3)))

        time.sleep(float(settings["search_throttle_seconds"]))

    summary = (f"{'DRY-RUN: ' if dry else ''}{len(acted)} demotion(s), "
               f"deficit {st['deficit_gb']:.0f} GB, "
               f"{len(ranked)} eligible candidates")
    db.log_run("loop", True, summary)
    return {"acted": True, "status": st, "chosen": acted,
            "eligible": len(ranked), "rejected": built["rejected"],
            "summary": summary}


def _profile_buckets(profile: dict) -> list[dict]:
    """A profile's top-level items, low->high, as rank buckets.

    A plain item is a bucket of one quality. A quality GROUP is one bucket whose
    members all share the group's rank -- Radarr compares grouped qualities as
    equals. The group's own 'allowed' flag is the authoritative one: Radarr
    ignores the members' flags (and they drift out of sync when qualities are
    dragged in and out of groups, or a profile is written by Recyclarr/the API).
    A group with no 'allowed' key at all falls back to its members' flags.
    """
    out: list[dict] = []
    for it in profile.get("items") or []:
        subs = it.get("items") or []
        if subs:
            names = [(s.get("quality") or {}).get("name") for s in subs]
            names = [n for n in names if n]
            allowed = (bool(it["allowed"]) if "allowed" in it
                       else any(s.get("allowed") for s in subs))
            ids = {it.get("id")} | {(s.get("quality") or {}).get("id") for s in subs}
            out.append({"names": names, "allowed": allowed,
                        "label": it.get("name") or (names[-1] if names else None),
                        "ids": {i for i in ids if i is not None}})
        else:
            q = it.get("quality") or {}
            name = q.get("name")
            out.append({"names": [name] if name else [],
                        "allowed": bool(it.get("allowed")), "label": name,
                        "ids": {q.get("id")} if q.get("id") is not None else set()})
    return out


def _profile_allowed(profile: dict) -> list[str]:
    """Allowed quality names, low->high (group members in their listed order)."""
    return [n for b in _profile_buckets(profile) if b["allowed"] for n in b["names"]]


def _profile_top_tier(profile: dict) -> str | None:
    """Highest allowed quality in a profile -- the tier a grab targets."""
    allowed = _profile_allowed(profile)
    return allowed[-1] if allowed else None


def _profile_quality_rank(profile: dict) -> dict[str, int]:
    """Every quality Radarr knows, ranked low->high by THIS profile's own item
    order -- not just the allowed subset. A profile's item order is what
    Radarr itself uses to decide what outranks what for grabs on that profile,
    and it's per-profile customizable (Archive-4K deliberately drags
    WEBDL-2160p to the top, ahead of Remux-2160p, to make it the ceiling). Using
    the profile's own order rather than a separate global fetch keeps a tier's
    rank consistent with whatever reordering that profile relies on."""
    rank: dict[str, int] = {}
    for i, b in enumerate(_profile_buckets(profile)):
        for name in b["names"]:
            rank[name] = i  # members of one group share a rank, as in Radarr
    return rank


def managed_grab_profiles(radarr: Radarr, settings: dict) -> list[dict]:
    """Profiles the curator force-grabs cutoff-unmet titles for: every profile
    named by a rule, plus the Archive-HD profile if that track is enabled."""
    profs = radarr.quality_profiles()
    by_id = {int(p["id"]): p for p in profs}
    by_name = {p["name"].lower(): p for p in profs}
    out: dict[int, dict] = {}
    for pid in assign.rule_target_profile_ids(radarr):
        if pid in by_id:
            out[pid] = by_id[pid]
    if settings.get("hd_track_enabled"):
        p = by_name.get(str(settings["hd_profile_name"]).lower())
        if p:
            out[int(p["id"])] = p
    return list(out.values())


def profile_pending(profile: dict, movies: list[dict], queued: set[int],
                    target: str | None = None) -> dict:
    """Movies on a profile whose file OUTRANKS every allowed quality -- i.e. an
    out-of-profile file (a Remux-2160p on a 1080p profile), which is the thing
    to demote. A file already within the allowed set is satisfied and left
    alone, so a grab never *upgrades* an in-profile title.

    A file that ranks BELOW every allowed quality is left alone too -- it was
    never a remux to begin with (some WEBDL-720p or DVD title already on this
    profile), so this isn't a demotion, it's an upgrade. Routing it through
    _grab_profile would cap the replacement's size at that already-tiny
    existing file (the demotion size guard), which only a mislabeled or
    garbage-quality "1080p" release could ever satisfy -- exactly the kind of
    release that looks like a downgrade despite matching the quality label.

    Excludes titles already downloading."""
    target = target or _profile_top_tier(profile)
    allowed = set(_profile_allowed(profile))
    rank = _profile_quality_rank(profile)
    top_rank = max((rank[a] for a in allowed if a in rank), default=None)
    pending = []
    if target:
        for m in movies:
            if m.get("qualityProfileId") != profile["id"]:
                continue
            mf = m.get("movieFile")
            if not mf:
                continue
            tier = ((mf.get("quality") or {}).get("quality") or {}).get("name")
            if tier in allowed or int(m["id"]) in queued:
                continue
            if top_rank is not None and rank.get(tier, -1) < top_rank:
                continue
            pending.append(m)
    return {"profile": profile, "target": target, "allowed": sorted(allowed),
            "pending": pending}


def _protected_ids(radarr: Radarr, settings: dict, movies: list[dict]) -> set[int]:
    """Titles that must never be demoted on any track: the blocklist plus
    anything carrying the exclusion tag (curator-keep)."""
    ids = set(db.blocklist_ids())
    label = str(settings.get("exclusion_tag", "")).lower()
    tag_id = None
    try:
        for t in radarr.tags():
            if t["label"].lower() == label:
                tag_id = int(t["id"])
    except Exception:  # noqa: BLE001
        pass
    if tag_id is not None:
        for m in movies:
            if tag_id in (m.get("tags") or []):
                ids.add(int(m["id"]))
    return ids


def _grab_profile(radarr: Radarr, profile: dict, movies: list[dict],
                  queued: set[int], protected: set[int],
                  dry: bool, batch: int, throttle: float, depth: int = 0,
                  max_failures: int = 0) -> dict:
    """Force-grab the target tier for cutoff-unmet titles on one profile. No
    profile switch -- rules/user already assigned them; this is only the
    search+grab Radarr won't do automatically."""
    info = profile_pending(profile, movies, queued)
    target = info["target"]
    pending = [m for m in info["pending"] if int(m["id"]) not in protected]
    acted = []
    for m in pending[:batch]:
        mf = m.get("movieFile") or {}
        old_tier = ((mf.get("quality") or {}).get("quality") or {}).get("name")
        old_size = int(mf.get("size") or 0)
        ladder = _tier_ladder(profile, old_tier, depth)
        rel, tier = _pick_release(radarr, m["id"], ladder, max_bytes=old_size)
        if not rel:
            db.record_action(movie_id=m["id"], title=m.get("title"), action="skip",
                             old_tier=old_tier, old_size=old_size, dry_run=dry,
                             status="skipped",
                             detail=f"{profile['name']}: no release available "
                                    f"in {'/'.join(ladder)}")
            continue
        fallback = ("" if tier.lower() in {t.lower() for t in rung_tiers(ladder[0])}
                    else f" (fell back from {ladder[0]})")
        aid = db.record_action(movie_id=m["id"], title=m.get("title"),
                               action="demote", old_tier=old_tier, old_size=old_size,
                               new_profile_id=profile["id"],
                               release_guid=rel.get("guid"), dry_run=dry,
                               status="pending",
                               detail=f"{profile['name']} -> {tier}{fallback}")
        if dry:
            db.update_action(aid, "dry-run")
            acted.append(m.get("title"))
            continue
        try:
            radarr.grab(rel["guid"], rel.get("indexerId"))
            db.update_action(aid, "grabbed")
            acted.append(m.get("title"))
        except Exception as e:  # noqa: BLE001
            db.update_action(aid, "failed", str(e)[:300])
            _blocklist_on_repeated_failure(m["id"], m.get("title"), max_failures)
        time.sleep(throttle)
    return {"profile": profile["name"], "target": target,
            "grabbed": acted, "remaining": len(pending)}


def run_managed(force: bool = False) -> dict:
    """Apply profile-assignment rules, then force-grab replacements for every
    managed profile. One scheduled pass keeps Radarr in sync and downloads the
    demotions it won't auto-search."""
    settings = db.all_settings()
    rules = db.rules_enabled()
    hd_on = bool(settings.get("hd_track_enabled"))
    if not rules and not hd_on and not force:
        return {"acted": False, "reason": "no rules and Archive-HD track off"}
    dry = bool(settings.get("dry_run", True))
    radarr = radarr_from_env()

    protected = _protected_ids(radarr, settings, radarr.movies())
    assigned = assign.apply_profile_rules(radarr, dry=dry, protected_ids=protected)

    # Re-pull after assignment so newly-switched titles show their new profile.
    movies = radarr.movies()
    queued = _queued_movie_ids(radarr)
    batch = int(settings.get("hd_batch_size", settings.get("batch_size", 5)))
    throttle = float(settings["search_throttle_seconds"])

    depth = int(settings.get("tier_fallback_depth", 0))
    results, grabbed, remaining = [], 0, 0
    for prof in managed_grab_profiles(radarr, settings):
        r = _grab_profile(radarr, prof, movies, queued, protected, dry, batch,
                          throttle, depth,
                          int(settings.get("max_grab_failures", 3)))
        results.append(r)
        grabbed += len(r["grabbed"])
        remaining += r["remaining"]

    summary = (f"{'DRY-RUN: ' if dry else ''}assigned {assigned['assigned']}, "
               f"grabbed {grabbed}, {remaining} pending "
               f"across {len(results)} profile(s)")
    db.log_run("managed", True, summary)
    return {"assigned": assigned, "grabbed": grabbed, "remaining": remaining,
            "profiles": results, "summary": summary}


def managed_health(radarr: Radarr | None = None) -> list[dict]:
    """Lightweight validity check for the dashboard badge -- just validates each
    managed profile (no movie/queue pull). Returns the invalid ones."""
    settings = db.all_settings()
    radarr = radarr or radarr_from_env()
    bad = []
    for prof in managed_grab_profiles(radarr, settings):
        target = _profile_top_tier(prof)
        v = (validate_archive_profile(prof, target) if target
             else {"ok": False, "reason": "profile has no allowed quality"})
        if not v.get("ok"):
            bad.append({"name": prof["name"], "reason": v.get("reason") or ""})
    return bad


def managed_status(radarr: Radarr | None = None) -> list[dict]:
    """What the curator currently derives from each managed profile: its target
    tier, whether it's a valid demotion profile, and how many titles are pending.

    This is the 'are we in sync' view -- the curator reads Radarr live, so a
    manual profile edit shows up here rather than drifting silently.
    """
    settings = db.all_settings()
    radarr = radarr or radarr_from_env()
    movies = cached_movies(radarr)
    queued = _queued_movie_ids(radarr)
    protected = _protected_ids(radarr, settings, movies)
    out = []
    for prof in managed_grab_profiles(radarr, settings):
        info = profile_pending(prof, movies, queued)
        target = info["target"]
        v = (validate_archive_profile(prof, target) if target
             else {"ok": False, "reason": "profile has no allowed quality"})
        pend = [m for m in info["pending"] if int(m["id"]) not in protected]
        out.append({
            "name": prof["name"], "target": target,
            "allowed": info.get("allowed"),
            "valid": bool(v.get("ok")), "reason": v.get("reason") or "",
            "pending": len(pend),
        })
    return out
