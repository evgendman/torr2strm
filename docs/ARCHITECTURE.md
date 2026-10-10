# Architecture

## Source of truth and processing path

TorrServer is the sole source of truth for torrent membership, category, info hash, metadata and FileStats. A synchronization pass reads only data already present on TorrServer, reuses cached NFO streamdetails where available, then reconciles independent Jellyfin and Kodi/Elementum projections.

```text
TorrServer torrent list + FileStats + custom metadata
        ↓
category only from TorrServer category field (unknown → _uncategorized)
        ↓
read ffprobe / structured metadata / title already present in TorrServer data
        ↓
if TorrServer ffprobe proves 4K → quality_root=4K; else quality_root=1080p
        ↓
reuse exact-file NFO streamdetails where available; never probe
        ↓
write STRM + NFO
        ↓
Jellyfin output (existing paths) + normalized Kodi output
```

## Category, quality, identity and display labels

Category comes only from TorrServer's top-level category field. Supported movie/tv values are used; a blank or unsupported category remains _uncategorized. No title-based inference, provider lookup, or enrichment can change it.

Only usable ffprobe stream data already embedded in TorrServer metadata can select the quality root:
- 4K when the maximum video dimension is at least 3840 pixels.
- 1080p when ffprobe is missing, invalid, or does not prove 4K.

Structured quality fields and explicit release-title markers already in TorrServer data may contribute to the visible basename label, but cannot select the 4K root. Cached NFOs may preserve exact-file fileinfo/streamdetails for output only; they cannot determine category, root, or display label. The importer never calls JacRed, Prowlarr, other external sources, TorrServer /ffp/, or local ffprobe.

Trusted IDs for Kodi grouping must already exist in TorrServer metadata. Unknown IDs, season numbers, and episode numbers are never invented; title similarity never merges unknown identities.

## NFO contract

No human-readable display title/name fields are written to any NFO. This is intentional: Kodi was observed to replace its scraper-localized title with NFO title values after scraping; Jellyfin should obtain localized names by provider ID.

- Movie NFO: trusted IDs, per-file ffprobe stream details, and a Combination NFO scraper URL when a trustworthy TMDb ID is available.
- `tvshow.nfo`: trusted series IDs, Combination NFO URL, and streamdetails copied from the deterministic representative release for that quality root; no title. Each episode sidecar NFO retains its own exact source-file streamdetails.
- Episode NFO: ordinary episode NFO, trusted IDs, known season/episode coordinates, and stream details from that exact source file; no Combination URL.
- If technical ffprobe data is not already present in the exact release payload or a valid cached NFO, the synchronizer still creates the STRM and an identity-only NFO. It never contacts TorrServer `/ffp/` and does not perform local probing.

NFO format is v3. It is distinct from software/manifest versions. Current development trees are cleaned and rebuilt; no physical directory-tree migration is performed. If an older NFO is encountered through manifest-based cache reuse, title-like fields are removed during format upgrade.

## Rebuild and safety

For v1.4.0, stop the service and timer, disable Jellyfin reverse deletion while clearing state, empty only the configured Jellyfin and Kodi output roots (including their `.torr2strm` manifests and root markers), then run a fresh sync. Do not remove arbitrary parent directories. `action=drop` is never used. The independent `hotcached` project is out of scope.
