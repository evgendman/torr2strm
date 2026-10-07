# torr2strm

TorrServer -> independent materialized STRM/NFO output trees.

Current implementation baseline: torr2strm 1.3.2.

TorrServer is the source of truth. The synchronizer reads TorrServer FileStats, optionally enriches exact torrent identity through JacRed, resolves real media information, and materializes independent consumer-specific trees.

Current outputs:
- Jellyfin: authoritative read/write projection.
- Kodi/Elementum: read-only projection.

Jellyfin default root: /mnt/torr2strm-media
Kodi default root: /mnt/torr2strm-media-kodi

Each output has its own .torr2strm/manifest.json and .torr2strm/root.marker. Manifest state is never copied between output roots.

Core invariants:
- Never guess movie/TV category from title text.
- Real ffprobe media information is the authority for quality.
- Current manifest schema is v4.
- Current NFO format is v2.
- NFO format version is independent from software and manifest versions.
- Jellyfin is the only reverse-deletion authority.
- Reverse deletion uses TorrServer action=rem permanently.
- action=drop is not used or implemented.
- Kodi/Elementum is read-only and can never remove a source torrent.

The repository contains the executable source, tests, service packaging, specifications and the engineering record of the project.