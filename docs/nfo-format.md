# NFO format

Current NFO format: v3.

NFO format version is independent from software and manifest versions. Generated NFO XML contains `<torr2strm formatversion="3" />` inside the root. The existing format remains unchanged in v1.4.7; only the Kodi TV sidecar-generation policy changes.

## Kodi

- Movie: one movie NFO for the torrent-level STRM, containing trusted provider IDs and technical stream details only when already available. When a trustworthy TMDb ID exists, the NFO ends with the canonical Combination URL `/movie/<tmdbid>`.
- Identified TV series: one `tvshow.nfo` at the root of each series directory within its quality root. It contains trusted series IDs and, when available, the Combination URL `/tv/<tmdbid>`. Streamdetails may be copied from the deterministic representative release only if already available.
- TV seasons and episodes: no NFO files are created in season folders or next to episode STRMs. Kodi's internet scraper is responsible for discovering per-episode metadata.
- Unknown TV identity: no speculative `tvshow.nfo` is created.
- Generated NFOs omit human-readable title/name fields so scraper-localized names are not replaced by locally supplied titles.

## Jellyfin

Jellyfin's current per-item NFO generation and output layout are unchanged. Available technical streamdetails and trusted provider IDs continue to be written according to the existing contract.

## Technical metadata and migration

No new ffprobe execution or TorrServer `/ffp/` request is made. If no usable technical details are already present, an NFO that is still required by the relevant output may be identity-only. Cached NFOs are not used to classify the quality root.

NFO format migrations are versioned independently from the software version. v1.4.7 does not introduce a new NFO XML format version, so no NFO-format-version bump is required.
