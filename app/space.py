"""Free space and on-disk truth, measured from the filesystem.

Why not Radarr's /api/v3/diskspace: this is an unRAID host, where /mnt/user is
a FUSE union (shfs) that computes free space PER SHARE based on which disks the
share includes. Radarr was measured returning two different answers for the same
~66 TiB array (/config: 5.47 TiB free, /downloads: 5.00 TiB free). Both are real
but share-scoped -- neither means "free space on the array", which is what a
control loop would assume. So we measure one deliberate, known-scoped path.

The same mount also gives us true on-disk sizes and orphan detection: Radarr
reported 33 folders it doesn't track, which are invisible to any API-derived
total.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

GB = 1024 ** 3


@dataclass
class SpaceReading:
    path: str
    free_bytes: int
    total_bytes: int
    used_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / GB

    @property
    def total_gb(self) -> float:
        return self.total_bytes / GB


def read_space(path: str) -> SpaceReading:
    st = os.statvfs(path)
    # f_frsize is the fragment size; f_bavail excludes root-reserved blocks,
    # which is what a non-root writer actually gets.
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return SpaceReading(path=path, free_bytes=free, total_bytes=total,
                        used_bytes=total - free)


def media_path() -> str:
    # The container mount point; stays an env var since it's tied to the volume.
    return os.environ.get("MC_MEDIA_PATH", "/media")


def _setting(key: str, default: str) -> str:
    from . import db
    val = db.get(key)
    return str(val) if val else default


def movies_dir() -> str:
    return os.path.join(media_path(), _setting("media_movies_subdir", "Movies"))


def arr_path_to_local(arr_path: str) -> str:
    """Translate a Radarr container path to our mount.

    Radarr sees /downloads/Movies/...; we mount the same share at /media.
    This mapping is UI-editable and MUST be verified before the first live run
    -- check_mapping() below is what the dashboard calls.
    """
    root = _setting("radarr_root", "/downloads/Movies").rstrip("/")
    if arr_path.startswith(root):
        rel = arr_path[len(root):].lstrip("/")
        return os.path.join(movies_dir(), rel)
    return arr_path


def check_mapping(sample_arr_paths: list[str]) -> dict:
    """Verify the Radarr-path -> local-mount translation actually resolves.

    The whole orphan walk and every size reading depends on this being right,
    and it is asserted by config rather than discovered -- so it gets checked
    explicitly rather than assumed.
    """
    if not os.path.isdir(movies_dir()):
        return {"ok": False, "reason": f"{movies_dir()} is not a directory. "
                                       "Check the bind mount and MC_MEDIA_MOVIES_SUBDIR.",
                "checked": 0, "hits": 0}
    hits = 0
    checked = 0
    misses: list[str] = []
    for p in sample_arr_paths[:40]:
        checked += 1
        local = arr_path_to_local(p)
        if os.path.exists(local):
            hits += 1
        elif len(misses) < 5:
            misses.append(f"{p} -> {local}")
    ok = checked > 0 and hits / checked > 0.9
    return {
        "ok": ok,
        "checked": checked,
        "hits": hits,
        "misses": misses,
        "reason": "" if ok else (
            "Radarr paths do not resolve under the mount. /downloads/Movies "
            "(Radarr) and /mnt/user/media (Tdarr) may not be the same share. "
            "Do not leave dry-run until this resolves."
        ),
    }


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def walk_movie_folders() -> dict[str, int]:
    """Top-level movie folder -> total bytes on disk."""
    base = movies_dir()
    out: dict[str, int] = {}
    if not os.path.isdir(base):
        return out
    with os.scandir(base) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False):
                out[entry.name] = dir_size(entry.path)
    return out


def find_orphans(known_folder_names: set[str]) -> list[dict]:
    """Folders on disk that Radarr doesn't track.

    These consume real space while being invisible to every API-derived total,
    which is why the API figure is a floor rather than the truth.
    """
    on_disk = walk_movie_folders()
    return [
        {"name": name, "size": size, "path": os.path.join(movies_dir(), name)}
        for name, size in sorted(on_disk.items(), key=lambda kv: -kv[1])
        if name not in known_folder_names
    ]
