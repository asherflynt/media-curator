<img src="icon.png" width="88" align="right" alt="media-curator">

# media-curator

**Reclaim disk space in your Radarr/Sonarr library by demoting over-quality files — 4K remuxes to 4K WEB-DL, and anything bigger than its quality profile wants — using Radarr's own download‑verify‑replace path. It never re‑encodes.**

![license](https://img.shields.io/badge/license-MIT-blue)
![image](https://img.shields.io/badge/image-ghcr.io%2Fasherflynt%2Fmedia--curator-blue)
![unRAID](https://img.shields.io/badge/unRAID-Community%20Apps-orange)

media-curator watches your library for files whose size contradicts the quality they claim to be, and holds a configurable amount of free disk by demoting the biggest, oldest titles first. Free space is read from the filesystem (`statvfs`), **not** Radarr's per‑share numbers — so it's correct on unRAID/ZFS, where the array can report terabytes free while the disk a download lands on is full.

Everything is configured in the web UI, *ARR‑style. No config files, no environment variables for keys.

## Why demote instead of transcode?

Transcoding targets the wrong bytes. Measured against a real ~64 TB library, H.264 files were **~5.6 % of the disk** and would recover a couple percent for **100+ hours of encoding and permanent quality loss** — while **4K remuxes held ~61 % of storage** and are skipped by an HEVC plugin's "don't reconvert HEVC" guard. Demoting a 4K remux to a 4K WEB‑DL reclaims **~40+ GB per title, keeps 4K and HDR**, and leaves Radarr's database honest. You give up lossless video and usually TrueHD/Atmos — not resolution — and it's reversible while the old file is still in the Recycle Bin.

Crucially, demotion goes through **Radarr's native path**: switch the title to an archive quality profile, force‑grab a smaller release, and Radarr downloads, verifies, imports, and only *then* removes the old file. There is never a window with no file.

## Features

- **Free‑space loop** — set "keep N TB free"; it demotes down a ranked list (reclaimable bytes + film age) until the target is met, then idles.
- **Reads real free space** via `statvfs` on the mount — never Radarr's misleading per‑share figure.
- **Quality audit** — flags *underweight* (Radarr thinks it's a Bluray, the file is tiny), *bloated*, *suspected upscales*, *orphans* (folders Radarr doesn't track), *broken*, and *duplicate* files, judged against per‑cohort medians derived from your own library.
- **Rule‑based profile assignment** — match by **genre / collection / tag** → assign a quality profile (e.g. `genre = Family → Archive‑HD`). New matching titles are kept in sync automatically, and the curator force‑grabs the replacement Radarr won't auto‑search for.
- **Tier fallback** — when the profile's top quality has no release, it walks *down* that profile's own allowed list (Bluray‑1080p → WEBDL‑1080p → WEBRip‑1080p) instead of skipping the title forever. Never picks a tier at or above the file it's replacing.
- **Clears blocked imports** — Radarr refuses to *import* a downgrade even after the profile changes, and parks the finished download waiting for a manual import. The curator does that itself: delete the old file (Recycle Bin if Radarr has one, permanent if not), then `ManualImport`. By default the scheduled sweep only touches downloads whose *sole* rejection is the downgrade — `auto_import_upgrade_only`.
- **Protection that's honored on every track** — a hard new‑release window, an in‑app blocklist, and a Radarr keep‑tag. A classic that's also a kids movie stays protected.
- **Safe by default** — starts in **dry‑run**; every action is written to a reversible manifest.

## Install

### unRAID — Community Applications
Search **media‑curator** in the **Apps** tab. Set the **Media (read‑only)** path to the real dataset/share that holds your movies, start it, then open the WebUI and configure Radarr/Sonarr under **Connections**.

To add the template before it's in the CA feed: *Docker → Add Container → Template* →
`https://raw.githubusercontent.com/asherflynt/media-curator/main/media-curator.xml`

### Docker
```bash
docker run -d --name media-curator \
  -p 8420:8420 \
  -v /mnt/user/appdata/media-curator:/data \
  -v /mnt/cluster/Media:/media:ro \
  ghcr.io/asherflynt/media-curator:latest
```

### docker compose
```yaml
services:
  media-curator:
    image: ghcr.io/asherflynt/media-curator:latest
    container_name: media-curator
    restart: unless-stopped
    ports: ["8420:8420"]
    volumes:
      - ./data:/data
      - /mnt/cluster/Media:/media:ro   # your media share, READ-ONLY, real dataset path
```

Then open `http://<host>:8420`.

## Configuration (all in the WebUI)

1. **Connections** — enter Radarr and Sonarr URL + API key (Radarr/Sonarr → Settings → General → API Key). There's a **Test** button.
2. **Media mount** — mount your media share **read‑only** at `/media`, pointed at the **real dataset/share** (e.g. `/mnt/cluster/Media`, not the `/mnt/user` FUSE union) so free‑space and orphan detection are accurate. media‑curator never writes to it.
3. **Archive profile — the one trick that matters.** Create a quality profile (e.g. `Archive‑4K`) in Radarr and drag the **target quality to the TOP** of the quality ordering, set it as the cutoff. Radarr decides upgrades by *ordering*, not checkboxes: with the target ranked top, nothing outranks it (so nothing re‑upgrades the title back), and it *is* an upgrade over the existing bigger file. The dashboard shows a red badge if a managed profile is ever edited into a state that breaks this.
4. **First run** — leave dry‑run on, review **Candidates**, set the free‑space target just above current free so a title or two qualify, then turn dry‑run off.

## How it works

- **Free space** is measured with `os.statvfs` on the read‑only mount — the one number the control loop trusts. Radarr's `/api/v3/diskspace` is shown only for comparison.
- **Ranking** is reclaimable bytes + film age. No watch‑history dependency.
- **The loop never over‑demotes**: it counts in‑flight downloads (and the Recycle Bin) toward projected free space and stops at the target, so it can't drain the library chasing a number that hasn't moved yet.
- **Two tracks that never fight**: the space loop handles size‑driven demotions; rule‑managed titles (and anything on a managed archive profile) are handed off to the assignment/grab track instead.
- **A grab and an import are two different fights.** Profile *ordering* decides what Radarr searches for and grabs. The import guard is a separate, blunter rule that compares the two files by quality‑definition *weight* — a global ranking where `Remux-2160p` simply outweighs `Bluray-1080p`, which no profile ordering changes. That's why a demotion downloads fine and then dies on the doorstep with *"Not an upgrade for existing movie file"*. The import sweep resolves it by deleting the existing file first (this is what actually frees the bytes; with a Radarr Recycle Bin configured it also stays recoverable, without one the delete is permanent and the sweep runs anyway), which removes the thing being compared against, then issuing `ManualImport`, which bypasses the upgrade check by design. Only downloads blocked on that exact rejection, for movies on a curator‑managed profile, are ever touched.

## Development

```bash
pip install -r requirements.txt
# exercise the engine against a live Radarr (read-only):
MC_RADARR_URL=... MC_RADARR_KEY=... MC_SONARR_URL=... MC_SONARR_KEY=... python verify.py

# tier ladder + blocked-import logic, against fakes (no Radarr needed):
python verify_offline.py
```

Images are built and pushed to GHCR by GitHub Actions on every push to `main` and on `v*` tags.

## License

MIT — see [LICENSE](LICENSE).
