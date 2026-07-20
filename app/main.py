"""FastAPI app: dashboard, settings, candidates, audit, history."""
from __future__ import annotations

import os
import traceback
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Form, Request
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


def _schedule() -> None:
    scheduler.remove_all_jobs()
    s = db.all_settings()
    scheduler.add_job(
        _safe_audit, CronTrigger(day_of_week=s["audit_cron_day"],
                                 hour=int(s["audit_cron_hour"])),
        id="audit", replace_existing=True,
    )
    if s.get("loop_enabled"):
        scheduler.add_job(
            _safe_loop, IntervalTrigger(minutes=int(s["loop_interval_minutes"])),
            id="loop", replace_existing=True,
        )
    if s.get("hd_track_enabled") or db.rules_enabled():
        scheduler.add_job(
            _safe_managed, IntervalTrigger(minutes=int(s["loop_interval_minutes"])),
            id="managed", replace_existing=True,
        )
    if s.get("auto_import_downgrades"):
        # Runs more often than the demotion loop: a grab is worthless until it
        # imports, and every hour a download sits blocked is an hour the space
        # it was meant to reclaim stays occupied by BOTH copies.
        scheduler.add_job(
            _safe_import, IntervalTrigger(minutes=int(s["import_interval_minutes"])),
            id="import", replace_existing=True,
        )


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
    return templates.TemplateResponse("dashboard.html", ctx)


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request):
    return templates.TemplateResponse("connections.html", {
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
        url, key = db.get_connection(name)
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
    return templates.TemplateResponse("settings.html", {
        "request": request, "page": "settings", "settings": db.all_settings(),
        "defaults": db.DEFAULTS,
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
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
    return templates.TemplateResponse("candidates.html", ctx)


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
    return templates.TemplateResponse("blocklist.html", {
        "request": request, "page": "blocklist", "rows": rows,
        "q": q, "matches": matches, "settings": db.all_settings(),
    })


@app.post("/blocklist/add")
async def blocklist_add(request: Request):
    form = await request.form()
    try:
        mid = int(str(form["movie_id"]))
    except (KeyError, ValueError):
        return RedirectResponse("/blocklist", status_code=303)
    db.blocklist_add(
        movie_id=mid,
        tmdb_id=int(form["tmdb_id"]) if form.get("tmdb_id") else None,
        title=str(form.get("title") or "") or None,
        year=int(form["year"]) if form.get("year") else None,
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
    return templates.TemplateResponse("audit.html", {
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
    return templates.TemplateResponse("history.html", {
        "request": request, "page": "history", "rows": rows,
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
    return templates.TemplateResponse("rules.html", ctx)


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


@app.post("/webhook/import")
async def webhook_import(request: Request):
    """Radarr/Sonarr Connect -> Custom Script / Webhook target.

    Judges each newly imported file against its cohort immediately, so the
    library stops accumulating new problems while the backlog is cleared.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    event = payload.get("eventType", "")
    if event not in ("Download", "Upgrade", "Test"):
        return {"ok": True, "ignored": event}
    if event == "Test":
        return {"ok": True, "test": True}
    try:
        res = audit.run_audit(tag=True)
        return {"ok": True, "findings": res["counts"], "new": res["new"]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
