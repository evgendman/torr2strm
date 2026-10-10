# torr2strm Development Roadmap

> **Status: v1.4.6 candidate; Windows/SMB-safe Kodi path fix pending CI and mini-PC validation.** External lookups remain removed; Jellyfin's output-tree behavior is unchanged.
> Baseline: prior implementation `v1.4.0`.
> Scope: STRM/NFO tree generation for Jellyfin and Kodi/Elementum. The separate `hotcached` project is out of scope.

## 1. Goal

Keep Jellyfin's existing output-tree construction and grouping behavior intact while implementing a separate normalized, human-readable Kodi tree. For identified series, Kodi groups releases by the stable series identity into one series directory per quality root. Preserve individual releases, truthful per-file NFO stream details, independent manifests, and safe synchronization.

## 2. Previous implementation baseline (`v1.4.0`)

### Jellyfin output

- Jellyfin is the authoritative output and the only output that can initiate reverse deletion of a TorrServer torrent.
- It creates file-level STRM entries for playable video files, using TorrServer `/play/{hash}/{file_id}` URLs.
- Its current directory-building/grouping behavior is the compatibility baseline and must not be redesigned as part of Kodi normalization.
- Reverse deletion is opt-in and uses `action=rem` only. `action=drop` is not used.
- Jellyfin and Kodi have independent output roots, root markers, and manifests.

### Kodi/Elementum output

- Kodi output is read-only: removing a Kodi STRM must never remove the source torrent.
- Movies currently use a torrent-level STRM and do not use `oindex`.
- TV output currently creates one STRM per playable file and passes the original zero-based FileStats order as Elementum `oindex`.
- Playback uses an Elementum plugin URI. The tree-layout work must not break this playback contract.

### Shared media information

- TorrServer is the source of truth for torrent inventory, info hash, and FileStats.
- Categories, quality roots, magnets, and IDs use only data already present in TorrServer; no external enrichment is performed.
- torr2strm does not execute local ffprobe and never calls TorrServer `/ffp/{hash}/{file_id}`. It reads ffprobe data already present in TorrServer metadata and may reuse cached NFO streamdetails for NFO output only.
- If technical stream details are not already available for an item, its NFO is identity-only. No new media probe is requested to populate it.
- Quality root has exactly two values, `4K` and `1080p`. Only ffprobe embedded in TorrServer metadata can select `4K`; missing, invalid, or non-4K probe data means `1080p`.
- Kodi has been observed to replace scraped display titles with titles from NFO late in its processing. Jellyfin reads ffprobe information present in NFO but does not use it as the effective stream metadata. Therefore the target NFO contract deliberately excludes human-readable name/title fields from both outputs.

## 3. Agreed target invariants

These decisions are settled and should not be reopened during implementation unless a reproducible technical defect makes a change unavoidable.

### 3.1 Output separation

1. **Jellyfin path/grouping behavior remains unchanged.** Its current folder-generation algorithm is not redesigned. It will continue to materialize its existing tree and use its current per-file playback URLs.
2. **Kodi gets its own normalized tree.** Kodi path planning must be independent of Jellyfin path planning, even though discovery and media-information collection may be shared.
3. Each output keeps an independent root, root marker, and manifest. Kodi stays read-only; only an explicitly enabled, tracked Jellyfin STRM deletion can cause TorrServer `action=rem`. Never use `action=drop`.

### 3.2 Quality root and quality label

4. There are exactly two quality roots: `4K` and `1080p`. The `1080p` root contains everything not classified into `4K`; it does not assert that every release within it is actually 1080p.
5. Quality classification makes no new ffprobe requests. Only usable ffprobe data already attached to TorrServer metadata can select `4K`. Missing, invalid, or non-4K probe data means `1080p`, regardless of title markers, structured quality fields, or cached NFOs.
6. The displayed `quality_label` is independent from the root and uses only TorrServer data: embedded ffprobe data, structured quality fields, explicit title markers, then unknown. Cached NFOs may preserve NFO streamdetails but do not provide category, root, or filename labels.
7. Do not manufacture a quality label. If no TorrServer-provided data supports a label, omit the quality suffix; the root defaults to `1080p` unless embedded ffprobe metadata already present in TorrServer proves 4K.
8. Normalize known quality markers to a consistent label. Examples include `480p`, `720p`, `1080p`, `1080i`, `1440p`, `2160p`, `2160p HDR`, and `2160p DV`. Add `HDR` or `DV` only when supported by already-available technical metadata or an explicit recognized title marker. Do not infer resolution from words such as `WEB-DL`, `BluRay`, `HEVC`, or `HD` alone.
9. The root and label may differ: if ffprobe is absent, the root remains `1080p` even if the TorrServer title includes a `2160p` marker retained in the basename.
10. Root selection examines usable ffprobe payloads already attached to TorrServer metadata only. A longer video dimension of at least 3840 selects `4K`; 1440p and missing/invalid probe data select `1080p`.

### 3.3 Kodi grouping and item names

11. **Known series identity:** episodes whose metadata identifies the same series are grouped under one human-readable series directory per quality root: `<Series Name> (<Year, if known>)/Season NN/`. Use stable, trusted series identifiers for grouping, not title similarity.
12. Derive the shared series-directory name from canonical metadata for that series. If metadata title is unavailable, use a cleaned series name extracted from the release title or file paths. Varying torrent titles for the same series ID must not create different series directories.
13. If two different series IDs genuinely collide on the same readable series name/year, append the namespaced series ID as a collision suffix. Do not routinely add a torrent hash to a known series directory.
14. **Unknown series identity:** use the torrent release title as the torrent-level directory name and preserve that torrent's original internal file hierarchy. Do not merge unknown series based on title similarity and do not invent IDs. Add a short torrent hash to the fallback directory name only if it is necessary to resolve an actual path collision.
15. For identified movies, group releases by trusted movie identity under a readable `<Movie Name> (<Year, if known>)/` directory in the relevant Kodi quality root. If the movie identity is unknown, use the torrent title and preserve the source torrent's internal hierarchy rather than guessing that separate torrents are the same movie.
16. Keep the existing Kodi playback model: one torrent-level STRM per movie release, no `oindex`; one STRM per playable TV file, with Elementum `oindex` equal to that file's original zero-based FileStats order. Never substitute the sorted path index or TorrServer `/play` file ID.
17. For a normalized Kodi episode, keep the readable name and `SxxEyy`, then add the quality label if known, and place the short torrent hash last before the extension. Example: `Show S01E01 — 720p [a1b2c3d4].strm`; the matching NFO uses the same basename. Movies follow the same suffix order: `<Movie> (<Year>) — 1080p [a1b2c3d4].strm`. If quality is unknown, omit the `— <quality>` portion. All Kodi path components, including torrent-internal folders for unidentified items, must be Windows/SMB-safe while preserving `.strm`/`.nfo` extensions; Jellyfin's path builder must remain unchanged.
18. Use the approved short-hash convention already documented for the tree. Start with 8 characters and increase the length only if short hashes collide at the same output path. Do not lengthen non-conflicting hashes.
19. If two files in one torrent resolve to the same logical episode and would otherwise have the same destination name, append a source-file ordinal from the original FileStats list (displayed starting at 1) only to the colliding items. This suffix is not added to normal episodes.
20. Do not fabricate episode or season numbers. If the series identity is known and an episode number can be parsed, use it. If an episode number is unknown, preserve a human-readable source filename rather than inventing `SxxEyy`. Use the known season when available; if no season can be inferred, use `Season 00` (existing `tv_unmatched_season = 0` behavior). If series identity is unknown, the original torrent-tree fallback in item 14 takes precedence.
21. Preserve deterministic filename sanitization and UTF-8-safe length handling. The basename is for human readability; the full torrent hash and source file identity in the manifest remain authoritative.

### 3.4 NFO contract

22. **No human-readable title/name fields are written to NFO.** In particular, do not write `title`, `originaltitle`, `sorttitle`, `showtitle`, or equivalent display-name fields. Kodi's observed post-scrape title replacement makes these fields unsuitable; Jellyfin should also find localized names via provider IDs.
23. A movie NFO is a Combination NFO containing trusted provider identifiers, technical stream data for its represented media item, and a scraper URL when a trustworthy supported ID is available. Do not add a title/name merely to make the XML look complete.
24. `tvshow.nfo` is a Combination NFO containing trusted series identifiers, the scraper URL, and technical `fileinfo/streamdetails` copied from the deterministic representative release selected for that quality root. This representative profile does not replace each episode sidecar NFO, which must retain that episode file's own ffprobe data.
25. An episode NFO uses the ordinary episode NFO format, contains trusted identifiers and the season/episode coordinates when known, and retains the ffprobe stream details for that exact source file. It does not contain a Combination NFO scraper URL.
26. Keep only values supported by source metadata or ffprobe. No IDs, years, episode numbers, names, or stream characteristics may be invented. The existing per-file ffprobe/NFO behavior stays in place even though the torrent's root and quality label are decided by its primary video file.
27. Keep a single compatible NFO contract for both output trees unless implementation tests demonstrate a real format incompatibility. Jellyfin may ignore NFO stream details, but that is not a reason to discard technically correct data that Kodi can use.

### 3.5 Identity, manifests, and synchronization

28. Distinguish logical media identity (movie ID, or series ID plus season/episode), release identity (full torrent hash), and physical identity (output path plus source-file identity).
29. The manifest, never a parsed filename or short hash, is the source of truth mapping managed STRM/NFO paths to full torrent hashes and source files.
30. Reconciliation must tolerate shared Kodi series/season directories. Removing or updating one torrent may affect only that torrent's entries; it must never delete other releases' entries or a shared folder that still contains managed files.
31. Remove managed directories only when they are empty and no longer needed. If multiple source files map to the same episode, use the exceptional source-file ordinal rule in item 19 to avoid overwriting.
32. Torrents disappearing from TorrServer are reflected in all enabled output trees at the next sync. Removing a Kodi STRM never removes a source torrent.
33. Unknown/insufficiently identified media uses the source-tree fallback. Only explicit supported TorrServer category values are accepted; blank, anime, or unsupported categories remain `_uncategorized`. There is no external category lookup or title-based guess.

## 4. Development phases

Phases are ordered by dependency. Phase 0's decision-making is complete; implementation work begins with Phase 1.

### Phase 0 — Freeze the behavior contract — DECISIONS COMPLETE

**Completed planning decisions**

- [x] Confirm that Jellyfin's existing output-tree/grouping behavior is retained.
- [x] Fix the two quality roots, primary-video quality policy, fallback order, unknown-quality behavior, and quality-label normalization.
- [x] Fix normalized Kodi grouping for identified media and source-tree fallback for unknown identities.
- [x] Fix basename suffix order, short-hash collision handling, and the rare duplicate-episode suffix.
- [x] Fix the NFO contract, including omission of all display-title fields and the Combination NFO rules.
- [x] Choose a clean rebuild of development output roots instead of migrating old directory trees.
- [x] Fix manifest ownership and source-deletion safety requirements.

**Implementation note**

These checkboxes mean the decisions are settled, not that code or regression fixtures have already been implemented. Existing Jellyfin/Kodi examples and edge cases still need to become test fixtures in Phase 6.

### Phase 1 — Implement torrent-level quality resolution

**Work**

- Keep exactly `4K` and `1080p` as output quality roots.
- Do not probe media. Inspect only ffprobe JSON already present in TorrServer metadata; cached NFOs may preserve streamdetails but do not classify the quality root.
- Gather structured quality fields and explicit resolution/HDR/DV markers from existing TorrServer metadata/title for display labels only.
- Set the root to `4K` only if embedded TorrServer ffprobe proves 4K; otherwise set it to `1080p`, independently from display-label selection.
- Choose the first available display label by source priority and omit the suffix when no label exists.
- Keep streamdetails only from a payload already attached to TorrServer or an exact-file cached NFO; otherwise generate identity-only NFO.
- Normalize known resolution and HDR/Dolby Vision labels as specified above. Unknown quality goes to `1080p` with no quality suffix.

**Exit criteria**

- A `2160p` title marker may remain in a basename under the `1080p` root when TorrServer has no usable ffprobe payload; root and label remain independent.
- A torrent uses the `4K` root only when its existing TorrServer ffprobe data confirms 4K dimensions; HDR/DV labels are included only when supported by data.
- Missing ffprobe data defaults the root to `1080p`, regardless of title or structured labels; no quality suffix is fabricated when no label is present.
- Per-file NFO stream details remain tied to their source file when already present in a matching NFO/payload; no ffprobe request is performed for missing details.

### Phase 2 — Implement the agreed NFO contract

**Work**

- Remove human-readable display-title/name elements from generated NFOs, including movie, series-root, and episode NFOs.
- Movie NFO: trusted provider IDs + streamdetails only if already available for that source item + a Combination NFO scraper URL when a trustworthy supported ID is available.
- `tvshow.nfo`: trusted series IDs + Combination NFO scraper URL + streamdetails from the deterministic representative release in that quality root; no human-readable title.
- Episode NFO: ordinary episode XML; trusted IDs, known season/episode coordinates, and that source file's `fileinfo/streamdetails` only if already available; no Combination NFO URL.
- Preserve Kodi Combination NFO URL placement/format supported by the current NFO implementation, updating the format version if the new contract requires it.
- Do not issue per-file ffprobe requests. Use technical data already present for a matching source file; NFOs without such data remain identity-only.
- Add fixtures/assertions proving that title-like fields are omitted and provider IDs/scraper URLs/stream details are kept in the correct NFO types.

**Exit criteria**

- NFO contains no title/name fields that could override localized scraper results. Series-root Combination NFOs have representative streamdetails, while episode sidecars retain exact per-file streamdetails.
- The Combination URL is present only in movie and series-root NFOs and only when a reliable supported ID exists.
- Episode NFO has no Combination URL and preserves correct season/episode coordinates and per-file technical information.
- Kodi and Jellyfin output reuse a valid common NFO representation; there is no speculative output-specific split.

### Phase 3 — Implement logical identity and Kodi path planning

**Work**

- Resolve movie/series IDs only from trustworthy metadata already attached to TorrServer; do not enrich IDs from external providers.
- For a known series ID, map torrents and source files for that series into one Kodi series directory per quality root; derive its human-readable name/year from canonical series metadata.
- For different IDs that collide on the same series name/year, append the namespaced series ID only to resolve that folder collision.
- For an unknown series ID, use the torrent release title for the torrent-level directory and preserve the original internal file hierarchy. Never group unknown series by textual similarity.
- For known series with a missing episode number, do not manufacture one. Use the inferred season when possible, otherwise `Season 00`, and preserve the readable source filename.
- For known movie IDs, group releases by logical movie identity under one readable movie directory per Kodi quality root. For unknown movie IDs, preserve each torrent's source hierarchy instead of guessing from title similarity.
- Keep source `FileStats.order` separately from sorted/path order; it is both needed for Elementum `oindex` and for the rare collision suffix (displayed ordinal is original order + 1).
- Plan item basenames so the optional quality label appears before `[short-hash]`; omit the label if unknown. Increase the short hash only when a path collision actually occurs.

**Exit criteria**

- Multiple releases with the same trusted series/movie ID converge on one logical Kodi folder per quality root.
- Differently named torrents for the same identified series do not create duplicate series folders.
- Unknown-identity torrents preserve their own inner hierarchy and are not accidentally merged.
- Duplicate file mappings for the same episode are handled only by the rare file-ordinal suffix; normal names remain clean.

### Phase 4 — Build the Kodi tree independently

**Work**

- Implement the normalized Kodi TV tree: `tv/<quality-root>/<series> (<year, if known>)/Season NN/` for identified series.
- Implement the normalized Kodi movie tree: `movie/<quality-root>/<movie> (<year, if known>)/` for identified movies.
- For unknown identities, keep the torrent-title directory and original torrent internal file structure.
- Keep readable names; do not routinely put IDs or hashes in series/movie directory names. Use a series ID only to resolve a real series-folder collision; use an eight-character torrent hash in item basenames.
- For identified TV episodes, produce names like `Series S01E01 — 720p [a1b2c3d4].strm` and the matching `.nfo`. If quality is unknown, omit `— <quality>`. Hash stays after the quality text and before the extension.
- For identified movie releases, retain the torrent-level Elementum link and use names like `Movie (Year) — 1080p [a1b2c3d4].strm` with matching NFO.
- Keep `oindex` equal to original zero-based FileStats order for TV; movies remain torrent-level and omit `oindex`.
- Preserve safe component sanitization, UTF-8 truncation, collision prevention, atomic writes, and playback URI encoding.
- Do not change Jellyfin's path builder or directory-grouping algorithm. Add regression tests before changing any shared helper that could affect Jellyfin paths.

**Exit criteria**

- Repeated syncs create deterministic paths without duplicate directories or overwritten releases.
- Series grouping, release labels, hash suffix ordering, and fallback structures match the approved examples.
- Elementum playback URI, movie torrent-level behavior, and TV `oindex` remain correct.
- Jellyfin output layout and path-building behavior match its pre-change fixtures.

### Phase 5 — Manifest, reconciliation, and clean rebuild

**Work**

- Keep independent Jellyfin/Kodi manifests and root markers. The manifest maps each STRM/NFO to full torrent hash, source path/file identity, and output-relative paths.
- Make the shared Kodi series/season folder safe under additions, updates, and removals from multiple torrents. A torrent sync must only reconcile that torrent's manifest entries.
- Never use the visible filename, display quality, short hash, or shared directory name as the authoritative source-torrent identity.
- Because this is still development and existing trees do not need to be retained, do not implement a directory-tree migration. Use a documented clean rebuild of the dedicated Jellyfin and Kodi output roots and their manifests/root markers.
- The reset/rebuild procedure must stop the scheduled service/timer and ensure reverse deletion is disabled while old output state is being cleared. Remove only the explicitly configured managed output roots/state, never an arbitrary parent directory. Then recreate markers/manifests and run a fresh sync.
- After the clean rebuild, verify that missing old paths cannot be interpreted as user-deleted Jellyfin STRMs and cannot remove TorrServer torrents.
- Keep reverse deletion restricted to the explicitly enabled Jellyfin flow using `action=rem`; Kodi is read-only and `action=drop` is forbidden.
- Bump the manifest format version if the stored schema/identity mapping changes. No backward path-migration routine is required for the development-tree reset, but malformed/mismatched manifests must still fail safely.

**Exit criteria**

- A clean rebuild is repeatable and does not trigger unintended source-torrent removals.
- Removing or updating one release removes only its managed entries; other torrents sharing the same series/season folders remain intact.
- Empty managed directories are cleaned only after the last dependent entry is gone.
- Kodi STRM deletion has no source-side effect; only an explicitly enabled tracked Jellyfin deletion can invoke `action=rem`.
- No code path invokes `action=drop`.

### Phase 6 — Automated tests and integration fixtures

**Work**

- Add regression fixtures for current Jellyfin paths and Kodi playback behavior before changing path generation.
- Test quality-root versus quality-label with 480p/720p/1080p/1080i/1440p/2160p and HDR/DV examples.
- Test no `/ffp/` or external-source requests, strict ffprobe-only root classification, display labels from TorrServer data, and missing ffprobe (`1080p` root).
- Verify that quality is determined from the primary torrent video, while other playable files retain their own per-file stream details in their NFOs and do not change the torrent label.
- Test two or more releases for one episode, multiple episodes in one release, a rare duplicate logical episode within one torrent, and short-hash collision extension.
- Test grouping of one series ID across releases with different torrent titles/internal file trees and different series IDs with the same human-readable name/year.
- Test unknown series/movie IDs: torrent-title directory and original internal file tree remain intact; no ID or entity is invented and no fuzzy merge occurs.
- Test missing season/episode coordinates without fabricated numbers, including `Season 00` fallback.
- Test movie-level Elementum STRM without `oindex`, TV file-level STRM with original zero-based `oindex`, and correct encoded playback URLs.
- Test NFO field allowlists: no title-like fields; movie and series-root Combination NFO URLs only when IDs exist; ordinary episode NFO has no scraper URL; representative series-root and exact per-file stream details are preserved.
- Test long Unicode names, sanitization, deterministic output, independent manifests, root markers, idempotent reconciliation, dry-run, and safe clean reset.
- Test that output path changes or reset cannot be mistaken for user-triggered Jellyfin source removal and that no Kodi code can call TorrServer removal.

**Exit criteria**

- All unit and integration tests pass.
- No external metadata lookup can override TorrServer identity, category or quality.
- No test permits Kodi-side source deletion or `action=drop`.
- Jellyfin path/grouping regression tests pass.

### Phase 7 — Real-player validation, documentation, and release

**Work**

- On the mini-PC, stop the timer/service and use the documented clean reset on the dedicated output roots. Start with reverse deletion disabled.
- Generate the new Kodi tree and rescan a test Kodi library. Verify one directory per identified series per quality root, visible quality/hash choices, localized scraper names, and correct Elementum playback.
- Verify that NFO title/name fields are absent and Kodi does not have its localized names overwritten after scraping.
- Validate Jellyfin playback/scanning and localized naming from IDs; technical ffprobe data may be present in NFO even though Jellyfin does not treat it as the effective stream data.
- Verify multi-release cleanup, torrent disappearance reflection, and the explicitly enabled Jellyfin reverse-delete path separately. Never test by enabling reverse deletion during a clean reset.
- Update README, architecture, configuration/NFO references and CHANGELOG to reflect shipped behavior.
- Update software, manifest, and NFO format versions only alongside the corresponding implementation and tests. No version number changes are part of this planning update.

**Exit criteria**

- Kodi and Jellyfin real-player checks pass on the mini-PC.
- A clean rebuild and follow-up sync work without duplicate entries or unintended torrent removal.
- Release notes clearly distinguish the new Kodi tree from the unchanged Jellyfin folder/grouping logic.

## 5. Explicit non-goals

- Do not redesign Jellyfin's existing path/grouping behavior as part of Kodi normalization.
- Do not add more quality-root categories: only `4K` and `1080p` exist.
- Do not infer a release's resolution from codec or source tokens alone.
- Do not issue any ffprobe request. Use technical data already embedded in TorrServer or exact-file cached NFOs for NFO streamdetails; missing ffprobe means the `1080p` root.
- Do not merge releases using approximate title similarity or invent missing provider IDs/season/episode numbers.
- Do not query/install local ffprobe, do not call TorrServer `/ffp/`, and do not contact JacRed, Prowlarr or any other external metadata source.
- Do not make Kodi output authoritative for deletion and do not implement/use `action=drop`.
- Do not include `hotcached` work in this roadmap.

## 6. Definition of done

The work is complete when the independent Kodi tree groups identified releases by stable IDs; unknown items preserve their source layout; the root is `4K` only when TorrServer-embedded ffprobe proves 4K and otherwise `1080p`; the display label follows source priority without a probe; NFO streamdetails are retained only when already available; multiple releases remain distinct and human-readable; manifests reconcile shared folders safely; the clean rebuild is verified; Kodi/Elementum playback works; and Jellyfin's existing path/grouping behavior and deletion-safety invariants remain intact.

## 7. Current status

- [x] Phase 0 — Behavior decisions finalized
- [x] Phase 1 — Torrent-level quality resolution implemented
- [x] Phase 2 — Title-free NFO contract implemented
- [x] Phase 3 — Logical identity and Kodi path planning implemented
- [x] Phase 4 — Independent Kodi tree builder implemented
- [x] Phase 5 — Manifest reconciliation and clean-rebuild path implemented
- [ ] Phase 6 — Regression tests for the TorrServer-only rules are pending CI
- [ ] Phase 7 — Real-player validation on the mini-PC and release tagging

This status records a candidate implementation, not a production-approved release. Do not tag or treat v1.4.6 as production-approved until CI passes and Kodi Windows/SMB path changes are validated on the mini-PC.
