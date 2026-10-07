# Accepted decisions

## NFO format versioning
NFO format version is independent from torr2strm software version and manifest version. Current NFO format is v2.

## NFO migration
Legacy/unversioned torr2strm NFOs are treated as v1 and migrated to v2 during normal synchronization without a new media probe.

## Combination NFO URL
For v2, one canonical TMDb Combination URL is written after the XML root when a trustworthy TMDb ID is available. Movie roots use /movie/<tmdbid>; TV roots use /tv/<tmdbid>. Episode NFOs do not receive the trailing URL.

## Output authority
Jellyfin is authoritative/read-write. Kodi/Elementum is read-only.

## Reverse deletion
Only a managed Jellyfin STRM may initiate reverse deletion. The only supported TorrServer operation is action=rem. action=drop is deliberately absent.

## Manifests
Every enabled output has its own independent manifest. Current manifest schema is v4; old v3 state is intentionally rejected.

## Quality
Quality classification is based on real media dimensions obtained from NFO/JacRed/TorrServer media information, never on release-title tokens.

## Source model
TorrServer remains the source of truth. torr2strm materializes playback references and metadata; it does not copy the underlying media.