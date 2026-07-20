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
        return r.json()

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

    def queue(self, page_size: int = 1000) -> list[dict]:
        q = self.get("queue", params={"pageSize": page_size,
                                      "includeMovie": "true"}) or {}
        return q.get("records", [])

    def delete_movie_file(self, movie_file_id: int) -> Any:
        """Delete the current file for a movie. Honours Radarr's Recycle Bin
        setting, so this is reversible for the retention window -- that is the
        whole basis of the manifest's 'a demotion can be reversed' claim."""
        return self.delete(f"moviefile/{int(movie_file_id)}")

    def manual_import_candidates(self, download_id: str) -> list[dict]:
        """What Radarr's manual-import dialog would show for a finished
        download, including its own rejections (which we deliberately ignore --
        see importer.py)."""
        return self.get("manualimport", params={
            "downloadId": download_id, "filterExistingFiles": "false"}) or []

    def command(self, name: str, **body: Any) -> Any:
        return self.post("command", json={"name": name, **body})

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
