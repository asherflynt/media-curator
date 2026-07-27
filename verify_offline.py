"""Offline exercise of the tier ladder + force-import against fakes that
reproduce the two observed failures."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MC_DB"] = os.path.join(tempfile.mkdtemp(), "verify.db")

from app import db, importer, loop
db.init()
GB = 1024**3
P = lambda b: "\033[92mPASS\033[0m" if b else "\033[91mFAIL\033[0m"
fails = []
def check(name, cond):
    print(f"  {P(cond)}  {name}")
    if not cond: fails.append(name)

# Archive-HD profile as configured: 1080p ladder, Bluray-1080p ranked top.
ARCHIVE_HD = {"id": 9, "name": "Archive-HD", "items": [
    {"quality": {"id": 1, "name": "WEBRip-1080p"}, "allowed": True},
    {"quality": {"id": 2, "name": "WEBDL-1080p"}, "allowed": True},
    {"quality": {"id": 3, "name": "Bluray-1080p"}, "allowed": True},
]}

class FakeRadarrEmpty:
    def releases(self, movie_id): return []

print("=== 1. tier ladder")
check("depth 0 = top tier only (old behaviour)",
      loop._tier_ladder(ARCHIVE_HD, "Remux-2160p", 0) == ["Bluray-1080p"])
check("depth 3 walks the profile's allowed list downward",
      loop._tier_ladder(ARCHIVE_HD, "Remux-2160p", 3)
      == ["Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"])
check("never offers a tier at or above the current file (no accidental upgrade)",
      loop._tier_ladder(ARCHIVE_HD, "WEBDL-1080p", 3) == ["WEBRip-1080p"])
check("file already at the bottom -> empty ladder, nothing to grab",
      loop._tier_ladder(ARCHIVE_HD, "WEBRip-1080p", 3) == [])
check("empty ladder is handled, not an IndexError",
      loop._pick_release(FakeRadarrEmpty(), 1, []) == (None, None))

print("\n=== 2. release picking")
def rel(tier, size_gb, rejections=(), score=0, seeders=10):
    return {"guid": f"g-{tier}-{size_gb}", "indexerId": 1,
            "quality": {"quality": {"name": tier}}, "size": int(size_gb * GB),
            "rejections": list(rejections), "customFormatScore": score,
            "seeders": seeders}

class FakeRadarr:
    def __init__(self, releases): self._r = releases
    def releases(self, movie_id): return self._r

# The exact reported case: no Bluray-1080p exists, only WEBDL-1080p.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8, ["Not an upgrade for existing movie file."])]),
    1, ["Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"], max_bytes=int(39.5 * GB))
check("falls back to WEBDL-1080p when no Bluray-1080p exists", tier == "WEBDL-1080p")

# Preference order still holds when the top tier IS available.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8), rel("Bluray-1080p", 14)]),
    1, ["Bluray-1080p", "WEBDL-1080p"], max_bytes=int(39.5 * GB))
check("prefers the top tier when it exists", tier == "Bluray-1080p")

# Hard rejections are still respected at every rung.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8, ["Unknown language"])]),
    1, ["Bluray-1080p", "WEBDL-1080p"], max_bytes=int(39.5 * GB))
check("a real rejection is still skipped at a fallback tier", r is None)

# Size guard: a 1080p remux bigger than the file we hold reclaims nothing.
r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 45)]), 1, ["Bluray-1080p"],
    max_bytes=int(39.5 * GB))
check("refuses a replacement no smaller than the current file", r is None)

print("\n=== 3. blocked-import detection")
STUCK = {
    "movieId": 42, "downloadId": "ABC123", "trackedDownloadState": "importPending",
    "trackedDownloadStatus": "warning",
    "statusMessages": [{"title": "Elemental (2023) (1080p BDRip...).mkv",
                        "messages": ["Not an upgrade for existing movie file. "
                                     "Existing quality: Remux-2160p. New Quality "
                                     "Bluray-1080p."]}],
    "movie": {"id": 42, "title": "Elemental", "qualityProfileId": 9},
}
UNRELATED = dict(STUCK, movieId=7, movie={"id": 7, "title": "Other", "qualityProfileId": 1})
HEALTHY = {"movieId": 8, "downloadId": "D", "trackedDownloadState": "downloading",
           "trackedDownloadStatus": "ok", "statusMessages": [],
           "movie": {"id": 8, "title": "Fine", "qualityProfileId": 9}}
STALLED = {"movieId": 9, "downloadId": "E", "trackedDownloadState": "importBlocked",
           "trackedDownloadStatus": "warning",
           "statusMessages": [{"title": "x", "messages": ["No files found are eligible for import"]}],
           "movie": {"id": 9, "title": "Stalled", "qualityProfileId": 9}}

class QRadarr:
    def __init__(self, recs): self._q = recs
    def queue(self, page_size=1000): return self._q

found = importer.stuck_items(QRadarr([STUCK, UNRELATED, HEALTHY, STALLED]), {9})
check("finds the downgrade-blocked item", any(r["movieId"] == 42 for r in found))
check("ignores a movie on an unmanaged profile", not any(r["movieId"] == 7 for r in found))
check("ignores a healthy download", not any(r["movieId"] == 8 for r in found))
check("ignores a differently-blocked item", not any(r["movieId"] == 9 for r in found))
check("exactly one match", len(found) == 1)

print("\n=== 4. force import: delete-then-import ordering")
calls = []
class ImpRadarr:
    """Fake Radarr whose movie gains a new file once ManualImport runs --
    unless `import_lands` is False, or the command reports `cmd_status`."""
    def __init__(self, import_lands=True, cmd_status="completed", rejections=()):
        self.imported = False
        self.import_lands = import_lands
        self.cmd_status = cmd_status
        self.rejections = list(rejections)
    def queue(self, page_size=1000, include_movie=True): return [STUCK]
    def movie(self, mid):
        if self.imported and self.import_lands:
            mf = {"id": 777, "size": int(9.2*GB),
                  "quality": {"quality": {"name": "Bluray-1080p"}}}
        elif self.imported:
            mf = None  # deleted, and the import never landed
        else:
            mf = {"id": 555, "size": int(39.5*GB),
                  "quality": {"quality": {"name": "Remux-2160p"}}}
        return {"id": 42, "title": "Elemental", "qualityProfileId": 9,
                "movieFile": mf}
    def wait_for_command(self, cid, timeout=0, interval=0):
        return {"id": cid, "status": self.cmd_status, "message": "boom"}
    def manual_import_candidates(self, did, movie_id=None):
        calls.append(("manualimport", did))
        return [{"path": "/downloads/sample.mkv", "size": 40*1024*1024,
                 "quality": {"quality": {"name": "Bluray-1080p"}}, "movieId": 42},
                {"path": "/downloads/Elemental.mkv", "size": int(9.2*GB),
                 "quality": {"quality": {"name": "Bluray-1080p"}},
                 "languages": [{"id": 1, "name": "English"}],
                 "movie": {"id": 42}, "folderName": "Elemental.2023.1080p",
                 "releaseGroup": "JBENT", "indexerFlags": 0,
                 "rejections": [{"reason": "Not an upgrade for existing movie "
                                 "file. Existing quality: Remux-2160p. New "
                                 "Quality Bluray-1080p.", "type": "permanent"}]
                                + self.rejections}]
    def delete_movie_file(self, fid): calls.append(("delete", fid))
    def command(self, name, **body):
        calls.append(("command", name, body))
        self.imported = True
        return {"id": 99, "name": name, "status": "queued"}

res = importer.force_import(ImpRadarr(), STUCK, dry=False)
names = [c[0] for c in calls]
check("succeeded", res["ok"] is True)
check("deleted the outranking file before importing",
      names.index("delete") < names.index("command"))
check("deleted the right movie file id", ("delete", 555) in calls)
cmd = [c for c in calls if c[0] == "command"][0]
check("issued ManualImport", cmd[1] == "ManualImport")
f = cmd[2]["files"][0]
check("imported the feature, not the sample", f["path"] == "/downloads/Elemental.mkv")
check("carried the downloadId through", f["downloadId"] == "ABC123")
check("no stray 'size' key in the command payload", "size" not in f)
check("no internal 'blockers' key in the command payload", "blockers" not in f)
check("passes folderName through (obfuscated-filename releases)",
      f["folderName"] == "Elemental.2023.1080p")
check("reads the movie id from the manual-import movie object", f["movieId"] == 42)
check("manifest row written", db.conn().execute(
    "SELECT COUNT(*) n FROM manifest WHERE action='import' AND status='imported'"
).fetchone()["n"] == 1)

print("\n=== 5. dry run touches nothing")
calls.clear()
res = importer.force_import(ImpRadarr(), STUCK, dry=True)
check("dry run performs no delete/command",
      not any(c[0] in ("delete", "command") for c in calls))

print("\n=== 6. force import refuses or reports honestly")
calls.clear()
res = importer.force_import(ImpRadarr(rejections=[
    {"reason": "Unpacking", "type": "temporary"}]), STUCK, dry=False)
check("another rejection on the chosen file -> nothing deleted",
      not res["ok"] and not any(c[0] == "delete" for c in calls))
calls.clear()
res = importer.force_import(ImpRadarr(import_lands=False), STUCK, dry=False)
check("import that never lands is reported as a failure, not success",
      res["ok"] is False and "Recycle Bin" in res["detail"])
calls.clear()
res = importer.force_import(ImpRadarr(cmd_status="failed"), STUCK, dry=False)
check("a failed ManualImport command is reported as a failure",
      res["ok"] is False and "failed" in res["detail"])
calls.clear()
res = importer.force_import(ImpRadarr(cmd_status="started", import_lands=False),
                            STUCK, dry=False)
check("an import still running is reported as pending, not imported",
      res.get("pending") is True)

print("\n=== 7. blocked-import edge cases")
WITH_SAMPLE = dict(STUCK, statusMessages=[
    {"title": "One or more movies expected in this release were not imported "
              "or missing", "messages": []},
    {"title": "Elemental.mkv", "messages": [
        "Not an upgrade for existing movie file. Existing quality: Remux-2160p. "
        "New Quality Bluray-1080p."]},
    {"title": "sample.mkv", "messages": ["Sample"]}])
check("a bundled sample doesn't disqualify the strict (upgrade-only) sweep",
      importer._upgrade_block_only(WITH_SAMPLE))
ALSO_BAD = dict(STUCK, statusMessages=STUCK["statusMessages"] + [
    {"title": "Elemental.mkv", "messages": ["No audio tracks detected"]}])
check("a real second rejection still disqualifies the strict sweep",
      not importer._upgrade_block_only(ALSO_BAD))
check("no managed profile -> nothing is ever touched (empty != everything)",
      importer.stuck_items(QRadarr([STUCK, UNRELATED]), set()) == [])

print("\n=== 8. quality groups (Radarr's default WEB groups)")
def grp(gid, name, members, allowed, child_allowed=None):
    return {"id": gid, "name": name, "allowed": allowed, "items": [
        {"quality": {"id": qid, "name": qn}, "items": [],
         "allowed": allowed if child_allowed is None else child_allowed}
        for qid, qn in members]}
ARCHIVE_4K_GROUPED = {"id": 5, "name": "Archive-4K", "cutoff": 1001, "items": [
    {"quality": {"id": 19, "name": "Remux-2160p"}, "items": [], "allowed": False},
    grp(1001, "WEB 2160p", [(18, "WEBDL-2160p"), (17, "WEBRip-2160p")], True),
]}
v = loop.validate_archive_profile(ARCHIVE_4K_GROUPED, "WEBDL-2160p")
check("archive tier inside the top group is valid (no false 'outranks')", v["ok"])
check("a group cutoff (id >= 1000) is resolved, and is the archive tier",
      v["cutoff"] == "WEB 2160p" and v["cutoff_ok"])
STALE_CHILDREN = dict(ARCHIVE_4K_GROUPED, items=[
    ARCHIVE_4K_GROUPED["items"][0],
    grp(1001, "WEB 2160p", [(18, "WEBDL-2160p"), (17, "WEBRip-2160p")],
        True, child_allowed=False)])
check("the group's allowed flag wins over stale member flags",
      loop.validate_archive_profile(STALE_CHILDREN, "WEBDL-2160p")["ok"])
BAD = dict(ARCHIVE_4K_GROUPED, items=[
    grp(1001, "WEB 2160p", [(18, "WEBDL-2160p"), (17, "WEBRip-2160p")], True),
    {"quality": {"id": 19, "name": "Remux-2160p"}, "items": [], "allowed": True}])
check("an allowed quality above the archive tier is still caught",
      not loop.validate_archive_profile(BAD, "WEBDL-2160p")["ok"])
check("the original ungrouped profile still validates",
      loop.validate_archive_profile(ARCHIVE_HD, "Bluray-1080p")["ok"])
HD_GROUPED = {"id": 9, "name": "Archive-HD", "items": [
    grp(1002, "WEB 1080p", [(3, "WEBDL-1080p"), (15, "WEBRip-1080p")], True),
    {"quality": {"id": 7, "name": "Bluray-1080p"}, "items": [], "allowed": True},
]}
check("ladder from a grouped tier skips its equal-rank peer",
      loop._tier_ladder(HD_GROUPED, "WEBRip-1080p", 3) == [])
check("grouped members share one rank",
      len({loop._profile_quality_rank(HD_GROUPED)[n]
           for n in ("WEBDL-1080p", "WEBRip-1080p")}) == 1)
check("current Radarr 'equal or higher preference' wording counts as soft",
      loop._rejection_is_soft("Existing file on disk has an equal or higher "
                              "preference: 1 (WEBDL-2160p)"))

print()
print("\033[91m%d FAILURE(S)\033[0m" % len(fails) if fails else "\033[92mall passed\033[0m")
sys.exit(1 if fails else 0)
