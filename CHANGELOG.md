# Changelog

## 1.4.16

- Avoid redundant TorrServer detail requests by parsing top-level FileStats already returned by `action=list`.
- Add a persistent, infohash-keyed source cache for complete FileStats inventories so previously processed DB-only torrents do not need repeated `action=get`, `/playlist` metadata loads, or polling.
- Refresh mutable title, category and custom metadata from the current `action=list` response on every run; cached data is limited to validated file IDs, paths, lengths and original FileStats order.
- Keep source-cache failures recoverable and prune entries only after a successful inventory confirms that a torrent disappeared.
- Add source-request/cache counters to the `DONE` summary and regressions for direct list FileStats, cache reuse, mutable metadata refresh, invalid-cache recovery, and disappearance pruning.
- Add a guarded whole-inventory fast path: skip quality/NFO resolution and output regeneration only when the complete input/config signature matches and every managed STRM/NFO remains intact; missing or modified files trigger normal self-repair. Verify shared tvshow.nfo content hashes and reject symlinked managed paths before fast-path entry.
- Add a guarded whole-inventory fast path: skip quality/NFO resolution and output regeneration only when the complete input/config signature matches and every managed STRM/NFO remains intact; missing or modified files trigger normal self-repair.
- Keep manifest schema v5, NFO format v3, media-tree contracts, output reconciliation, and reverse-deletion safety unchanged.

### v1.4.16 candidate validation — 2026-10-30

- Final GitHub Actions CI passed on commit `4ed684ea1ae2a67fae686ff81c857133bf6cb7ea`: Python compilation and all 66 regression tests passed.
- Reference MINI-Ubuntu dry-run processed all 635 TorrServer inventory entries with `quality_ready=635` and `failed=0`. The initial cache-empty pass reported `list=1,get=1236,playlist=618,polls=618,cache_hits=0,cache_misses=618`; the 7 embedded and 10 top-level FileStats entries came directly from `action=list`.
- An isolated cache warm-up against the live TorrServer populated 635 cache entries. The second run completed in 2.62 seconds with `get=0,playlist=0,polls=0,cache_hits=594,cache_misses=0,list_embedded=7,list_file_stats=34`, `unchanged=1270`, and `failed=0`.
- A self-repair check deliberately removed a managed STRM and modified a shared Kodi `tvshow.nfo` in the isolated output tree. The fast path declined, full reconciliation restored both files, and the following run re-entered the fast path.
- A production-like compatibility check used temporary copies of the current Jellyfin/Kodi output trees and manifests. The v1.4.16 reconciliation added no paths, removed no paths, and changed no STRM/NFO content, mtimes, or permissions; only the two manifests were updated with the new integrity hashes. A follow-up run completed in 2.46 seconds with `FAST_PATH=true`, `get=0,playlist=0,polls=0,cache_hits=618`, `unchanged=1270`, and `failed=0`.
- The production `/etc/torr2strm/config.toml` and both production manifests retained their pre-test SHA-256 values. The reference host remains on v1.4.15 pending installation; its timer is enabled but currently stopped. No production output tree or TorrServer inventory was changed during candidate acceptance.
- Manifest schema v5 and NFO format v3 remain unchanged. No clean rebuild or output-tree migration is required.
## 1.4.15

- Fix season/episode parsing by evaluating TorrServer file paths and torrent titles as independent sources. Complete coordinates in a file path take precedence; an explicit path season is preserved before any title-level fallback is considered.
- Recognize valid episode coordinates when the episode digits are followed by underscores, dots, hyphens, whitespace, or brackets, including patterns such as `S03E01_Dub` and `S02E01_RHS`.
- Reject partial digit matches and malformed/ambiguous tokens so `S03E01` cannot degrade into season zero. Do not copy an episode number from the torrent title into a file that already has its own explicit season but no episode coordinate.
- Add parser regressions and an end-to-end check of Black Mirror S03/S04 and Squid Game S02/S03 through Jellyfin season directories/NFO coordinates and Kodi episode filenames.
- Keep Season 00 fallback behavior, output layout contracts, manifest schema v5, NFO format v3, and the TorrServer-only/no-new-HTTP-call policy unchanged.

## Operational note — v1.4.15 reference-host acceptance (2026-10-29)

- The official release archive checksum and deployed source SHA-256 were verified; the installed version reports `1.4.15`.
- A controlled live sync on reference MINI-Ubuntu exited successfully: `torrents=635 quality_ready=635 quality_unresolved=0 created=0 updated=15 unchanged=1255 failed=0`, elapsed 311.28 seconds.
- Post-sync validation passed for both output manifests (635 records each), all 33 corrected-season STRMs in each of the Jellyfin and Kodi output trees, and absence of target `Season 00` paths. Kodi numbering checks passed for X-Files S03 (24 episodes, 01–24) and The Umbrella Academy S03 (10 episodes, 01–10).
- The timer was restored and verified active/enabled. The existing configuration was preserved and a pre-deployment backup was created. Manifest format v5 and NFO format v3 remain unchanged.
- This records successful initial live deployment acceptance; a separate second no-op/idempotency run has not been claimed.

## Operational note — systemd timer schedule (2026-10-27)

- The repository template and the published `v1.4.14` release both use the monotonic schedule: `OnBootSec=2min`, `OnUnitActiveSec=5min`, `RandomizedDelaySec=15s`, and `AccuracySec=1s`. The same settings were confirmed in `/etc/systemd/system/torr2strm.timer` on reference MINI-Ubuntu.
- On 2026-10-27, the reference host's timer schedule was restored and verified. `systemd-analyze verify` passed. A post-completion inspection recorded `torr2strm.service` exiting with status 0 at 22:42:57 +05 (`torrents=431 quality_ready=431 quality_unresolved=0 created=0 updated=0 unchanged=862 failed=0`), with the timer active/enabled and the next trigger scheduled for 22:45:20 +05.
- The earlier `NEXT=-` snapshot was taken while the `Type=oneshot` service was still activating/running. No post-completion snapshot of the prior setup established a re-arming failure, so this was a precautionary restoration of the intended schedule—not a confirmed defect fix. `NEXT=-` during an active oneshot run is not, by itself, evidence of timer failure.
- This is an operational history note, not an unreleased application change: the published `v1.4.14` unit template already contains the schedule. No Python code, manifest schema v5, or NFO format v3 change was made.

## 1.4.14

- Preserve the category from TorrServer action=list when a refreshed action=get response omits or blanks the category after metadata loading.
- Prefer an explicit category in the refreshed response. Explicit unsupported categories remain _uncategorized; no category is inferred from titles, paths, or external metadata.
- When both current responses omit the category but a prior manifest has a trusted movie/tv category, defer that torrent for the current run and retain its prior manifest records and managed outputs.
- Add concise category-source, list-fallback, unresolved-category, and deferred-category diagnostics.
- Add regressions for movie/TV category fallback, refreshed-response precedence, explicit unsupported categories, and preservation/recovery across a temporary category omission.
- Manifest schema v5 and NFO format v3 are unchanged; no migration or clean rebuild is required.

## 1.4.13

- Replace raw log-line forwarding to Apprise with plain-language, user-facing summaries grouped by problem type and media/torrent.
- Include the TorrServer inventory title as early notification context so failed action=get requests can still identify the affected release; use a hash only when no reliable title is available.
- Distinguish common source failures (not found/HTTP 404, connection errors, timeouts, invalid responses, and unavailable metadata) and explain retry and output-preservation behavior.
- Keep technical details and exception causes in the local journal; exclude successful cleanup and intermediate fallback diagnostics from user-facing alerts.
- Group related file-level events under the affected media, retain STRM-collision summaries, and bound Telegram-sized bodies with an omitted-event count.
- Preserve the shared Apprise request contract, warning/critical tags, and unchanged-message suppression.
- No manifest/NFO schema or media-tree changes.


## 1.4.12

- Scope Phase 15's canonical title/year selection strictly to identified Kodi TV series. Identified movie folder naming retains the previous per-quality-root selection behavior.
- Add a regression test with the same movie identity in 1080p and 4K roots but different source titles/years, proving each root retains its prior folder name.
- Fix follows the v1.4.11 mini-PC dry-run, which reported planned removal of existing Kodi movie paths; do not accept v1.4.11 for live synchronization.
- No manifest schema, NFO format, or configuration changes.

## 1.4.11

- Decouple generated NFO output from the application release version; upgrades that change only the app version must not rewrite existing sidecars. Actual NFO content and `NFO_FORMAT_VERSION` changes still trigger updates.
- Stabilize canonical Kodi series name/year from all parsed playable inventory snapshots for a trusted identity, independent of quality-root and quality-resolution readiness; choose the earliest valid year deterministically.
- Preserve previous output files and manifest records when quality resolution fails temporarily for a torrent that is still in the source inventory. A truly absent torrent is cleaned up normally; a source entry with no playable video is handled as a permanent skip and old managed output is cleaned up.
- Apply `chmod` only when the current permission bits differ from the target, including output roots and manifest directories. Preserve maximally permissive target modes: `0666` for files and `0777` for directories.
- Add regressions for NFO version semantics, canonical series years across quality roots, temporary quality failure/recovery, true source disappearance, permanent no-video cleanup, and permission-mode corrections.
- No manifest schema bump, NFO format bump, clean rebuild, external metadata integration, or torr2jellyfin/Jellyfin-plugin orchestration integration.

## 1.4.10

## 1.4.10

- Make Apprise collision warnings actionable: each collision-bearing torrent now produces one summary with the release title, full hash, affected-file counts and representative source paths.
- Keep detailed per-file STRM_COLLISION_RESOLVED / STRM_COLLISION_HASH_EXTENDED warnings in the local journal without repeating every file as a separate Apprise event.
- Include the release title in STRM_COLLISION_UNRESOLVED critical logs and notification details.
- Add regression coverage for summary content, example paths and suppression of duplicate per-file collision lines in Apprise.
- No manifest/NFO schema changes or clean rebuild required.

## 1.4.9
- Resolve Jellyfin STRM destination collisions before writing files. Every participant receives a deterministic suffix containing the short torrent hash and TorrServer file ID; if a residual collision remains, retry with the full hash and file ID.
- Leave ordinary Jellyfin filenames and the existing Season NN directory-generation behavior unchanged when no collision occurs. Do not infer episode numbers or interpret DVD/Blu-ray directory semantics.
- Keep every generated STRM basename within the 255-byte filesystem-component limit including the .strm extension; keep NFO sidecars paired to the resolved STRM path.
- Never silently skip a colliding video file. Add regressions for repeated VOB basenames across a multi-DVD Lexx-like release, stable repeated synchronization, manifest/file-ID pairing, and long UTF-8 names.
- No manifest or NFO schema change is required. Mini-PC deployment acceptance remains pending.

## 1.4.8 (CI passed; mini-PC acceptance pending)

- Complete quality-root evidence precedence: embedded video dimensions, recognized structured TorrServer resolution fields, explicit 4K/2160p markers in the torrent title, then file paths, then the 1080p fallback.
- Isolate per-torrent metadata/FileStats failures. Preserve prior Jellyfin/Kodi outputs and manifest records for deferred hashes while continuing to synchronize healthy torrents; retry on the next run.
- Add optional aggregated Apprise warnings/errors using the shared arr-stack endpoint, with warnings and critical tags, bounded requests and persisted fingerprint suppression. Delivery failures remain log-only and do not affect sync exit status.
- Keep standalone CLI and systemd timer behavior; torr2jellyfin integration Phase 10 remains deferred.
- Add regressions for structured/path quality evidence, deferred-source safety and Apprise routing/deduplication.

## 1.4.7 (published and accepted on the mini-PC)

- Use usable video dimensions already embedded in TorrServer metadata as the first choice for the quality root. When such dimensions are unavailable, an explicit 4K or 2160p marker in the release title may select the 4K root; usable non-4K dimensions always override title markers.
- In Kodi TV output, stop creating per-episode NFO sidecars. Create tvshow.nfo only at the root of an identified series and keep it until no managed release references it.
- Bound every Kodi directory and file component to 255 UTF-8 bytes using stable suffixes for truncated names; keep Windows/SMB sanitization and resolve collisions after truncation.
- GitHub Actions passed 36 tests. Mini-PC acceptance completed on 2026-10-14 using commit 387696cde4eb9682a231eb4d893b6c5344981a95 after clean rebuild of both output trees; the first timer-triggered run reported 98 unchanged entries. The only logged skip was a Blu-ray ISO with no playable video files.
- Official release v1.4.7 was published on 2026-10-13. The later clean-rebuild/path acceptance confirmed it works for ordinary scheduled synchronization.

## 1.4.6

- Sanitize Kodi-only directory, item, and torrent-internal path components for Windows/SMB clients, avoiding Samba short-name aliases for names containing characters such as `|`.
- Preserve real `.strm` and `.nfo` extensions and readable names; protect Windows-reserved device names and trailing dots/spaces.
- Keep Jellyfin's historical path naming unchanged; add end-to-end regressions for Windows-safe Kodi paths and managed sidecar extensions.

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