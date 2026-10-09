# Synchronization workflow

Each run follows this high-level sequence:

1. Load and validate TOML configuration.
2. Enumerate TorrServer and resolve torrent details/FileStats.
3. Build candidate title queries and optionally search JacRed.
4. Accept JacRed enrichment only when a candidate's BTIH/infoHash exactly matches the TorrServer hash.
5. Resolve category: recognized non-empty TorrServer `movie`/`tv` first; exact JacRed category only if the source category is blank; otherwise `_uncategorized`.
6. Determine which video files require media information for enabled outputs.
7. Reuse valid NFO data where possible; otherwise use usable ffprobe data from the exact JacRed result, or request `GET /ffp/{hash}/{file_id}` from TorrServer.
8. Classify the primary eligible video as `4K` when its maximum dimension is at least 2160 pixels, otherwise `1080p`. Do not infer quality from release-title tokens.
9. Build the desired independent Jellyfin and Kodi/Elementum tree layouts.
10. Generate or migrate NFO content, write STRM references and update independent manifests.
11. Remove stale reflections of torrents no longer present in TorrServer.
12. If configured, process reverse deletion from missing managed Jellyfin STRM files using TorrServer `action=rem`; Kodi never triggers source removal.

JacRed lookup is an optional enrichment stage. It can return category, ffprobe stream data, magnet URL and provider IDs from the same exact result. An approximate title match never passes the hash check.

Per-torrent recoverable failures are logged in `DONE` as unresolved/failed results and do not make the oneshot exit non-zero. Fatal configuration or source errors may fail the service.
