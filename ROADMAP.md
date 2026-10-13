# torr2strm Development Roadmap

> **v1.4.7 candidate: implementation and CI complete; mini-PC acceptance pending.** GitHub Actions passed all 33 tests. Phase 8 changes: (1) use a clear `4K`/`2160p` release-title marker as a fallback for the `4K` root only when usable video dimensions are unavailable; (2) in Kodi TV output, generate `tvshow.nfo` only at the root of an identified series and omit season-folder and episode-sidecar NFOs. Jellyfin's existing output behavior remains unchanged. Mini-PC acceptance also exposed a Kodi path-component length defect; the component-bounding fix and regressions in Phase 8b are required before publishing v1.4.7. The formal tag remains withheld until Phase 8b and the clean-rebuild/player acceptance pass. The broader quality-root evidence precedence requested during mini-PC validation is tracked separately in Phase 9 and is not part of v1.4.7.
> Baseline: prior implementation `v1.4.0`.
> Scope: STRM/NFO tree generation for Jellyfin and Kodi/Elementum. The separate `hotcached` project is out of scope.

## 1. Goal

Keep Jellyfin's existing output-tree construction, grouping, and NFO behavior intact while maintaining a separate normalized, human-readable Kodi tree. For identified series, Kodi groups releases by stable series identity into one series directory per quality root. Preserve individual releases, Kodi scraper discovery for episodes, independent manifests, and safe synchronization.

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
- Quality root has exactly two values, `4K` and `1080p`. The planned evidence order is: (1) usable video dimensions embedded in TorrServer ffprobe metadata; (2) a recognized explicit quality/resolution value in structured TorrServer metadata; (3) an unambiguous `4K`/`2160p` marker in the torrent title or release/file paths; (4) default to `1080p` only when none of those sources establishes 4K. A Sonarr-1080 origin/transfer path is not proof of actual quality and must not override stronger evidence.
- Kodi has been observed to replace scraped display titles with titles from NFO late in its processing. Jellyfin reads ffprobe information present in NFO but does not use it as the effective stream metadata. Therefore the target NFO contract deliberately excludes human-readable name/title fields from both outputs.

## 3. Agreed target invariants

These decisions are settled and should not be reopened during implementation unless a reproducible technical defect makes a change unavoidable.

### 3.1 Output separation

1. **Jellyfin path/grouping behavior remains unchanged.** Its current folder-generation algorithm is not redesigned. It will continue to materialize its existing tree and use its current per-file playback URLs.
2. **Kodi gets its own normalized tree.** Kodi path planning must be independent of Jellyfin path planning, even though discovery and media-information collection may be shared.
3. Each output keeps an independent root, root marker, and manifest. Kodi stays read-only; only an explicitly enabled, tracked Jellyfin STRM deletion can cause TorrServer `action=rem`. Never use `action=drop`.

### 3.2 Quality root and quality label

4. There are exactly two quality roots: `4K` and `1080p`. The `1080p` root contains everything not classified into `4K`; it does not assert that every release within it is actually 1080p.
5. Quality-root classification makes no new ffprobe requests and follows a strict evidence order: (a) usable video dimensions already attached to TorrServer ffprobe metadata; (b) a recognized explicit quality/resolution value in structured TorrServer metadata; (c) an unambiguous `4K`/`2160p` marker in the TorrServer torrent title or release/file paths; (d) `1080p` as the default when no higher-priority evidence establishes 4K. Usable technical dimensions always win: a maximum dimension of at least 3840 selects `4K`, while usable lower dimensions select `1080p`. Structured quality metadata takes precedence over title/path markers. A marker in a title or path must not override usable technical dimensions or a recognized structured quality value. The way a torrent entered TorrServer—including the Sonarr-1080 workflow or a transfer service—is provenance, not quality evidence, and must not by itself force the `1080p` root.
6. The displayed `quality_label` is independent from the root and uses only TorrServer data: embedded ffprobe data, structured quality fields, explicit title markers, then unknown. Cached NFOs may preserve NFO streamdetails but do not provide category, root, or filename labels.
7. Do not manufacture a display quality label. If no TorrServer-provided data supports a label, omit the quality suffix. The root classifier may also use an explicit structured quality value or title/release-path marker under item 5; these root-only fallbacks must not invent unrelated HDR/DV details. If no evidence supports `4K`, the root defaults to `1080p`.
8. Normalize known quality markers to a consistent display label. Examples include `480p`, `720p`, `1080p`, `1080i`, `1440p`, `2160p`, `2160p HDR`, and `2160p DV`. Recognize standalone/unambiguous `4K` and `2160p` markers in the torrent title or release/file paths as root-selection evidence under item 5. Add `HDR` or `DV` to a display label only when supported by already-available technical metadata or an explicit recognized marker. Do not infer resolution from source/codec tokens such as `WEB-DL`, `BluRay`, `HEVC`, or `HD` alone.
9. The root and display label may differ: a title can retain a `1440p` label while using the `1080p` root. A recognized structured quality value or a title/release-path `4K`/`2160p` marker may select the `4K` root only after higher-priority evidence is unavailable; these sources must not override usable non-4K dimensions or conflicting recognized structured quality metadata.
10. Root selection follows item 5's priority exactly: usable ffprobe dimensions; recognized explicit structured quality/resolution metadata; recognized `4K`/`2160p` tokens in the torrent title or release/file paths; then default `1080p`. A `1440p` marker alone never selects `4K`. Metadata indicating the torrent's Sonarr/transfer origin is not an explicit quality value and cannot force a lower root.

### 3.3 Kodi grouping and item names

11. **Known series identity:** episodes whose metadata identifies the same series are grouped under one human-readable series directory per quality root: `<Series Name> (<Year, if known>)/Season NN/`. Use stable, trusted series identifiers for grouping, not title similarity.
12. Derive the shared series-directory name from canonical metadata for that series. If metadata title is unavailable, use a cleaned series name extracted from the release title or file paths. Varying torrent titles for the same series ID must not create different series directories.
13. If two different series IDs genuinely collide on the same readable series name/year, append the namespaced series ID as a collision suffix. Do not routinely add a torrent hash to a known series directory.
14. **Unknown series identity:** use the torrent release title as the torrent-level directory name and preserve that torrent's original internal file hierarchy. Do not merge unknown series based on title similarity and do not invent IDs. Add a short torrent hash to the fallback directory name only if it is necessary to resolve an actual path collision.
15. For identified movies, group releases by trusted movie identity under a readable `<Movie Name> (<Year, if known>)/` directory in the relevant Kodi quality root. If the movie identity is unknown, use the torrent title and preserve the source torrent's internal hierarchy rather than guessing that separate torrents are the same movie.
16. Keep the existing Kodi playback model: one torrent-level STRM per movie release, no `oindex`; one STRM per playable TV file, with Elementum `oindex` equal to that file's original zero-based FileStats order. Never substitute the sorted path index or TorrServer `/play` file ID.
17. For a normalized Kodi episode, keep the readable name and `SxxEyy`, then add the quality label if known, and place the short torrent hash last before the extension. Example: `Show S01E01 — 720p [a1b2c3d4].strm`; Kodi TV episodes do not get matching item-level NFO sidecars. Movies follow the same suffix order: `<Movie> (<Year>) — 1080p [a1b2c3d4].strm` with a matching movie NFO. If quality is unknown, omit the `— <quality>` portion. All Kodi path components, including directory names and torrent-internal folders for unidentified items, must be Windows/SMB-safe and length-bounded while preserving `.strm`/`.nfo` extensions; Jellyfin's path builder must remain unchanged.
18. Use the approved short-hash convention already documented for the tree. Start with 8 characters and increase the length only if short hashes collide at the same output path. Do not lengthen non-conflicting hashes.
19. If two files in one torrent resolve to the same logical episode and would otherwise have the same destination name, append a source-file ordinal from the original FileStats list (displayed starting at 1) only to the colliding items. This suffix is not added to normal episodes.
20. Do not fabricate episode or season numbers. If the series identity is known and an episode number can be parsed, use it. If an episode number is unknown, preserve a human-readable source filename rather than inventing `SxxEyy`. Use the known season when available; if no season can be inferred, use `Season 00` (existing `tv_unmatched_season = 0` behavior). If series identity is unknown, the original torrent-tree fallback in item 14 takes precedence.
21. Preserve deterministic filename sanitization and UTF-8-safe length handling for both file basenames and directory components. The basename is for human readability; the full torrent hash and source file identity in the manifest remain authoritative.

### 3.4 NFO contract

22. **No human-readable title/name fields are written to NFOs that are generated.** In particular, do not write `title`, `originaltitle`, `sorttitle`, `showtitle`, or equivalent display-name fields. Kodi's observed post-scrape title replacement makes these fields unsuitable; localized names should be obtained through trusted provider IDs and the scraper.
23. Movie NFO behavior remains unchanged: a movie NFO is a Combination NFO containing trusted provider identifiers, technical stream data already available for that media item, and a scraper URL when a trustworthy supported ID is available. Do not add a title/name merely to make the XML look complete.
24. **Kodi TV output creates exactly one `tvshow.nfo` at the root of each identified series directory within each quality root.** It contains trusted series identifiers and the Combination NFO scraper URL when a trustworthy supported ID is available. Preserve only technical fields that are appropriate and already available under the current contract. Do not create `tvshow.nfo` in season folders.
25. **Do not create per-episode `.nfo` sidecars in the Kodi TV tree.** STRM episodes must remain eligible for Kodi's internet scraper to discover episode metadata. Do not create episode NFOs to preserve per-file streamdetails. For an unidentified series, do not create a speculative `tvshow.nfo`; keep the source-tree fallback and let Kodi identify media through its scraper.
26. Jellyfin's existing NFO output behavior is unchanged, including its current per-item NFO generation. Kodi movie NFOs continue to follow item 23; the root-only/no-episode-NFO rule applies to Kodi TV output. Keep all written identifiers and technical metadata grounded in source data; do not invent IDs, years, episode coordinates, or stream characteristics.
27. The NFO contract is intentionally output-specific: Kodi TV prioritizes scraper discovery and therefore uses a series-root `tvshow.nfo` without season/episode sidecars, while Jellyfin retains its existing NFO layout. A software/NFO format version change is required only if the serialized format of a surviving NFO changes, not merely because Kodi episode sidecars stop being emitted.

### 3.5 Identity, manifests, and synchronization

28. Distinguish logical media identity (movie ID, or series ID plus season/episode), release identity (full torrent hash), and physical identity (output path plus source-file identity).
29. The manifest, never a parsed filename or short hash, is the source of truth mapping managed STRM/NFO paths to full torrent hashes and source files.
30. Reconciliation must tolerate shared Kodi series/season directories. Removing or updating one torrent may affect only that torrent's entries; it must never delete other releases' entries or a shared folder that still contains managed files.
31. Remove managed directories only when they are empty and no longer needed. If multiple source files map to the same episode, use the exceptional source-file ordinal rule in item 19 to avoid overwriting.
32. Torrents disappearing from TorrServer are reflected in all enabled output trees at the next sync. Removing a Kodi STRM never removes a source torrent.
33. Unknown/insufficiently identified media uses the source-tree fallback. Only explicit supported TorrServer category values are accepted; blank, anime, or unsupported categories remain `_uncategorized`. There is no external category lookup or title-based guess.

## 4. Development phases

Phases 0–7 document the original v1.4.6 implementation and validation. The follow-up phase records two newly agreed behavior changes; it supersedes the earlier quality-root and Kodi TV NFO rules for the next implementation without rewriting the v1.4.6 history.

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

### Phase 8 — Apply the new quality-root fallback and Kodi TV NFO policy

**Status: implementation prepared for v1.4.7; GitHub Actions CI passed (33 tests); mini-PC acceptance remains a release gate.**

**Work**

- Keep ffprobe data already attached to TorrServer as the authoritative quality evidence when it contains usable video dimensions. A maximum dimension of at least 3840 selects `4K`; usable lower dimensions select `1080p`.
- If the embedded ffprobe payload is missing, invalid, or does not provide usable video dimensions, recognize only clear `4K` or `2160p` release-title markers as a fallback for the `4K` root. A title marker must not override usable ffprobe data that proves a lower resolution; `1440p`, codec names, and source tokens alone must not select `4K`.
- Keep the displayed quality label separate from root classification. If the title provides an explicit `4K`/ `2160p` marker but no other quality data is usable, the label may be derived from that marker; do not invent HDR/DV.
- Make Kodi TV NFO generation output-specific: create one `tvshow.nfo` in the root of each identified series directory per quality root, and never create a `tvshow.nfo` in a season folder or an episode-level `.nfo` sidecar. Do not create a speculative series-root NFO for unidentified series.
- Keep Kodi movie NFO behavior and Jellyfin's entire existing path/grouping/NFO behavior unchanged.
- Ensure Kodi's manifest/reconciliation treats the series-root `tvshow.nfo` as a shared managed file: adding or removing one release must not delete it while another release still uses that series root; remove it only when no managed records reference it.
- Add regression cases for usable 4K and non-4K ffprobe payloads, absent/invalid/unusable payloads with and without explicit title markers, conflicting technical dimensions and title markers, and false-positive title tokens.
- Add NFO tests proving that Kodi has a single series-root `tvshow.nfo`, no season- or episode-level NFOs, no root NFO for an unidentified series, unchanged Kodi movie NFOs, and unchanged Jellyfin NFO output.
- On the mini-PC, run a clean sync and rescan the Kodi test library. Confirm that internet scraping resolves episode metadata, localized display names are not overwritten by NFO title fields, and Elementum playback and multi-release reconciliation still work. Update applicable documentation and the changelog when implementation is shipped.

**Exit criteria**

- The quality root follows the technical-first/title-fallback rule exactly, with no additional ffprobe calls or external lookups.
- Kodi TV contains only the identified series-root `tvshow.nfo` and no NFO in season folders or beside episode STRMs; Kodi movie and Jellyfin NFO behavior are unchanged.
- Removing or updating one torrent never removes a shared `tvshow.nfo` still needed by another release.
- Regression tests and real Kodi scraper/playback checks pass on the mini-PC.
### Phase 8b — Bound Kodi path components and prevent overlong directories

**Status: required corrective fix discovered during v1.4.7 mini-PC acceptance; must pass before the formal v1.4.7 tag.**

**Reason**

The mini-PC dry run for *The Prestige* produced a Kodi movie-directory component of 259 UTF-8 bytes, exceeding the common Linux filesystem component limit of 255 bytes. `bounded_kodi_leaf()` already limits STRM/NFO basenames, but `sanitize_kodi_component()` only replaces Windows-incompatible characters and does not limit length. `clean_media_component()` can therefore create an overlong directory even though the item filename itself is bounded. This is a coverage gap, not a reason to remove the existing Windows/SMB sanitization.

**Work**

- Keep the existing Kodi-only Windows/SMB sanitization: replace forbidden Windows filename characters, protect reserved device names, remove trailing dots/spaces, and preserve real `.strm`/`.nfo` extensions. Do not weaken or remove this behavior.
- Add one shared bounded-component helper for Kodi paths. Apply Windows-safe normalization and then enforce a conservative maximum of 255 UTF-8 bytes per component, including any collision suffix. Truncate only on UTF-8 character boundaries.
- Preserve short names exactly. For a component that must be shortened, retain a readable prefix and add a deterministic short identity suffix where needed. The suffix must fit inside the byte budget; do not generate unstable names from process order or timestamps.
- Apply the bound to every generated Kodi directory and file component, including normalized movie/series directory names, fallback torrent-title directories, and torrent-internal folders. Keep the existing filename-specific helpers responsible for their quality/hash suffixes and extensions, while ensuring their results also satisfy the common component invariant.
- Resolve collisions **after** Windows normalization and length truncation. Different long names that collapse to the same shortened component must remain distinct using a stable provider identity or torrent hash; do not rely on names before normalization to guarantee uniqueness.
- Do not change Jellyfin path generation, existing short Kodi names, quality labels, playback URIs, `oindex`, manifest identity semantics, NFO behavior, or deletion safeguards.
- Add regression tests using the *Prestige* title/path from the mini-PC case and adversarial long Unicode names. Assert that every path component in generated Kodi STRM/NFO paths is at most 255 UTF-8 bytes, Windows-incompatible characters and reserved names remain handled, UTF-8 is never split, extensions and quality/hash suffixes survive, truncation collisions are resolved, and repeated syncs do not churn names or damage manifest reconciliation.
- Test that nested source folders for unidentified torrents are bounded too, not only the top-level movie/series directory. Verify representative paths over the actual Windows/SMB share; treat total-path-length behavior as a separate client/configuration constraint rather than assuming a per-component limit guarantees every full path is portable.

**Exit criteria**

- The exact 259-byte *Prestige* directory case produces a valid, stable Kodi path with no component exceeding 255 UTF-8 bytes.
- All generated Kodi path components remain Windows/SMB-safe after truncation, and collision tests prove that distinct items do not merge or overwrite one another.
- Existing short names, `.strm`/`.nfo` extensions, quality/hash conventions, Elementum playback behavior, manifest reconciliation, and all Jellyfin paths remain unchanged.
- New regression tests pass in CI and on the mini-PC before ordinary synchronization and publication of the v1.4.7 tag.

### Phase 9 — Correct quality-root evidence precedence

**Status: agreed follow-up for the next implementation version; not included in v1.4.7.**

**Reason**

Mini-PC dry-run validation showed releases whose torrent/file paths contain explicit `2160p` markers being placed in `1080p` when the torrent title and embedded ffprobe payload do not expose usable quality evidence. The current `default_1080p` fallback is a safe default, not proof that the release is truly 1080p. Likewise, the fact that a torrent arrived through the Sonarr-1080 workflow or another transfer service does not guarantee its actual quality: users may manually select a release outside the instance's usual quality policy.

**Work**

- Keep the classifier TorrServer-only. Do not call ffprobe, request `/ffp/{hash}/{file_id}`, or query external metadata providers.
- Apply this quality-root evidence order, stopping at the first recognized, usable source:
  1. **Existing ffprobe dimensions:** use valid video-stream dimensions already embedded in TorrServer metadata. A maximum dimension of at least 3840 selects `4K`; valid lower dimensions select `1080p`. This evidence is authoritative over all text/metadata hints.
  2. **Explicit structured quality in TorrServer metadata:** inspect recognized fields whose semantics describe the release's actual resolution. A recognized 4K-class value selects `4K`; a recognized lower-resolution value selects `1080p`. Ignore values that do not unambiguously identify resolution and fields that only denote an import source, Sonarr instance/quality profile, or intended download policy.
  3. **Explicit release markers:** when the higher-priority sources are absent/unusable, inspect standalone `4K`/`2160p` markers in the TorrServer torrent title and release names present in its file paths. A recognized marker selects `4K`. Do not infer 4K from source/codec labels alone, and guard against partial-token false positives.
  4. **Default:** if none of those sources supplies usable evidence for 4K, use the `1080p` root. This is a catch-all folder, not a claim that the content is actually 1080p.
- Do not treat the Sonarr-1080 instance, its import/transfer route, or other provenance indicators as quality evidence. They must not downgrade a torrent when stronger embedded metadata or explicit release markers identify 4K. The same applies to any source-workflow label: ingestion origin is not a substitute for media quality.
- Keep the root decision separate from the displayed quality label. A path marker may select the root but must not fabricate an HDR/DV label or otherwise override the existing label-source policy.
- Add tests for all four evidence stages; conflicts between dimensions, structured metadata, torrent title, and release paths; manual 4K imports through the Sonarr-1080 workflow; false-positive tokens; and the default when no usable evidence exists.
- Preserve the policy that no new ffprobe request or external lookup is made and that all non-4K qualities share the `1080p` root.

**Exit criteria**

- The first available recognized evidence source determines the root in the order above.
- Usable ffprobe dimensions and recognized structured quality metadata are never overridden by title/path text.
- Explicit release-level `4K`/`2160p` markers in file paths can select `4K` even when the torrent entered through Sonarr-1080, provided no higher-priority contradictory evidence exists.
- `1080p` remains the default only when no higher-priority source supports 4K; the folder/source workflow itself is never treated as proof of resolution.
- All regression tests pass, including the mini-PC dry-run cases, before the updated classifier is used for ordinary synchronization.

## 5. Explicit non-goals

- Do not redesign Jellyfin's existing path/grouping or NFO behavior as part of Kodi normalization.
- Do not add more quality-root categories: only `4K` and `1080p` exist.
- Do not infer resolution from codec/source tokens alone. Quality-root evidence must follow Phase 9's priority: usable ffprobe dimensions, an explicit structured value describing the release's actual quality, and then explicit `4K`/`2160p` markers in the torrent title or release/file paths. Import provenance or a Sonarr quality-profile label alone is not proof of the media's actual resolution.
- Do not issue any ffprobe request, call TorrServer `/ffp/`, or contact JacRed, Prowlarr, or any other external metadata source. Reuse only technical data already present in TorrServer or cached NFOs for the limited purposes described above.
- Do not create season-folder or per-episode NFO sidecars in Kodi TV output; do not alter Jellyfin's existing NFO behavior.
- Do not merge releases using approximate title similarity or invent missing provider IDs/season/episode numbers.
- Do not make Kodi output authoritative for deletion and do not implement/use `action=drop`.
- Do not include `hotcached` work in this roadmap.

## 6. Definition of done

The follow-up is complete when the independent Kodi tree groups identified releases by stable IDs; unknown items preserve their source layout; quality-root evidence follows the order of existing ffprobe dimensions, explicit structured TorrServer quality metadata, explicit `4K`/`2160p` markers in torrent/file-path release names, and finally the `1080p` default; all Kodi path components are Windows/SMB-safe, UTF-8-bounded to at most 255 bytes, and collision-safe after truncation; Kodi TV emits `tvshow.nfo` only at each identified series root and no season/episode NFOs; movie NFO behavior and Jellyfin output behavior remain intact; manifests reconcile shared folders safely; Kodi's internet scraper can identify episodes; playback works; and deletion-safety invariants remain intact.

## 7. Current status

- [x] Phase 0 — Behavior decisions finalized
- [x] Phase 1 — Torrent-level quality resolution implemented
- [x] Phase 2 — Title-free NFO contract implemented
- [x] Phase 3 — Logical identity and Kodi path planning implemented
- [x] Phase 4 — Independent Kodi tree builder implemented
- [x] Phase 5 — Manifest reconciliation and clean-rebuild path implemented
- [x] Phase 6 — Regression tests passed in GitHub Actions and on the mini-PC (32 tests)
- [x] Phase 7a — Real-player validation on the mini-PC: Windows/SMB paths and Kodi playback verified
- [x] Phase 8a — Implement the 4K title-marker fallback and Kodi TV root-only NFO policy; GitHub Actions passed (33 tests)
- [ ] Phase 8b — Bound Kodi path components; add Windows/SMB and UTF-8 length regression tests (release blocker)
- [ ] Phase 8c — Clean rebuild and mini-PC acceptance: Kodi internet scraping, Elementum playback, shared-series reconciliation, and bounded-path validation
- [ ] Phase 8d — Publish the formal `v1.4.7` release tag only after Phase 8b and Phase 8c pass
- [ ] Phase 9 — Implement expanded quality-root evidence precedence in a subsequent version after v1.4.7 acceptance

The v1.4.6 implementation remains the historical validated baseline. The v1.4.7 candidate is already on `main` for controlled mini-PC acceptance; the overlong-directory defect found during testing is a release blocker and must be fixed before ordinary synchronization and tagging. Publish v1.4.7 only after Phase 8b path-boundary tests and Phase 8c clean-rebuild/player acceptance pass. Phase 9 is a separate accepted follow-up and must not be mistaken for behavior already implemented in v1.4.7.
