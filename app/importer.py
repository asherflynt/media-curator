"""Force-import of completed demotion downloads that Radarr refuses.

The problem this exists to solve
--------------------------------
A demotion downloads fine and then dies on the doorstep. Radarr runs its upgrade
check at IMPORT time, against the file still on disk, and reports:

    Not an upgrade for existing movie file.
    Existing quality: Remux-2160p. New Quality Bluray-1080p.

Switching the movie to the archive profile does not clear this. The check that
blocks the import compares the two files by quality DEFINITION weight, which is
a global ranking -- Remux-2160p simply outweighs Bluray-1080p, and no profile
ordering changes that. Profile ordering governs what Radarr *searches for and
grabs*; the import guard is a separate, blunter rule. So the grab succeeds, the
import is refused, and the item parks in the queue waiting for a human to click
through the manual-import dialog. Fifty times in one night, in this library's
case.

The fix
-------
Do exactly what that human did, on a schedule:

  1. Delete the existing file first. Radarr sends it to the Recycle Bin (so it
     stays recoverable for the retention window, and the manifest's "a demotion
     can be reversed" promise still holds), and it also removes the thing the
     upgrade check was comparing against.
  2. Issue ManualImport for the downloaded file. Manual import bypasses the
     upgrade specification by design -- that is the entire reason the dialog
     lets you click Import next to a red rejection.

Deleting first is what makes this deterministic rather than a retry loop, and it
is also what actually frees the bytes: until the remux leaves the movie folder,
a demotion has reclaimed nothing.

Blast radius
------------
Only queue items that (a) are blocked specifically on the upgrade rejection and
(b) belong to a movie sitting on a curator-managed profile are ever touched.
Anything else in the queue -- a normal upgrade, a failed download, a title the
curator never demoted -- is left completely alone.
"""
from __future__ import annotations

import time

from . import db
from .arr import Radarr, radarr_from_env

GB = 1024 ** 3

# Radarr phrases the block a few ways depending on version and whether the
# comparison was against a file or a queued grab.
_UPGRADE_BLOCK_MARKERS = (
    "not an upgrade for existing movie file",
    "not an upgrade for existing",
    "existing quality",
)


def _is_upgrade_block(messages: list[str]) -> bool:
    return any(any(m in msg.lower() for m in _UPGRADE_BLOCK_MARKERS)
               for msg in messages)


def _status_messages(record: dict) -> list[str]:
    """Flatten a queue record's nested statusMessages into plain strings."""
    out: list[str] = []
    for sm in record.get("statusMessages") or []:
        if isinstance(sm, str):
            out.append(sm)
            continue
        title = sm.get("title")
        if title:
            out.append(str(title))
        out.extend(str(m) for m in (sm.get("messages") or []))
    for key in ("errorMessage", "trackedDownloadStatusMessage"):
        if record.get(key):
            out.append(str(record[key]))
    return out


def stuck_items(radarr: Radarr, managed_profile_ids: set[int]) -> list[dict]:
    """Queue entries that finished downloading but are blocked on the downgrade
    rejection, restricted to movies on a curator-managed profile."""
    out = []
    for rec in radarr.queue():
        if not rec.get("movieId"):
            continue
        state = str(rec.get("trackedDownloadState") or "").lower()
        tds = str(rec.get("trackedDownloadStatus") or "").lower()
        # importPending / importBlocked is the "waiting on a human" state.
        # importFailed also lands here after Radarr gives up retrying.
        if state not in ("importpending", "importblocked", "importfailed") \
                and tds not in ("warning", "error"):
            continue
        if not _is_upgrade_block(_status_messages(rec)):
            continue
        movie = rec.get("movie") or {}
        pid = movie.get("qualityProfileId")
        if managed_profile_ids and pid not in managed_profile_ids:
            continue
        out.append(rec)
    return out


def _importable_file(radarr: Radarr, download_id: str, movie_id: int) -> dict | None:
    """The one real video file in a finished download.

    Rejections on these entries are ignored on purpose -- the upgrade rejection
    is precisely what we are overriding. The largest file wins, which is how
    samples and extras get left behind.
    """
    try:
        cands = radarr.manual_import_candidates(download_id)
    except Exception:  # noqa: BLE001
        return None
    usable = [c for c in cands if c.get("path") and int(c.get("size") or 0) > 0]
    if not usable:
        return None
    best = max(usable, key=lambda c: int(c.get("size") or 0))
    return {
        "path": best["path"],
        "movieId": int(best.get("movieId") or movie_id),
        "quality": best.get("quality"),
        "languages": best.get("languages") or [],
        "releaseGroup": best.get("releaseGroup") or "",
        "indexerFlags": best.get("indexerFlags") or 0,
        "downloadId": download_id,
        "size": int(best.get("size") or 0),
    }


def force_import(radarr: Radarr, record: dict, dry: bool = True) -> dict:
    """Delete the outranking file, then manually import the replacement."""
    movie_id = int(record["movieId"])
    movie = record.get("movie") or {}
    title = movie.get("title") or record.get("title") or f"movie {movie_id}"
    download_id = record.get("downloadId")

    if not download_id:
        return {"title": title, "ok": False, "detail": "queue item has no downloadId"}

    # Re-read the movie: the queue's embedded copy can be stale, and we are
    # about to delete a file based on it.
    try:
        fresh = radarr.movie(movie_id)
    except Exception as e:  # noqa: BLE001
        return {"title": title, "ok": False, "detail": f"movie lookup failed: {e}"}

    mf = fresh.get("movieFile") or {}
    old_tier = ((mf.get("quality") or {}).get("quality") or {}).get("name")
    old_size = int(mf.get("size") or 0)

    incoming = _importable_file(radarr, download_id, movie_id)
    if not incoming:
        return {"title": title, "ok": False,
                "detail": "no importable file found for this download"}
    new_tier = ((incoming.get("quality") or {}).get("quality") or {}).get("name")

    aid = db.record_action(
        movie_id=movie_id, title=title, action="import", old_tier=old_tier,
        old_size=old_size, new_profile_id=fresh.get("qualityProfileId"),
        dry_run=dry, status="pending",
        detail=f"force-import {old_tier or 'no file'} -> {new_tier} "
               f"({incoming['size'] / GB:.1f}GB)",
    )

    if dry:
        db.update_action(aid, "dry-run")
        return {"title": title, "ok": True, "dry_run": True,
                "from": old_tier, "to": new_tier}

    try:
        # Step 1 -- clear the blocker. Goes to the Recycle Bin, not oblivion.
        if mf.get("id"):
            radarr.delete_movie_file(int(mf["id"]))
        # Step 2 -- import past the rejection.
        radarr.command("ManualImport", importMode="auto", files=[{
            k: v for k, v in incoming.items() if k != "size"
        }])
        db.update_action(aid, "imported")
        return {"title": title, "ok": True, "from": old_tier, "to": new_tier,
                "freed_gb": old_size / GB}
    except Exception as e:  # noqa: BLE001
        # A failure after the delete leaves the movie fileless with the old file
        # in the Recycle Bin -- recoverable, and loud in the manifest.
        db.update_action(aid, "failed", str(e)[:300])
        return {"title": title, "ok": False, "detail": str(e)[:200]}


def run_import_sweep(force: bool = False) -> dict:
    """Clear every demotion download parked on the downgrade rejection."""
    settings = db.all_settings()
    if not settings.get("auto_import_downgrades", True) and not force:
        return {"acted": False, "reason": "auto-import of downgrades is off"}

    dry = bool(settings.get("dry_run", True))
    radarr = radarr_from_env()

    # Step 1 of a force-import deletes the existing file. That is only a
    # *reversible* act if Radarr has a Recycle Bin configured -- without one the
    # DELETE is permanent, and this sweep would quietly shred remuxes at machine
    # speed the first time it ran. Refuse rather than find that out afterwards.
    if not dry and not settings.get("allow_permanent_delete"):
        try:
            bin_path = (radarr.media_management() or {}).get("recycleBin")
        except Exception as e:  # noqa: BLE001
            return {"acted": False, "error": f"could not read Radarr media "
                                             f"management config: {e}"}
        if not bin_path:
            msg = ("refusing to force-import: Radarr has no Recycle Bin "
                   "configured, so deleting the existing file would be "
                   "permanent. Either set it in Radarr -> Settings -> Media "
                   "Management -> File Management -> Recycle Bin, or turn on "
                   "'Allow permanent delete' in media-curator Settings to "
                   "accept irreversible demotions.")
            db.log_run("import", False, msg)
            return {"acted": False, "error": msg}

    from .loop import find_archive_profile, managed_grab_profiles
    managed = {int(p["id"]) for p in managed_grab_profiles(radarr, settings)}
    arch = find_archive_profile(radarr, settings["archive_profile_name"])
    if arch:
        managed.add(int(arch["id"]))

    stuck = stuck_items(radarr, managed)
    throttle = float(settings.get("import_throttle_seconds", 2))
    results = []
    for rec in stuck:
        results.append(force_import(radarr, rec, dry=dry))
        time.sleep(throttle)

    ok = sum(1 for r in results if r.get("ok"))
    summary = (f"{'DRY-RUN: ' if dry else ''}{ok}/{len(stuck)} blocked "
               f"import(s) forced")
    db.log_run("import", True, summary)
    return {"acted": bool(stuck), "stuck": len(stuck), "imported": ok,
            "results": results, "summary": summary}


def pending_count(radarr: Radarr | None = None) -> int:
    """Dashboard badge: how many downloads are stuck on the rejection."""
    try:
        radarr = radarr or radarr_from_env()
        settings = db.all_settings()
        from .loop import find_archive_profile, managed_grab_profiles
        managed = {int(p["id"]) for p in managed_grab_profiles(radarr, settings)}
        arch = find_archive_profile(radarr, settings["archive_profile_name"])
        if arch:
            managed.add(int(arch["id"]))
        return len(stuck_items(radarr, managed))
    except Exception:  # noqa: BLE001
        return 0
