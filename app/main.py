"""FastAPI app: dashboard, settings, candidates, audit, history."""
from __future__ import annotations

import os
import threading
import traceback
from contextlib import asynccontextmanager
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import audit, db, importer, loop, space
from .arr import radarr_from_env

GB = 1024 ** 3
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _datetimeformat(ts: object) -> str:
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


templates.env.filters["datetimeformat"] = _datetimeformat
scheduler = BackgroundScheduler()


_SCHEDULE_KEYS = ("audit_cron_day", "audit_cron_hour", "loop_interval_minutes",
                  "import_interval_minutes")


def _triggers(s: dict) -> dict:
    """Build every trigger from settings, raising ValueError on a bad value.

    Kept separate from _schedule so a bad value can be rejected BEFORE it is
    saved, instead of being discovered after the jobs were already removed (or
    on the next container start, where it would stop the app booting)."""
    def minutes(key: str) -> int:
        v = int(float(s[key]))
        if v < 1:
            raise ValueError(f"{key} must be at least 1 minute")
        return v
    hour = int(float(s["audit_cron_hour"]))
    if not 0 <= hour <= 23:
        raise ValueError("audit_cron_hour must be between 0 and 23")
    return {
        "audit": CronTrigger(day_of_week=str(s["audit_cron_day"]).strip().lower(),
                             hour=hour),
        "loop": IntervalTrigger(minutes=minutes("loop_interval_minutes")),
        "managed": IntervalTrigger(minutes=minutes("loop_interval_minutes")),
        "import": IntervalTrigger(minutes=minutes("import_interval_minutes")),
    }


def _schedule() -> None:
    s = db.all_settings()
    try:
        trig = _triggers(s)
    except (KeyError, TypeError, ValueError) as e:
        # A value stored by an older build, or edited into the DB by hand. Run
        # on the defaults rather than refuse to start.
        db.log_run("schedule", False, f"invalid schedule setting ({e}); "
                                      f"using defaults")
        trig = _triggers({**s, **{k: db.DEFAULTS[k] for k in _SCHEDULE_KEYS}})
    scheduler.remove_all_jobs()
    scheduler.add_job(_safe_audit, trig["audit"], id="audit", replace_existing=True)
    if s.get("loop_enabled"):
        scheduler.add_job(_safe_loop, trig["loop"], id="loop", replace_existing=True)
    if s.get("hd_track_enabled") or db.rules_enabled():
        scheduler.add_job(_safe_managed, trig["managed"], id="managed",
                          replace_existing=True)
    if s.get("auto_import_downgrades"):
        # Runs more often than the demotion loop: a grab is worthless until it
        # imports, and every hour a download sits blocked is an hour the space
        # it was meant to reclaim stays occupied by BOTH copies.
        scheduler.add_job(_safe_import, trig["import"], id="import",
                          replace_existing=True)


def _safe_audit() -> None:
    try:
        audit.run_audit()
    except Exception as e:  # noqa: BLE001
        db.log_run("audit", False, f"{e}\n{traceback.format_exc()[:500]}")


def _safe_loop() -> None:
    try:
        loop.run_once()
    except Exception as e:  # noqa: BLE001
        db.log_run("loop", False, str(e)[:500])


def _safe_managed() -> None:
    try:
        loop.run_managed()
    except Exception as e:  # noqa: BLE001
        db.log_run("managed", False, str(e)[:500])


def _safe_import() -> None:
    try:
        importer.run_import_sweep()
    except Exception as e:  # noqa: BLE001
        db.log_run("import", False, str(e)[:500])


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _schedule()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="media-curator", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ctx: dict = {"request": request, "page": "dashboard", "errors": [],
                 "stuck_imports": 0}
    try:
        ctx["status"] = loop.status()
    except Exception as e:  # noqa: BLE001
        ctx["errors"].append(f"Free-space read failed for {space.media_path()}: {e}")
        ctx["status"] = None

    # Surface the asserted path mapping rather than trusting it silently.
    try:
        r = radarr_from_env()
        # Cached: this is ~20 MB for this library and none of it moves
        # minute-to-minute. A dashboard refresh shouldn't re-pull it.
        movies = audit.cached_movies(r)
        ctx["cache_age"] = audit.cache_age()
        paths = [m["path"] for m in movies[:60] if m.get("path")]
        ctx["mapping"] = space.check_mapping(paths)
        recs = [
            {"kind": "movie", "tier": ((m["movieFile"].get("quality") or {}).get("quality") or {}).get("name", "?"),
             "size": int(m["movieFile"].get("size") or 0)}
            for m in movies if m.get("movieFile")
        ]
        ctx["tiers"] = audit.tier_breakdown(recs, "movie")
        ctx["tracked_gb"] = sum(r_["size"] for r_ in recs) / GB
        # Shown only for comparison: these are per-share shfs figures and are
        # NOT what the loop uses.
        ctx["arr_diskspace"] = r.diskspace()
        # Badge: warn if a managed profile was edited into an invalid state.
        try:
            ctx["managed_bad"] = loop.managed_health(r)
        except Exception:  # noqa: BLE001
            ctx["managed_bad"] = []
        ctx["stuck_imports"] = importer.pending_count(r)
    except Exception as e:  # noqa: BLE001
        ctx["errors"].append(
            f"Radarr unreachable ({e}). The page still renders; free space above "
            "is read from the filesystem and does not depend on Radarr."
        )
        ctx["mapping"] = None
        ctx["tiers"] = []
        ctx["arr_diskspace"] = None

    ctx["runs"] = db.conn().execute(
        "SELECT * FROM runs ORDER BY ts DESC LIMIT 10").fetchall()
    ctx["settings"] = db.all_settings()
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request):
    return templates.TemplateResponse(request, "connections.html", {
        "request": request, "page": "connections", "settings": db.all_settings(),
        "radarr": db.connection_display("radarr"),
        "sonarr": db.connection_display("sonarr"),
        "saved": request.query_params.get("saved"),
    })


@app.post("/connections/save")
async def connections_save(request: Request):
    form = await request.form()
    for name in ("radarr", "sonarr"):
        url = str(form.get(f"{name}_url") or "").strip()
        key = str(form.get(f"{name}_key") or "").strip()
        if url:
            db.set_connection(name, url, key or None)  # blank key = keep existing
    return RedirectResponse("/connections?saved=1", status_code=303)


@app.post("/connections/test")
async def connections_test(request: Request):
    """Ping a service with the given (or saved) creds, like *ARR's Test button."""
    form = await request.form()
    name = str(form.get("name") or "radarr")
    url = str(form.get("url") or "").strip()
    key = str(form.get("key") or "").strip()
    if not url or not key:
        # Fill in only what the form left blank: the key field is blank by
        # design ("leave blank to keep current"), and testing a new URL must
        # not silently test the old saved one instead.
        saved_url, saved_key = db.get_connection(name)
        url, key = url or (saved_url or ""), key or (saved_key or "")
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API key required"})
    try:
        from .arr import ArrClient
        st = ArrClient(url, key).ping()
        return {"ok": True, "version": st.get("version"),
                "app": st.get("appName") or st.get("instanceName") or name}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)[:200]})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "request": request, "page": "settings", "settings": db.all_settings(),
        "defaults": db.DEFAULTS, "error": request.query_params.get("error"),
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    # Reject an unusable schedule up front -- once stored it would take down
    # the scheduler, and with it every automatic run.
    proposed = {**db.all_settings(),
                **{k: str(form[k]).strip() for k in _SCHEDULE_KEYS if k in form}}
    try:
        _triggers(proposed)
    except (KeyError, TypeError, ValueError) as e:
        return RedirectResponse(
            "/settings?error=" + quote(f"Schedule not saved: {e}"), status_code=303)
    for key, default in db.DEFAULTS.items():
        if key not in form:
            if isinstance(default, bool):
                db.set_(key, False)  # unchecked checkboxes don't POST
            continue
        raw = str(form[key]).strip()
        if isinstance(default, bool):
            db.set_(key, raw.lower() in ("on", "true", "1", "yes"))
        elif isinstance(default, list):
            db.set_(key, [p.strip() for p in raw.split(",") if p.strip()])
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                db.set_(key, int(float(raw)))
            except ValueError:
                pass
        elif isinstance(default, float):
            try:
                db.set_(key, float(raw))
            except ValueError:
                pass
        else:
            db.set_(key, raw)
    _schedule()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request):
    ctx: dict = {"request": request, "page": "candidates", "error": None,
                 "settings": db.all_settings()}
    try:
        s = db.all_settings()
        built = loop.build_candidates(s, radarr_from_env())
        ranked = built["ranked"]
        ctx["rejected"] = built["rejected"]
        ctx["target_gb"] = built["target_bytes"] / GB
        ctx["candidates"] = ranked[:100]
        ctx["total_eligible"] = len(ranked)
        ctx["total_reclaim_gb"] = sum(c.reclaim for c in ranked) / GB
    except Exception as e:  # noqa: BLE001
        ctx["error"] = str(e)
        ctx["candidates"] = []
    return templates.TemplateResponse(request, "candidates.html", ctx)


@app.get("/blocklist", response_class=HTMLResponse)
def blocklist_page(request: Request, q: str = ""):
    rows = db.blocklist_all()
    matches = []
    if q.strip():
        # Search Radarr's library so the user can block a title that isn't
        # currently in the candidate list.
        try:
            ql = q.strip().lower()
            for m in audit.cached_movies(radarr_from_env()):
                if ql in str(m.get("title", "")).lower():
                    matches.append(m)
            matches = sorted(matches, key=lambda m: m.get("title", ""))[:25]
        except Exception:  # noqa: BLE001
            matches = []
    return templates.TemplateResponse(request, "blocklist.html", {
        "request": request, "page": "blocklist", "rows": rows,
        "q": q, "matches": matches, "settings": db.all_settings(),
    })


def _opt_int(v: object) -> int | None:
    """Optional integer form field. Templates can render a missing value as
    'None' or '', and neither should 500 the request."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n or None


@app.post("/blocklist/add")
async def blocklist_add(request: Request):
    form = await request.form()
    try:
        mid = int(str(form["movie_id"]))
    except (KeyError, ValueError):
        return RedirectResponse("/blocklist", status_code=303)
    db.blocklist_add(
        movie_id=mid,
        tmdb_id=_opt_int(form.get("tmdb_id")),
        title=str(form.get("title") or "") or None,
        year=_opt_int(form.get("year")),
        reason=str(form.get("reason") or "") or None,
    )
    return RedirectResponse(str(form.get("back") or "/blocklist"), status_code=303)


@app.post("/blocklist/remove/{movie_id}")
def blocklist_remove(movie_id: int):
    db.blocklist_remove(movie_id)
    return RedirectResponse("/blocklist", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    rows = db.conn().execute(
        "SELECT * FROM findings WHERE dismissed=0 ORDER BY "
        "CASE klass WHEN 'underweight' THEN 0 WHEN 'orphan' THEN 1 "
        "WHEN 'broken' THEN 2 WHEN 'bloated' THEN 3 ELSE 4 END, size DESC "
        "LIMIT 500").fetchall()
    counts = db.conn().execute(
        "SELECT klass, COUNT(*) n, COALESCE(SUM(size),0) bytes FROM findings "
        "WHERE dismissed=0 GROUP BY klass ORDER BY n DESC").fetchall()
    return templates.TemplateResponse(request, "audit.html", {
        "request": request, "page": "audit", "findings": rows, "counts": counts,
        "settings": db.all_settings(),
    })


@app.post("/audit/run")
def audit_run():
    try:
        return JSONResponse(audit.run_audit())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/audit/dismiss/{fid}")
def audit_dismiss(fid: int):
    db.conn().execute("UPDATE findings SET dismissed=1 WHERE id=?", (fid,))
    db.conn().commit()
    return RedirectResponse("/audit", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    rows = db.conn().execute(
        "SELECT * FROM manifest ORDER BY ts DESC LIMIT 300").fetchall()
    return templates.TemplateResponse(request, "history.html", {
        "request": request, "page": "history", "rows": rows,
        "settings": db.all_settings(),
    })


@app.post("/loop/run")
def loop_run():
    try:
        return JSONResponse(loop.run_once(force=True), status_code=200)
    except loop.LoopAbort as e:
        return JSONResponse({"error": str(e), "aborted": True}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/managed/run")
def managed_run():
    try:
        return JSONResponse(loop.run_managed(force=True), status_code=200)
    except loop.LoopAbort as e:
        return JSONResponse({"error": str(e), "aborted": True}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/import/run")
def import_run():
    try:
        return JSONResponse(importer.run_import_sweep(force=True), status_code=200)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    ctx: dict = {"request": request, "page": "rules", "settings": db.all_settings(),
                 "rules": db.rules_all(), "profiles": [], "genres": [], "error": None}
    try:
        r = radarr_from_env()
        ctx["profiles"] = [p["name"] for p in r.quality_profiles()]
        movies = audit.cached_movies(r)
        genres: dict[str, int] = {}
        colls: dict[str, int] = {}
        for m in movies:
            for g in (m.get("genres") or []):
                genres[g] = genres.get(g, 0) + 1
            c = (m.get("collection") or {}).get("title")
            if c:
                colls[c] = colls.get(c, 0) + 1
        ctx["genres"] = sorted(genres.items(), key=lambda kv: -kv[1])
        ctx["collections"] = sorted(colls.items(), key=lambda kv: -kv[1])[:40]
        ctx["managed"] = loop.managed_status(r)
    except Exception as e:  # noqa: BLE001
        ctx["error"] = str(e)
        ctx["collections"] = []
        ctx["managed"] = []
    return templates.TemplateResponse(request, "rules.html", ctx)


@app.post("/rules/add")
async def rules_add(request: Request):
    form = await request.form()
    mt = str(form.get("match_type") or "").strip()
    mv = str(form.get("match_value") or "").strip()
    pn = str(form.get("profile_name") or "").strip()
    if mt in ("genre", "collection", "tag") and mv and pn:
        db.rule_add(mt, mv, pn)
        _schedule()  # a first rule enables the managed job
    return RedirectResponse("/rules", status_code=303)


@app.post("/rules/remove/{rule_id}")
def rules_remove(rule_id: int):
    db.rule_remove(rule_id)
    _schedule()
    return RedirectResponse("/rules", status_code=303)


@app.get("/api/status")
def api_status():
    try:
        return loop.status()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


_audit_lock = threading.Lock()
_audit_rerun = threading.Event()


def _webhook_audit() -> None:
    """Coalesced audit for import webhooks. A season pack fires one webhook per
    episode; at most one audit runs at a time, and any number of webhooks that
    arrive meanwhile collapse into a single follow-up run."""
    _audit_rerun.set()
    if not _audit_lock.acquire(blocking=False):
        return  # the running audit will see the flag and go again
    try:
        while _audit_rerun.is_set():
            _audit_rerun.clear()
            try:
                audit.run_audit(tag=True)
            except Exception as e:  # noqa: BLE001
                db.log_run("audit", False, f"webhook audit: {e}"[:500])
    finally:
        _audit_lock.release()


@app.api_route("/webhook/import", methods=["POST", "PUT"])
async def webhook_import(request: Request, background: BackgroundTasks):
    """Radarr/Sonarr Connect -> Webhook target.

    Judges newly imported files against their cohort, so the library stops
    accumulating new problems while the backlog is cleared. Answers at once and
    audits in the background: a full audit can outlast Radarr's webhook timeout
    (100s), and running it inline would also stall every other page.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = str(payload.get("eventType") or "")
    # Imports and upgrades both arrive as "Download" (with isUpgrade); "Upgrade"
    # is accepted for older builds.
    if event.lower() not in ("download", "upgrade", "test"):
        return {"ok": True, "ignored": event}
    if event.lower() == "test":
        return {"ok": True, "test": True}
    background.add_task(_webhook_audit)
    return JSONResponse({"ok": True, "queued": True}, status_code=202)
