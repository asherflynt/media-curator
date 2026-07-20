"""SQLite-backed config, action manifest and audit findings.

Tunables AND connections (service URLs + API keys) live here, edited via the UI
like other *ARR apps. Env vars (MC_RADARR_URL/KEY, MC_SONARR_URL/KEY, path vars)
are a bootstrap fallback only: a DB value always wins, so once set in the UI the
.env can go away.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

DB_PATH = os.environ.get("MC_DB", "/data/media-curator.db")

_local = threading.local()

GB = 1024 ** 3

DEFAULTS: dict[str, Any] = {
    # --- the headline control ---
    "free_space_target_gb": 5000,
    # Stop demoting once projected free reaches target + this. Prevents the
    # loop oscillating around the threshold.
    "hysteresis_gb": 500,

    # Hard exclusion: a title released within this window is never demoted.
    # A recent film sitting unplayed means "hasn't got to it yet".
    "new_release_window_months": 24,

    # Score weights (relative; normalised at ranking time). Ranking is by
    # impact (bytes reclaimed) and age only -- watch history is not used.
    "w_impact": 1.0,
    "w_age": 0.5,

    "source_tiers": ["Remux-2160p"],
    "archive_tier": "WEBDL-2160p",
    "archive_profile_name": "Archive-4K",

    # Path mapping (UI-editable; env vars are the bootstrap default). media_path
    # is the container mount point and stays tied to the volume mount.
    "radarr_root": os.environ.get("MC_RADARR_ROOT", "/downloads/Movies"),
    "media_movies_subdir": os.environ.get("MC_MEDIA_MOVIES_SUBDIR", "Movies"),

    "batch_size": 5,
    "search_throttle_seconds": 20,

    # Second demotion track. Unlike the free-space loop this isn't space-driven:
    # the user picks titles by assigning them to hd_profile_name in Radarr (e.g.
    # kids movies -> Archive-HD), and media-curator force-grabs the target that
    # Radarr won't auto-search for after a profile change. hd_target_tier must be
    # the TOP allowed quality in that profile.
    "hd_track_enabled": False,
    "hd_profile_name": "Archive-HD",
    "hd_target_tier": "Remux-1080p",
    "hd_batch_size": 5,

    "exclusion_tag": "curator-keep",
    "archived_tag": "archived",

    # Default ON. A real run requires turning this off deliberately.
    "dry_run": True,

    # Anomaly thresholds, as a ratio against the file's own cohort median.
    "bloated_ratio": 2.5,
    "underweight_ratio": 0.4,
    "min_cohort_size": 8,

    "audit_cron_hour": 4,
    "audit_cron_day": "sun",
    "loop_interval_minutes": 60,
    "loop_enabled": False,
}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn = c
    return c


def init() -> None:
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Every action taken, for audit and rollback.
        CREATE TABLE IF NOT EXISTS manifest (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            REAL NOT NULL,
            movie_id      INTEGER,
            title         TEXT,
            action        TEXT NOT NULL,
            old_tier      TEXT,
            old_size      INTEGER,
            old_profile_id INTEGER,
            new_profile_id INTEGER,
            release_guid  TEXT,
            dry_run       INTEGER NOT NULL DEFAULT 1,
            status        TEXT NOT NULL DEFAULT 'pending',
            detail        TEXT
        );

        CREATE TABLE IF NOT EXISTS findings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            first_seen REAL NOT NULL,
            last_seen  REAL NOT NULL,
            kind       TEXT NOT NULL,
            klass      TEXT NOT NULL,
            ref_id     INTEGER,
            title      TEXT,
            path       TEXT,
            size       INTEGER,
            tier       TEXT,
            codec      TEXT,
            bitrate    REAL,
            cohort_median REAL,
            ratio      REAL,
            dismissed  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(klass, kind, path)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       REAL NOT NULL,
            kind     TEXT NOT NULL,
            ok       INTEGER NOT NULL,
            summary  TEXT
        );

        -- Titles the user never wants demoted. A hard filter, like the
        -- exclusion tag, but managed in this app's own UI. Keyed by Radarr
        -- movie id (what eligibility matches on); tmdb id + title are for
        -- display and portability.
        CREATE TABLE IF NOT EXISTS blocklist (
            movie_id INTEGER PRIMARY KEY,
            tmdb_id  INTEGER,
            title    TEXT,
            year     INTEGER,
            reason   TEXT,
            added    REAL NOT NULL
        );

        -- Service connections (URL + API key), edited in the UI like other
        -- *ARR apps. Env vars are a fallback only; a DB value wins.
        CREATE TABLE IF NOT EXISTS connections (
            name    TEXT PRIMARY KEY,
            url     TEXT,
            api_key TEXT,
            updated REAL
        );

        -- Standing rules that assign a Radarr quality profile to movies by
        -- genre / collection / tag, so new titles land on the right archive
        -- profile without a manual bulk edit. match_type in (genre,collection,tag).
        CREATE TABLE IF NOT EXISTS profile_rules (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            match_type   TEXT NOT NULL,
            match_value  TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            enabled      INTEGER NOT NULL DEFAULT 1,
            added        REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_manifest_ts ON manifest(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_findings_klass ON findings(klass, dismissed);
        """
    )
    c.commit()


def get(key: str) -> Any:
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return DEFAULTS.get(key)


def set_(key: str, value: Any) -> None:
    c = conn()
    c.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    c.commit()


def all_settings() -> dict[str, Any]:
    out = dict(DEFAULTS)
    for row in conn().execute("SELECT key,value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            pass
    return out


_CONN_ENV = {
    "radarr": ("MC_RADARR_URL", "MC_RADARR_KEY"),
    "sonarr": ("MC_SONARR_URL", "MC_SONARR_KEY"),
}


def get_connection_row(name: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM connections WHERE name=?", (name,)).fetchone()


def get_connection(name: str) -> tuple[str | None, str | None]:
    """(url, api_key): DB value if set, else the env-var fallback."""
    row = get_connection_row(name)
    env_url, env_key = _CONN_ENV.get(name, (None, None))
    url = (row["url"] if row and row["url"] else None) or \
        (os.environ.get(env_url) if env_url else None)
    key = (row["api_key"] if row and row["api_key"] else None) or \
        (os.environ.get(env_key) if env_key else None)
    return url, key


def set_connection(name: str, url: str, api_key: str | None = None) -> None:
    """Save a connection. Empty api_key keeps the existing one (so the UI can
    show a masked field without forcing a re-entry on every save)."""
    row = get_connection_row(name)
    if not api_key:
        api_key = row["api_key"] if row else None
    c = conn()
    c.execute(
        "INSERT INTO connections(name,url,api_key,updated) VALUES(?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET url=excluded.url, "
        "api_key=excluded.api_key, updated=excluded.updated",
        (name, url.strip(), api_key, time.time()),
    )
    c.commit()


def connection_display(name: str) -> dict:
    """URL + a masked key indicator for the UI (never returns the raw key)."""
    url, key = get_connection(name)
    row = get_connection_row(name)
    source = "database" if (row and (row["url"] or row["api_key"])) else \
        ("env var" if (url or key) else "unset")
    return {"name": name, "url": url or "", "has_key": bool(key),
            "key_hint": (key[-4:] if key else ""), "source": source}


def log_run(kind: str, ok: bool, summary: str) -> None:
    c = conn()
    c.execute(
        "INSERT INTO runs(ts,kind,ok,summary) VALUES(?,?,?,?)",
        (time.time(), kind, int(ok), summary),
    )
    c.commit()


def record_action(**kw: Any) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO manifest(ts,movie_id,title,action,old_tier,old_size,"
        "old_profile_id,new_profile_id,release_guid,dry_run,status,detail) "
        "VALUES(:ts,:movie_id,:title,:action,:old_tier,:old_size,"
        ":old_profile_id,:new_profile_id,:release_guid,:dry_run,:status,:detail)",
        {
            "ts": time.time(),
            "movie_id": kw.get("movie_id"),
            "title": kw.get("title"),
            "action": kw["action"],
            "old_tier": kw.get("old_tier"),
            "old_size": kw.get("old_size"),
            "old_profile_id": kw.get("old_profile_id"),
            "new_profile_id": kw.get("new_profile_id"),
            "release_guid": kw.get("release_guid"),
            "dry_run": int(kw.get("dry_run", True)),
            "status": kw.get("status", "pending"),
            "detail": kw.get("detail"),
        },
    )
    c.commit()
    return int(cur.lastrowid or 0)


def update_action(action_id: int, status: str, detail: str | None = None) -> None:
    c = conn()
    c.execute(
        "UPDATE manifest SET status=?, detail=COALESCE(?,detail) WHERE id=?",
        (status, detail, action_id),
    )
    c.commit()


def blocklist_add(movie_id: int, tmdb_id: int | None = None,
                  title: str | None = None, year: int | None = None,
                  reason: str | None = None) -> None:
    c = conn()
    c.execute(
        "INSERT INTO blocklist(movie_id,tmdb_id,title,year,reason,added) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(movie_id) DO UPDATE SET "
        "tmdb_id=excluded.tmdb_id, title=excluded.title, year=excluded.year, "
        "reason=COALESCE(excluded.reason, blocklist.reason)",
        (int(movie_id), tmdb_id, title, year, reason, time.time()),
    )
    c.commit()


def blocklist_remove(movie_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM blocklist WHERE movie_id=?", (int(movie_id),))
    c.commit()


def blocklist_ids() -> set[int]:
    return {int(r["movie_id"])
            for r in conn().execute("SELECT movie_id FROM blocklist")}


def blocklist_all() -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM blocklist ORDER BY added DESC").fetchall()


def rule_add(match_type: str, match_value: str, profile_name: str) -> None:
    c = conn()
    c.execute(
        "INSERT INTO profile_rules(match_type,match_value,profile_name,enabled,added) "
        "VALUES(?,?,?,1,?)",
        (match_type.strip().lower(), match_value.strip(), profile_name.strip(),
         time.time()),
    )
    c.commit()


def rule_remove(rule_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM profile_rules WHERE id=?", (int(rule_id),))
    c.commit()


def rule_set_enabled(rule_id: int, enabled: bool) -> None:
    c = conn()
    c.execute("UPDATE profile_rules SET enabled=? WHERE id=?",
              (1 if enabled else 0, int(rule_id)))
    c.commit()


def rules_all() -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM profile_rules ORDER BY id").fetchall()


def rules_enabled() -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM profile_rules WHERE enabled=1 ORDER BY id").fetchall()


def upsert_finding(f: dict[str, Any]) -> bool:
    """Returns True if this is a newly-seen finding (drives the weekly diff)."""
    now = time.time()
    c = conn()
    row = c.execute(
        "SELECT id FROM findings WHERE klass=? AND kind=? AND path IS ?",
        (f["klass"], f["kind"], f.get("path")),
    ).fetchone()
    if row:
        c.execute("UPDATE findings SET last_seen=?, size=?, bitrate=?, ratio=? WHERE id=?",
                  (now, f.get("size"), f.get("bitrate"), f.get("ratio"), row["id"]))
        c.commit()
        return False
    c.execute(
        "INSERT INTO findings(first_seen,last_seen,kind,klass,ref_id,title,path,"
        "size,tier,codec,bitrate,cohort_median,ratio) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, f["kind"], f["klass"], f.get("ref_id"), f.get("title"),
         f.get("path"), f.get("size"), f.get("tier"), f.get("codec"),
         f.get("bitrate"), f.get("cohort_median"), f.get("ratio")),
    )
    c.commit()
    return True
