# Architecture

## Source of truth and processing path

TorrServer is the source of truth for torrent membership, info hash and FileStats. A synchronization pass loads the torrent and its files, optionally enriches with an exact-hash JacRed result, resolves per-file media information, determines one torrent-level quality root/label from the primary eligible video, then reconciles independent Jellyfin and Kodi/Elementum projections.

```text
TorrServer torrent list + FileStats
        ↓
optional JacRed search + exact BTIH/infoHash validation
        ↓
resolve category and trustworthy provider IDs
        ↓
reusable NFO → exact JacRed ffprobe → TorrServer /ffp/ for primary file
        ↓ if primary ffprobe unavailable
structured quality field → explicit release-title quality markers → unknown
        ↓
quality_root (4K / 1080p) + quality_label (480p / 720p / 1080p / 2160p HDR / ...)
        ↓
per-file ffprobe as needed for each file's own NFO
        ↓
Jellyfin output (existing path/grouping behavior) + normalized Kodi output
```

## JacRed matching and identity

JacRed is optional enrichment, never a substitute for TorrServer's torrent inventory. A candidate is accepted only when its `infoHash`, `guid` or BTIH extracted from its magnet/download URL exactly matches the TorrServer hash. Approximate title similarity alone is not sufficient.

Provider IDs are used to establish logical movie/series identity. For Kodi, identified releases with the same logical identity share one readable directory per quality root. Unknown series identity does not cause title-based fuzzy merging: the release title names the directory and the torrent's inner file hierarchy is preserved. Missing IDs, season numbers and episode numbers are never fabricated.

## Category and output roots

Category resolution remains unchanged:

1. A non-empty TorrServer category equal to `movie` or `tv` after normalization wins.
2. JacRed category may be used only when TorrServer category is genuinely blank and the hash match is exact.
3. If still unknown, use `_uncategorized`; anime is not silently treated as TV.

The quality root has only two values:

- `4K` when the primary eligible video's `max(width, height) >= 3840`;
- `1080p` for everything else, including unknown quality.

The primary eligible video is the largest playable video file, with path as deterministic tie-breaker. One root and one display quality label are used for the whole torrent. We do not call ffprobe separately on each episode just to classify it, but retain per-file ffprobe requests when needed to write truthful stream data to the matching NFO.

## Quality-label fallback

The root and display label are independent. A 720p or 480p item may live under `1080p`, and a release with unknown quality also lives under `1080p` but receives no quality suffix.

Resolution/display label priority:

1. Real primary-file ffprobe stream dimensions and HDR/DV indicators (or valid cached NFO stream details for that exact file).
2. Structured quality/resolution fields from TorrServer metadata or a hash-exact JacRed match.
3. Explicit quality markers in the torrent title.
4. Unknown.

Explicit labels are normalized (for example, `480p`, `720p`, `1080p`, `1080i`, `1440p`, `2160p`, `2160p HDR`, `2160p DV`). Source/codec tokens such as `WEB-DL`, `BluRay`, `HEVC`, or `HD` do not establish resolution by themselves. Unknown label means no quality suffix in the basename, not a claim that the media is 1080p.

## Jellyfin output

Jellyfin's current per-torrent directory-building/grouping behavior is the compatibility contract and is not redesigned for the Kodi project. Jellyfin continues to create file-level STRM entries with TorrServer `/play/{hash}/{file_id}` links. Its paths and reverse-delete behavior must be covered by regression tests.

Jellyfin is the only authoritative/read-write output. A tracked Jellyfin STRM deletion may remove a source torrent only when reverse deletion is explicitly enabled, through `action=rem`. `action=drop` is forbidden.

## Kodi/Elementum output

Kodi has an independently planned path tree:

- Identified series: `tv/<quality-root>/<Series Name> (<Year if known>)/Season NN/`.
- Identified movies: `movie/<quality-root>/<Movie Name> (<Year if known>)/`.
- Unknown TV identity: `tv/<quality-root>/<release title>/<original torrent file hierarchy>/`.
- Unknown movie/other identity: a torrent-release-title directory, preserving available source layout where individual file items are materialized.

A known series directory has no torrent hash. If genuinely different series IDs collide on the same readable name/year, append the namespaced provider ID only to resolve that collision. Distinct fallback torrents get a short-hash directory suffix only if their title would otherwise collide.

Every Kodi item release remains physically distinct. Its basename includes a readable title/episode coordinate where known, then ` — <quality-label>` if known, then `[<short torrent hash>]` immediately before the extension. Start with 8 hash characters and lengthen only when short-hash strings collide. If two files in one torrent map to the same logical episode, append a file ordinal only to those colliding items.

Playback semantics do not change:

- Movies: one torrent-level Elementum STRM per release, no `oindex`.
- TV: one STRM per playable file with `oindex` equal to that source file's original zero-based FileStats order.
- Kodi output is read-only and never removes a TorrServer torrent.

## Manifest and shared-folder reconciliation

Each output has its own root marker and manifest. The manifest is the source of truth mapping each STRM/NFO to full torrent hash, source file path/id/order, and output-relative paths. Filenames and short hashes are display aids, never deletion identities.

Kodi series/season directories may be shared by several torrents. The synchronizer reconciles each torrent's entries independently; it removes only old managed files no longer referenced by any new record and prunes directories only when empty. Removing one torrent cannot delete another release's STRM or a still-used `tvshow.nfo`. Source torrents disappearing from TorrServer are reflected in all enabled outputs on the next sync.

## NFO contract

No human-readable display title/name fields are written to any NFO. This is intentional: Kodi was observed to replace its scraper-localized title with NFO title values after scraping; Jellyfin should obtain localized names by provider ID.

- Movie NFO: trusted IDs, per-file ffprobe stream details, and a Combination NFO scraper URL when a trustworthy TMDb ID is available.
- `tvshow.nfo`: trusted series IDs, Combination NFO URL, and streamdetails copied from the deterministic representative release for that quality root; no title. Each episode sidecar NFO retains its own exact source-file streamdetails.
- Episode NFO: ordinary episode NFO, trusted IDs, known season/episode coordinates, and stream details from that exact source file; no Combination URL.
- If ffprobe is unavailable, the synchronizer still creates the STRM and an identity-only NFO. That NFO is not treated as a technical-data cache.

NFO format is v3. It is distinct from software/manifest versions. Current development trees are cleaned and rebuilt; no physical directory-tree migration is performed. If an older NFO is encountered through manifest-based cache reuse, title-like fields are removed during format upgrade.

## Rebuild and safety

For v1.4.0, stop the service and timer, disable Jellyfin reverse deletion while clearing state, empty only the configured Jellyfin and Kodi output roots (including their `.torr2strm` manifests and root markers), then run a fresh sync. Do not remove arbitrary parent directories. `action=drop` is never used. The independent `hotcached` project is out of scope.
