"""Radarr / Sonarr v3 API clients.

These services are authoritative for metadata, quality tiers and file
operations. They are NOT trusted for free space -- see space.py for why.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class ArrError(RuntimeError):
    pass


class ArrClient:
    def __init__(self, base: str, key: str, timeout: float = 120.0):
        self.base = base.rstrip("/")
        self.key = key
        self._c = httpx.Client(
            timeout=timeout, headers={"X-Api-Key": key}, follow_redirects=True
        )

    def _req(self, method: str, path: str, timeout: float | None = None,
             **kw: Any) -> Any:
        url = f"{self.base}/api/v3/{path.lstrip('/')}"
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            r = self._c.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise ArrError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise ArrError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError as e:
            # A URL missing Radarr's URL Base (or pointing at a reverse-proxy
            # login page) answers 200 with the web UI's HTML, not JSON.
            raise ArrError(
                f"{method} {path}: response was not JSON -- check the URL "
                f"(including any URL Base) points at the API"
            ) from e

    def get(self, path: str, **kw: Any) -> Any:
        return self._req("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self._req("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self._req("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self._req("DELETE", path, **kw)

    # Small/fast endpoints get a short timeout so a page render degrades in
    # seconds when the host is down rather than blocking on the 120s default.
    # (Observed for real: the box answered ICMP while every service was dead,
    # and the dashboard hung for two minutes.)
    FAST_TIMEOUT = 8.0

    def ping(self) -> dict:
        return self.get("system/status", timeout=self.FAST_TIMEOUT)

    # --- tags -------------------------------------------------------------
    def tags(self) -> list[dict]:
        return self.get("tag") or []

    def ensure_tag(self, label: str) -> int:
        label = label.lower()
        for t in self.tags():
            if t["label"].lower() == label:
                return int(t["id"])
        created = self.post("tag", json={"label": label})
        return int(created["id"])

    def quality_profiles(self) -> list[dict]:
        return self.get("qualityprofile") or []


class Radarr(ArrClient):
    def movies(self) -> list[dict]:
        return self.get("movie") or []

    def movie(self, movie_id: int) -> dict:
        return self.get(f"movie/{movie_id}")

    def update_movie(self, movie: dict) -> dict:
        return self.put(f"movie/{movie['id']}", json=movie)

    def releases(self, movie_id: int) -> list[dict]:
        return self.get("release", params={"movieId": movie_id}) or []

    def grab(self, guid: str, indexer_id: int) -> Any:
        """Force-grab a specific release. Radarr downloads, verifies, imports,
        and only then replaces the existing file (old one -> Recycle Bin)."""
        return self.post("release", json={"guid": guid, "indexerId": indexer_id})

    def queue(self, page_size: int = 500, include_movie: bool = True) -> list[dict]:
        """Every queue record, following pagination (a single page silently
        truncated large queues)."""
        out: list[dict] = []
        seen: set = set()
        page = 1
        while True:
            q = self.get("queue", params={
                "page": page, "pageSize": page_size,
                "includeMovie": "true" if include_movie else "false"}) or {}
            recs = q.get("records") or []
            for r in recs:
                # Radarr clamps an out-of-range page back to the last real one,
                # so a queue that shrank mid-walk can repeat records.
                key = r.get("id") or (r.get("downloadId"), r.get("movieId"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
            total = int(q.get("totalRecords") or 0)
            if (len(recs) < page_size or len(out) >= total
                    or int(q.get("page") or page) != page or page >= 100):
                return out
            page += 1

    def delete_movie_file(self, movie_file_id: int) -> Any:
        """Delete the current file for a movie. Honours Radarr's Recycle Bin
        setting, so this is reversible for the retention window -- that is the
        whole basis of the manifest's 'a demotion can be reversed' claim."""
        return self.delete(f"moviefile/{int(movie_file_id)}")

    def manual_import_candidates(self, download_id: str) -> list[dict]:
        """What Radarr's manual-import dialog would show for a finished
        download, including its own per-file rejections (importer.py decides
        which of those it is safe to override).

        Deliberately no movieId: passing one makes Radarr skip its own
        matching and stamp that movie onto every file, which would defeat the
        wrong-movie check in importer.force_import."""
        return self.get("manualimport", params={
            "downloadId": download_id, "filterExistingFiles": "false"}) or []

    def command(self, name: str, **body: Any) -> Any:
        return self.post("command", json={"name": name, **body})

    def command_status(self, command_id: int) -> dict:
        return self.get(f"command/{int(command_id)}",
                        timeout=self.FAST_TIMEOUT) or {}

    def wait_for_command(self, command_id: int, timeout: float = 300.0,
                         interval: float = 3.0) -> dict:
        """Poll a queued command until it finishes. POST /command only QUEUES
        work; success of the POST says nothing about the outcome. Returns the
        last status seen -- check its 'status' ('completed', 'failed', ...)."""
        import time
        deadline = time.monotonic() + timeout
        st: dict = {}
        while True:
            try:
                st = self.command_status(command_id) or st
            except ArrError:
                # A transient blip (proxy 502, socket reset) is not an import
                # failure; keep polling until the deadline.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(interval)
                continue
            if str(st.get("status") or "").lower() in (
                    "completed", "failed", "aborted", "cancelled", "orphaned"):
                return st
            if time.monotonic() >= deadline:
                return st
            time.sleep(interval)

    def root_folders(self) -> list[dict]:
        return self.get("rootfolder") or []

    def diskspace(self) -> list[dict]:
        """Present for display/comparison ONLY. Never drives the loop: on this
        unRAID host shfs reports per-share figures that don't mean what a
        control loop would assume. See space.py."""
        return self.get("diskspace", timeout=self.FAST_TIMEOUT) or []

    def media_management(self) -> dict:
        return self.get("config/mediamanagement", timeout=self.FAST_TIMEOUT) or {}


class Sonarr(ArrClient):
    def series(self) -> list[dict]:
        return self.get("series") or []

    def episode_files(self, series_id: int) -> list[dict]:
        return self.get("episodefile", params={"seriesId": series_id}) or []


def radarr_from_env() -> Radarr:
    from . import db
    url, key = db.get_connection("radarr")
    if not url or not key:
        raise ArrError("Radarr not configured -- set URL + API key in Settings -> Connections")
    return Radarr(url, key)


def sonarr_from_env() -> Sonarr:
    from . import db
    url, key = db.get_connection("sonarr")
    if not url or not key:
        raise ArrError("Sonarr not configured -- set URL + API key in Settings -> Connections")
    return Sonarr(url, key)
