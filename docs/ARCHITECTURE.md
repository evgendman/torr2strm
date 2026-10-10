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

Display-label priority:
1. ffprobe JSON already included in TorrServer metadata.
2. Structured quality/resolution fields already present in TorrServer metadata.
3. Explicit quality markers in the TorrServer release title.
4. Unknown (omit the quality suffix).

Explicit labels are normalized (for example, 480p, 720p, 1080p, 1080i, 1440p, 2160p, 2160p HDR, 2160p DV). Source/codec tokens such as WEB-DL, BluRay, HEVC, or HD do not establish resolution by themselves. Unknown label means no quality suffix in the basename, not a claim that the media is 1080p.

Trusted IDs for Kodi grouping must already exist in TorrServer metadata. Unknown IDs, season numbers, and episode numbers are never invented; title similarity never merges unknown identities.

## Jellyfin output

Jellyfin's current per-torrent directory-building/grouping behavior is the compatibility contract and is not redesigned for the Kodi project. Jellyfin continues to create file-level STRM entries with TorrServer /play/{hash}/{file_id} links. Its paths and reverse-delete behavior must be covered by regression tests.

Jellyfin is the only authoritative/read-write output. A tracked Jellyfin STRM deletion may remove a source torrent only when reverse deletion is explicitly enabled, through action=rem. action=drop is forbidden.

## Kodi/Elementum output

Kodi has an independently planned path tree:

- Identified series: tv/<quality-root>/<Series Name> (<Year if known>)/Season NN/.
- Identified movies: movie/<quality-root>/<Movie Name> (<Year if known>)/.
- Unknown TV identity: tv/<quality-root>/<release title>/<original torrent file hierarchy>/.
- Unknown movie/other identity: a torrent-release-title directory, preserving available source layout where individual file items are materialized.

A known series directory has no torrent hash. If genuinely different series IDs collide on the same readable name/year, append the namespaced provider ID only to resolve that collision. Distinct fallback torrents get a short-hash directory suffix only if their title would otherwise collide.

Every Kodi item release remains physically distinct. Its basename includes a readable title/episode coordinate where known, then — <quality-label> if known, then [<short torrent hash>] immediately before the extension. Start with 8 hash characters and lengthen only when short-hash strings collide. If two files in one torrent map to the same logical episode, append a file ordinal only to those colliding items.

Kodi directory and filename components, including torrent-internal folders for unidentified content, are sanitized for Windows/SMB: forbidden characters are replaced, reserved device names are prefixed, and trailing dots/spaces are removed. `.strm` and `.nfo` extensions stay intact. This is Kodi-only; Jellyfin's Linux path naming is unchanged. The next normal sync reconciles old managed paths with the sanitized paths recorded in the manifest; do not rename managed files manually.

Playback semantics do not change:

- Movies: one torrent-level Elementum STRM per release, no oindex.
- TV: one STRM per playable file with oindex equal to that source file's original zero-based FileStats order.
- Kodi output is read-only and never removes a TorrServer torrent.

## Manifest and shared-folder reconciliation

Each output has its own root marker and manifest. The manifest is the source of truth mapping each STRM/NFO to full torrent hash, source file path/id/order, and output-relative paths. Filenames and short hashes are display aids, never deletion identities.

Kodi series/season directories may be shared by several torrents. The synchronizer reconciles each torrent's entries independently; it removes only old managed files no longer referenced by any new record and prunes directories only when empty. Removing one torrent cannot delete another release's STRM or a still-used tvshow.nfo. Source torrents disappearing from TorrServer are reflected in all enabled outputs on the next sync.

## NFO contract

No human-readable display title/name fields are written to any NFO. This is intentional: Kodi was observed to replace its scraper-localized title with NFO title values after scraping; Jellyfin should obtain localized names by provider ID.

- Movie NFO: trusted IDs, per-file ffprobe stream details, and a Combination NFO scraper URL when a trustworthy TMDb ID is available.
- `tvshow.nfo`: trusted series IDs, Combination NFO URL, and streamdetails copied from the deterministic representative release for that quality root; no title. Each episode sidecar NFO retains its own exact source-file streamdetails.
- Episode NFO: ordinary episode NFO, trusted IDs, known season/episode coordinates, and stream details from that exact source file; no Combination URL.
- If technical ffprobe data is not already present in the exact release payload or a valid cached NFO, the synchronizer still creates the STRM and an identity-only NFO. It never contacts TorrServer `/ffp/` and does not perform local probing.

NFO format is v3. It is distinct from software/manifest versions. Current development trees are cleaned and rebuilt; no physical directory-tree migration is performed. If an older NFO is encountered through manifest-based cache reuse, title-like fields are removed during format upgrade.

## Rebuild and safety

For v1.4.0, stop the service and timer, disable Jellyfin reverse deletion while clearing state, empty only the configured Jellyfin and Kodi output roots (including their `.torr2strm` manifests and root markers), then run a fresh sync. Do not remove arbitrary parent directories. `action=drop` is never used. The independent `hotcached` project is out of scope.
