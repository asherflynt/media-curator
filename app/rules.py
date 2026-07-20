"""Cohort statistics and anomaly classification.

Thresholds are derived from the library itself -- median/IQR per
(quality tier x codec) -- rather than hardcoded from internet lore. Each file is
judged against its own peers, so the rules self-tune as the library changes.
"""
from __future__ import annotations

import statistics as st
from typing import Any, Iterable

GB = 1024 ** 3


def parse_runtime(value: Any) -> float | None:
    """mediaInfo.runTime looks like '1:52:00'. Returns seconds."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value) * 60 if value < 1000 else float(value)
    parts = str(value).split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0.0, nums[0], nums[1]
    else:
        return None
    total = h * 3600 + m * 60 + s
    return total or None


def effective_bitrate(size: int, runtime_seconds: float | None,
                      video_bitrate: Any) -> tuple[float | None, str]:
    """Return (bits-per-second, method).

    Prefers size/duration -- the *total* bitrate -- deliberately, and uses it
    uniformly wherever a runtime exists.

    The reason: 11,008 of 13,524 files report no videoBitrate at all. Mixing a
    reported video-only bitrate for some files with a derived total bitrate for
    others inside the same cohort would compare two different quantities and
    manufacture false outliers -- audio-heavy remuxes would look 'bloated'
    purely from the metric switching under them. One consistent metric per
    cohort matters more than which metric it is, since everything here is a
    comparison against peers rather than an absolute judgement.
    """
    if runtime_seconds and size > 0:
        return (size * 8.0) / runtime_seconds, "size/duration"
    try:
        vb = float(video_bitrate or 0)
    except (TypeError, ValueError):
        vb = 0.0
    if vb > 0:
        return vb, "reported"
    return None, "none"


def file_record(kind: str, title: str, path: str | None, size: int,
                quality: dict, media_info: dict, ref_id: int | None = None,
                fallback_runtime_min: Any = None) -> dict:
    q = (quality or {}).get("quality") or {}
    mi = media_info or {}
    runtime = parse_runtime(mi.get("runTime"))
    if runtime is None and fallback_runtime_min:
        runtime = parse_runtime(fallback_runtime_min)
    br, method = effective_bitrate(size, runtime, mi.get("videoBitrate"))
    return {
        "kind": kind,
        "ref_id": ref_id,
        "title": title,
        "path": path,
        "size": size,
        "tier": q.get("name") or "?",
        "resolution": q.get("resolution") or 0,
        "codec": mi.get("videoCodec") or "?",
        "runtime": runtime,
        "bitrate": br,
        "bitrate_method": method,
        "media_info": mi,
    }


def cohorts(records: Iterable[dict], min_size: int = 8) -> dict[tuple[str, str], dict]:
    buckets: dict[tuple[str, str], list[float]] = {}
    for r in records:
        if r["bitrate"] and r["bitrate"] > 0:
            buckets.setdefault((r["tier"], r["codec"]), []).append(r["bitrate"])
    out = {}
    for key, vals in buckets.items():
        if len(vals) < min_size:
            continue  # too sparse to judge against; skipped rather than guessed
        vals.sort()
        out[key] = {
            "n": len(vals),
            "median": st.median(vals),
            "p25": vals[len(vals) // 4],
            "p75": vals[(len(vals) * 3) // 4],
        }
    return out


def classify(records: list[dict], settings: dict) -> list[dict]:
    """Flag files whose size contradicts the quality they claim to be."""
    bloated_ratio = float(settings.get("bloated_ratio", 2.5))
    under_ratio = float(settings.get("underweight_ratio", 0.4))
    min_cohort = int(settings.get("min_cohort_size", 8))

    co = cohorts(records, min_cohort)
    tier_medians = _tier_medians(records, min_cohort)
    findings: list[dict] = []

    for r in records:
        if r["size"] == 0 or not r["runtime"]:
            findings.append(_finding(r, "broken", None, None,
                                     "zero bytes or no runtime/mediaInfo"))
            continue
        if not r["bitrate"]:
            continue

        # Suspected upscale: a 2160p file thinner than its OWN source family at
        # 1080p. Checked before the tier x codec gate below, because this only
        # needs the tier median -- gating it on a codec cohort would exempt
        # files with unusual codecs, which are the ones most likely to be odd.
        #
        # Compared against the sibling tier rather than "all 1080p files"
        # because tiers span wildly different compression regimes. Measured on
        # this library, the all-1080p median is 27.4 Mbps -- but that cohort is
        # 155/226 Remux-1080p (lossless, 31.4 Mbps), which drags it above the
        # legitimate WEBDL-2160p median of 19.7. A cross-resolution rule
        # therefore flagged 276 files, 202 of them healthy 4K WEB-DLs, because
        # it was comparing lossless 1080p against lossy 4K. Family-to-family
        # (WEBDL-2160p vs WEBDL-1080p) compares like with like: 26 flags.
        if r["resolution"] and r["resolution"] >= 2160:
            sib = _sibling_1080p_tier(r["tier"])
            sib_med = tier_medians.get(sib) if sib else None
            if sib_med and r["bitrate"] < sib_med:
                findings.append(_finding(
                    r, "upscale", sib_med, r["bitrate"] / sib_med,
                    f"2160p carrying less bitrate than a typical {sib}"))

        stats = co.get((r["tier"], r["codec"]))
        if not stats:
            continue  # cohort too sparse to judge against; skipped, not guessed
        med = stats["median"]
        if not med:
            continue
        ratio = r["bitrate"] / med

        if ratio > bloated_ratio:
            findings.append(_finding(r, "bloated", med, ratio,
                                     f"{ratio:.2f}x its {r['tier']}/{r['codec']} cohort"))
        elif ratio < under_ratio:
            # The valuable class: the arr DB believes this is a high tier, so it
            # is 'at cutoff' and will never be upgrade-searched. Invisible
            # without this check.
            findings.append(_finding(r, "underweight", med, ratio,
                                     f"{ratio:.2f}x its {r['tier']}/{r['codec']} cohort -- "
                                     "likely mislabelled; arr will never upgrade it"))
    return findings


def _sibling_1080p_tier(tier: str) -> str | None:
    return tier.replace("2160p", "1080p") if "2160p" in tier else None


def _tier_medians(records: list[dict], min_size: int) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for r in records:
        if r["bitrate"] and r["bitrate"] > 0:
            buckets.setdefault(r["tier"], []).append(r["bitrate"])
    return {t: st.median(v) for t, v in buckets.items() if len(v) >= min_size}


def _finding(r: dict, klass: str, median: float | None,
             ratio: float | None, detail: str) -> dict:
    return {
        "kind": r["kind"],
        "klass": klass,
        "ref_id": r.get("ref_id"),
        "title": r["title"],
        "path": r["path"],
        "size": r["size"],
        "tier": r["tier"],
        "codec": r["codec"],
        "bitrate": r["bitrate"],
        "cohort_median": median,
        "ratio": ratio,
        "detail": detail,
    }


def find_duplicates(records: list[dict]) -> list[dict]:
    seen: dict[tuple, list[dict]] = {}
    for r in records:
        if r.get("ref_id") is None:
            continue
        seen.setdefault((r["kind"], r["ref_id"]), []).append(r)
    out = []
    for (_kind, _ref), group in seen.items():
        if len(group) > 1:
            for r in group:
                out.append(_finding(r, "duplicate", None, None,
                                    f"{len(group)} files for one item"))
    return out
