# Changelog

## 1.4.5

- Removed runtime lookups to JacRed, Prowlarr, and all other external metadata providers.
- Category comes only from TorrServer's category field; missing or unsupported values remain in `_uncategorized`.
- A torrent enters `4K` only when ffprobe already embedded in TorrServer metadata proves 4K-class dimensions; otherwise it enters `1080p`.
- Preserved Jellyfin output paths, normalized Kodi/Elementum tree, manifests, NFO contracts and safe reconciliation.
- Removed JacRed configuration/CLI options; legacy `[jacred]` sections are ignored.

## 1.4.4

- Restored the original direct JacRed v2 JSON API call at `/api/v2.0/indexers/all/results`, matching the existing `jacred2prowlarr` client.
- Uses the native `q`, `category=movie_`/`tv_`, `limit`, and optional `year` parameters plus the established JSON Accept/User-Agent headers.
- Parses JacRed's `Results` JSON array and normalizes native fields including `Title`, `MagnetUri`, `Category`, and nested `info` metadata.
- Added one-second spacing between upstream requests and capped candidate query variants to avoid issuing requests for years and release-audio/codec fragments.
- Removed the mistaken Torznab XML request path from the direct client.

## 1.4.3

- Replaced the Prowlarr Search Feed (`/api/v1/search`) with JacRed's public native v2 JSON endpoint (`/api/v2.0/indexers/all/results`); torr2strm no longer needs a local Prowlarr instance or its indexer ID.
- Parses Torznab RSS items and extended attributes, including `infohash`, magnet URL, category and year.
- Sends standard Torznab `t`, `q`, `limit`, `extended` and optional `apikey` query parameters.
- Fixed exact-hash matching to check every candidate independently so an unrelated GUID/hash cannot mask a matching BTIH.
- Added regression tests for XML parsing, direct endpoint parameters and exact-match selection.

## 1.4.2

- Fixed exact-hash matching when a JacRed/Prowlarr result has a non-empty result-page URL in `guid` and the actual BTIH is present in `magnetUrl`/`downloadUrl`.
- Hash candidates are now parsed independently; an invalid or non-hash `guid` no longer prevents checking a later valid hash/magnet field.
- Added regression tests for a page URL plus matching magnet hash, and for a direct hexadecimal `infoHash`.

## 1.4.1

- Removed all TorrServer `/ffp/{hash}/{file_id}` requests and local ffprobe execution from quality classification.
- Classifies using only already-available ffprobe payloads/cached NFO, structured quality fields and explicit release-title markers.
- Sends a torrent to `4K` if any available source provides affirmative 4K evidence; otherwise it goes to `1080p`.
- Keeps the display quality label separate from the root; unknown label means no quality suffix.
- Writes stream details into NFO only when already available; otherwise NFO contains identity data only.
- Legacy `[quality]` timeout/retry settings are ignored. Manifest v5 and NFO v3 are unchanged.

## 1.4.0

- Implemented a Kodi-specific normalized media tree grouped by trusted movie/series identity, without changing Jellyfin's existing directory-building behavior.
- Added one shared Kodi series directory per quality root with `Season NN` children for identified series; unknown series retain the release-title directory and original torrent-internal hierarchy.
- Separated the two-value quality root (`4K` / `1080p`) from the visible per-release quality label. Quality falls back from primary-file ffprobe/cache to structured metadata, explicit release-title markers, and unknown.
- Preserved per-file ffprobe collection for accurate sidecar NFO stream details while classifying a torrent from its primary eligible video only.
- Changed Kodi item basenames to show the quality label before the short torrent hash; unknown quality has no quality label.
- Changed generated NFOs to omit all title/name fields; movie and series-root NFOs keep IDs/Combination URLs and streamdetails, and episode NFOs keep IDs/coordinates and exact per-file stream details without a scraper URL. Kodi's shared `tvshow.nfo` uses one deterministic representative release per quality root.
- Added manifest v5 and safer Kodi shared-folder reconciliation; only no-longer-referenced managed files are removed, shared directories are pruned only when empty, and Kodi remains read-only.
- Bumped NFO format to v3. Existing development trees are intended to be cleaned and rebuilt instead of migrated.

## 1.3.2

- Added independent `NFO_FORMAT_VERSION=2`, separate from software and manifest versions.
- Added automatic migration of legacy/unversioned NFOs from format v1 to v2 during normal synchronization, without re-probing media.
- Added `<torr2strm formatversion="2" />` to generated NFO XML.
- Added Kodi Combination NFO support with one canonical TMDb URL after the XML root for movie NFOs and `tvshow.nfo`; episode NFOs remain XML-only.
- NFO readers now correctly parse Kodi Combination NFOs instead of treating the trailing URL as malformed XML.
- Legacy migration preserves existing XML metadata and existing scraper URL hints when no trustworthy TMDb ID is available.

## 1.3.1

- Fixed UTF-8 filename length handling for generated `.strm` and `.nfo` files.
- Long output basenames are truncated on UTF-8 character boundaries with a short stable identity suffix; short existing names remain unchanged.
- Atomic writes now use a short temporary filename prefix, preventing `ENAMETOOLONG` caused by long final basenames.

## 1.3.0

- Added two independent output trees with separate root markers and manifests.
- Jellyfin output remains the authoritative/read-write view; Kodi output is read-only.
- If a managed Jellyfin STRM is deleted and reverse deletion is enabled, the TorrServer torrent is permanently removed with `action=rem`; Kodi is never allowed to remove a source torrent.
- If the source torrent disappears from TorrServer, its managed reflection is removed from every enabled output tree.
- A successful Jellyfin reverse deletion also suppresses same-run recreation in the Kodi output and removes its stale reflection.
- JacRed exact-match lookup is performed once per torrent and its result is shared by all outputs and by media-info resolution.
- Exact JacRed match may now supply `movie`/`tv` category for TorrServer items whose category is actually blank. JacRed `anime` remains `_uncategorized`.
- JacRed exact match can also supply the full magnet used by the Kodi/Elementum tree. If no full magnet is available, a BTIH+display-name magnet is constructed from the TorrServer source.
- Kodi/Elementum movies use one torrent-level `.strm` per torrent and no `oindex`.
- Kodi/Elementum TV uses one `.strm` per playable file and passes the original zero-based FileStats order as `oindex`.
- Original FileStats order is now preserved separately from the human/path sorting used by filesystem layout.
- The same NFO content is duplicated into all enabled output trees.
- A valid NFO found in any output tree can satisfy media-info cache reuse for all other output trees, avoiding another ffprobe call.
- Added `tvshow.nfo` for TV torrent roots.
- Expanded NFO provider IDs to use nested `providerIds` plus known Arr-style direct fields and safe title fallbacks.
- Added base metadata fields already known to the importer: `title`, `originaltitle`, `sorttitle`, `year`, `premiered`, `releasedate`, and episode `aired` where available.
- Added video `hdrtype`, bit depth and stereomode when present in ffprobe.
- Removed the old release-title quality fallback permanently; quality remains ffprobe-only.
- Per-torrent ffprobe failures remain recoverable and do not make the service exit non-zero.
- Manifest format bumped to v4; old v3 state is intentionally rejected. Start with clean output roots when upgrading from v1.2.x.
