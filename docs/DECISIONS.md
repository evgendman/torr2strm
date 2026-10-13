# Accepted decisions

## NFO format versioning
NFO format version is independent from torr2strm software version and manifest version. Current NFO format is v3. The Kodi TV sidecar-policy change does not alter the serialized format of NFOs that remain, so v1.4.7 does not bump the NFO format version.

## NFO placement
For Kodi movies, preserve the existing movie NFO behavior. For identified Kodi TV series, create one `tvshow.nfo` in the series root for each quality root. Do not create NFOs in season folders or beside episode STRMs; Kodi's internet scraper should discover episode metadata. Do not create a speculative root NFO for unknown series. Jellyfin's existing per-item NFO output remains unchanged.

## Combination NFO URL
For NFO v3, one canonical TMDb Combination URL is written after the XML root when a trustworthy TMDb ID is available. Movie roots use `/movie/<tmdbid>`; TV series roots use `/tv/<tmdbid>`. Jellyfin NFO behavior remains unchanged by the Kodi TV sidecar policy.

## Output authority
Jellyfin is authoritative/read-write. Kodi/Elementum is read-only.

## Reverse deletion
Only a managed Jellyfin STRM may initiate reverse deletion. The only supported TorrServer operation is `action=rem`. `action=drop` is deliberately absent.

## Manifests
Every enabled output has its own independent manifest. Current manifest schema is v5. The manifest is authoritative for mapping managed paths to torrent/source-file identity. Shared Kodi `tvshow.nfo` entries must remain referenced while any release uses that series root; old episode sidecars from pre-v1.4.7 trees are removed during the clean rebuild or stale-path reconciliation.

## Quality
Quality roots are `4K` and `1080p`. Usable video dimensions from ffprobe already embedded in TorrServer metadata take priority: maximum dimension >= 3840 is `4K`; lower usable dimensions are `1080p`. If usable video dimensions are unavailable, an explicit `4K` or `2160p` marker in the release title may select `4K`. Title markers do not override usable non-4K dimensions. Structured quality fields and cached NFOs do not independently classify the root.

## Source model
TorrServer remains the source of truth. torr2strm materializes playback references and metadata; it does not copy the underlying media. The project does not run local ffprobe, call TorrServer `/ffp/`, or perform external metadata lookups.
