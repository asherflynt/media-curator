"""Exercise the engine against the live library. Read-only."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("MC_DB", "/tmp/mc-verify/mc.db")
os.environ.setdefault("MC_MEDIA_PATH", os.path.expanduser("~"))
os.environ.setdefault("MC_MEDIA_MOVIES_SUBDIR", "Movies")

from app import db, rules, score, space, audit          # noqa: E402
from app.arr import radarr_from_env                      # noqa: E402
from app.loop import validate_archive_profile            # noqa: E402

GB = 1024 ** 3
db.init()
s = db.all_settings()
ok = lambda b: "\033[92mPASS\033[0m" if b else "\033[91mFAIL\033[0m"  # noqa: E731

print("=== 1. statvfs free space")
r = space.read_space(space.media_path())
print(f"  {r.path}: {r.free_gb:,.0f} GB free / {r.total_gb:,.0f} GB total  {ok(r.total_bytes > 0)}")

print("\n=== 2. inventory")
FIXTURE = os.environ.get("MC_FIXTURE")
radarr = None
if FIXTURE:
    # Offline mode: replay a real library pull captured from this Radarr.
    import json
    raw = json.load(open(FIXTURE))
    movies = raw["movies"]
    recs = []
    for m in movies:
        mf = m.get("movieFile")
        if not mf:
            continue
        recs.append(rules.file_record(
            kind="movie", title=m.get("title", "?"), path=mf.get("path"),
            size=int(mf.get("size") or 0), quality=mf.get("quality") or {},
            media_info=mf.get("mediaInfo") or {}, ref_id=int(m["id"]),
            fallback_runtime_min=m.get("runtime")))
    print(f"  [offline fixture: {FIXTURE}]")
else:
    radarr = radarr_from_env()
    inv = audit.inventory(radarr=radarr, sonarr=None)
    movies = inv["movies"]
    recs = inv["movie_records"]
tracked = sum(x["size"] for x in recs)
print(f"  {len(movies)} movies, {len(recs)} with files, {tracked/GB:,.0f} GB  {ok(len(recs) > 1000)}")

print("\n=== 3. bitrate coverage (the 11,008-missing-videoBitrate problem)")
methods = {}
for x in recs:
    methods[x["bitrate_method"]] = methods.get(x["bitrate_method"], 0) + 1
print(f"  {methods}")
have = sum(1 for x in recs if x["bitrate"])
print(f"  {have}/{len(recs)} have a usable bitrate  {ok(have/len(recs) > 0.95)}")

print("\n=== 4. cohorts")
co = rules.cohorts(recs, int(s["min_cohort_size"]))
for k, v in sorted(co.items(), key=lambda kv: -kv[1]["n"])[:6]:
    print(f"  {k[0]:16} {k[1]:8} n={v['n']:4}  median {v['median']/1e6:6.2f} Mbps")
print(f"  {len(co)} cohorts met the n>={s['min_cohort_size']} bar  {ok(len(co) > 3)}")

print("\n=== 5. classification")
findings = rules.classify(recs, s)
counts = {}
for f in findings:
    counts[f["klass"]] = counts.get(f["klass"], 0) + 1
print(f"  {counts}")

print("\n=== 6. archive-tier median (measured, not assumed)")
tb = audit.archive_tier_median_bytes(recs, s["archive_tier"])
print(f"  {s['archive_tier']} median = {tb/GB:.2f} GB  {ok(tb > 0)}")

print("\n=== 7. hard filters")
cands, rej = score.eligible(movies, s, None, tb)
print(f"  eligible={len(cands)}  rejected={rej}")
print(f"  new-release window protected {rej['inside_new_release_window']} titles")

print("\n=== 8. THE CRITICAL GUARD: new-release window holds at any weighting")
now = time.time()
window = float(s["new_release_window_months"]) * score.MONTH_SECONDS
violations = [c for c in cands if c.release_ts and (now - c.release_ts) < window]
print(f"  candidates inside the {s['new_release_window_months']}-month window: "
      f"{len(violations)}  {ok(len(violations) == 0)}")

extreme = dict(s, w_impact=1000.0, w_age=0.0)
ranked = score.rank(cands, extreme)
viol2 = [c for c in ranked if c.release_ts and (now - c.release_ts) < window]
print(f"  same, with impact weighted 1000x: {len(viol2)}  {ok(len(viol2) == 0)}")

print("\n=== 9. release-date basis (fallback chain)")
basis = {}
for c in cands:
    basis[c.release_basis] = basis.get(c.release_basis, 0) + 1
print(f"  {basis}")

print("\n=== 10. ranking with default weights")
ranked = score.rank(cands, s)
print(f"  {len(ranked)} ranked, total reclaim {sum(c.reclaim for c in ranked)/GB:,.0f} GB")
for c in ranked[:8]:
    print(f"    {c.score:.3f}  {c.reclaim/GB:5.1f} GB  {c.movie.get('year')}  {c.title[:42]}")

print("\n=== 11. select_for_deficit stops at the deficit (the Recycle Bin trap)")
deficit = 100 * GB
chosen = score.select_for_deficit(ranked, deficit, batch_size=999)
total = sum(c.reclaim for c in chosen)
print(f"  deficit 100 GB -> chose {len(chosen)} titles, {total/GB:.1f} GB")
# Greedy-by-score: overshoot is bounded by the last title added, never more.
without_last = total - chosen[-1].reclaim
print(f"  overshoot bounded by one title ({without_last/GB:.1f} GB < 100 GB "
      f"before the last pick): {ok(without_last < deficit)}")
chosen_b = score.select_for_deficit(ranked, 100000 * GB, batch_size=5)
print(f"  batch_size caps runaway: huge deficit -> {len(chosen_b)} titles  {ok(len(chosen_b) == 5)}")

print("\n=== 12. archive profile ordering validation")
bad = {"name": "T", "cutoff": 1, "items": [
    {"quality": {"id": 1, "name": "WEBDL-2160p"}, "allowed": True},
    {"quality": {"id": 2, "name": "Remux-2160p"}, "allowed": True}]}
good = {"name": "T", "cutoff": 1, "items": [
    {"quality": {"id": 2, "name": "Remux-2160p"}, "allowed": False},
    {"quality": {"id": 1, "name": "WEBDL-2160p"}, "allowed": True}]}
rb = validate_archive_profile(bad, "WEBDL-2160p")
rg = validate_archive_profile(good, "WEBDL-2160p")
print(f"  remux ranked above target -> rejected: {ok(not rb['ok'])}")
print(f"    reason: {rb['reason'][:80]}...")
print(f"  target ranked top -> accepted: {ok(rg['ok'])}")

print("\n=== 13. real archive profile present in Radarr?")
if radarr is None:
    print("  skipped (offline fixture mode)")
else:
    from app.loop import find_archive_profile  # noqa: E402
    p = find_archive_profile(radarr, s["archive_profile_name"])
    if p:
        v = validate_archive_profile(p, s["archive_tier"])
        print(f"  '{s['archive_profile_name']}' exists. ordering ok={v['ok']} top={v['top']}")
    else:
        print(f"  '{s['archive_profile_name']}' NOT created yet (expected -- manual step)")
        print(f"  existing profiles: {[q['name'] for q in radarr.quality_profiles()]}")
