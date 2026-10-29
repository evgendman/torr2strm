# torr2strm Development Roadmap

> **v1.4.12 was published, installed, and accepted on the reference mini-PC on 2026-10-23.** GitHub Actions passed compilation and 49 regression tests. The controlled mini-PC sync verified the Kodi TV canonical-directory migration for all 10 episodes, preserved every STRM payload, and a second live run reported `created=0 updated=0 unchanged=304 failed=1` with no path, mode, `mtime`, or `ctime` changes across 3,401 inspected filesystem objects. Phases 14–16 are accepted for the tested scenarios. Jellyfin log review for 12:10–12:39 showed periodic Webhook notifier completion roughly every 91 seconds, with no explicit scan/refresh/watcher entries in the filtered lines; the dashboard showed the last full-library scan about four hours earlier (duration about one minute). This is reasonable acceptance evidence, not proof that no incremental refresh occurred. The timer was enabled at 12:52:38 +05 on 2026-10-23. Its immediate catch-up activation completed at 12:53:05 and two normal five-minute periodic activations ran 12:57:39–12:58:05 and 13:02:40–13:03:07. All three exited successfully with `created=0 updated=0 unchanged=304 failed=1`. Jellyfin's filtered log showed only its existing ~91-second Webhook notifier cadence and no explicit scan/refresh/watcher entries around either periodic cycle. No recurrence of the suspected scanning anomaly was observed during these two cycles; this does not prove that no incremental refresh occurred. Phase 10, the separate torr2jellyfin orchestration interface, remains deferred.
> **v1.4.15 was published, installed, and accepted on reference MINI-Ubuntu on 2026-10-29.** GitHub Actions passed compilation and all 59 tests for PR #22. The official release archive checksum and installed-source SHA-256 were verified. A controlled live sync completed with exit status 0 across 635 torrents (`quality_ready=635 quality_unresolved=0 created=0 updated=15 unchanged=1255 failed=0`, 311.28 seconds). Post-sync validation passed for both 635-record manifests, all 33 corrected-season STRM targets in Jellyfin and Kodi, and Kodi episode numbering for X-Files S03 (24 episodes) and The Umbrella Academy S03 (10 episodes). The timer was restored and verified active/enabled. The dry-run's Jellyfin-removal logging gap remains a non-blocking follow-up; no second idempotency run is claimed.
> Baseline: prior implementation v1.4.0.
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

**Status: implementation, regression tests, CI, and mini-PC path acceptance complete.** GitHub Actions passed 36 tests; the clean rebuild and Windows/SMB component audit passed on 2026-10-14. The formal release tag remains a separate publication step.

**Reason**

The mini-PC dry run for *The Prestige* produced a Kodi movie-directory component of 259 UTF-8 bytes, exceeding the common Linux filesystem component limit of 255 bytes. `bounded_kodi_leaf()` already limits STRM/NFO basenames, but `sanitize_kodi_component()` only replaces Windows-incompatible characters and does not limit length. `clean_media_component()` can therefore create an overlong directory even though the item filename itself is bounded. This is a coverage gap, not a reason to remove the existing Windows/SMB sanitization.

**Work**

- Keep the existing Kodi-only Windows/SMB sanitization: replace forbidden Windows filename characters, protect reserved device names, remove trailing dots/spaces, and preserve real `.strm`/`.nfo` extensions. Do not weaken or remove this behavior.
- Add one shared bounded-component helper for Kodi paths. Apply Windows-safe normalization and then enforce a conservative maximum of 255 UTF-8 bytes per component, including any collision suffix. Truncate only on UTF-8 character boundaries.
- Preserve short names exactly. For a component that must be shortened, retain a readable prefix and add a deterministic short identity suffix where needed. The suffix must fit inside the byte budget; do not generate unstable names from process order or timestamps.
- Apply the bound to every generated Kodi directory and file component, including normalized movie/series directory names, fallback torrent-title directories, and torrent-internal folders. Keep the existing filename-specific helpers responsible for their quality/hash suffixes and extensions, while ensuring their results also satisfy the common component invariant.
- Resolve collisions **after** Windows normalization and length truncation. Different long names that collapse to the same shortened component must remain distinct using a stable provider identity or torrent hash; do not rely on names before normalization to guarantee uniqueness.
- Do not change Jellyfin path generation, existing short Kodi names, quality labels, playback URIs, `oindex`, manifest identity semantics, NFO behavior, or deletion safeguards.
- Do not migrate test STRM trees or manifests during this development cycle. For acceptance, stop the service/timer and clear only the configured generated Jellyfin/Kodi output roots, including manifests and root markers, then rebuild from TorrServer inventory. Preserve the configuration and TorrServer data.
- Add regression tests using the *Prestige* title/path from the mini-PC case and adversarial long Unicode names. Assert that every path component in generated Kodi STRM/NFO paths is at most 255 UTF-8 bytes, Windows-incompatible characters and reserved names remain handled, UTF-8 is never split, extensions and quality/hash suffixes survive, truncation collisions are resolved, and repeated syncs do not churn names or damage manifest reconciliation.
- Test that nested source folders for unidentified torrents are bounded too, not only the top-level movie/series directory. Verify representative paths over the actual Windows/SMB share; treat total-path-length behavior as a separate client/configuration constraint rather than assuming a per-component limit guarantees every full path is portable.

**Exit criteria**

- The exact 259-byte *Prestige* directory case produces a valid, stable Kodi path with no component exceeding 255 UTF-8 bytes.
- All generated Kodi path components remain Windows/SMB-safe after truncation, and collision tests prove that distinct items do not merge or overwrite one another.
- Existing short names, `.strm`/`.nfo` extensions, quality/hash conventions, Elementum playback behavior, manifest reconciliation, and all Jellyfin paths remain unchanged.
- New regression tests pass in CI and on the mini-PC before ordinary synchronization and publication of the v1.4.7 tag.
- Acceptance starts from clean generated output roots; no data migration is attempted, and the TorrServer inventory/configuration remain untouched.

### Phase 9 — Correct quality-root evidence precedence

**Status: implemented in v1.4.8; CI passed, mini-PC acceptance pending.**

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

### Phase 10 — Backward-compatible orchestration and run reports for torr2jellyfin

**Status: deferred by decision; explicitly excluded from v1.4.8.** This phase must not change or remove the existing standalone workflow.

**Goal**

Allow `torr2strm` to run either as a standalone synchronizer under its existing CLI/systemd timer, or as the execution engine launched and monitored by the Jellyfin plugin `torr2jellyfin`. Both modes must use the same synchronization implementation, configuration semantics, manifests, output layout, and safety rules.

**Work**

- Preserve the existing standalone interface: `--config`, `--dry-run`, `--version`, the current TOML configuration, and operation via `torr2strm.service` + `torr2strm.timer`. Do not make the Jellyfin plugin a mandatory dependency.
- Define an explicit deployment/operation choice: (a) standalone automatic mode, where systemd timer controls regular synchronization; or (b) plugin-controlled mode, where the plugin initiates runs and owns the user-facing schedule. Document how to ensure the timer does not start duplicate cycles in plugin-controlled mode.
- Prefer a one-shot CLI/service invocation over adding a permanently running HTTP server in the first integration version. Choose the exact plugin-to-process/systemd launch mechanism with least privilege; do not grant the Jellyfin process unrestricted root execution.
- Add a non-mutating health/check command (for example `--check`) that validates the configuration, reachability of TorrServer, output-root state, and relevant filesystem access. It must not execute a normal synchronization or alter media trees/manifest state. Clearly distinguish a binary/version check from a full dependency health check.
- Add a versioned machine-readable JSON run report (for example through `--report-json PATH`) independent of human-readable logs. The report should include run ID, service/software and report-schema versions, start/end time, overall state, exit status, configured output roots, aggregate counts, warnings, fatal errors, and per-item/per-output error summaries where practical.
- Report meaningful change information by logical subdirectory, at minimum `movie/4K`, `movie/1080p`, `tv/4K`, and `tv/1080p`, while also handling `_uncategorized` and any future/other generated subdirectories. Track changes using the synchronizer's own reconciliation operations/manifest, not by requiring the plugin to duplicate manifest interpretation or rescan the filesystem independently.
- Capture additions, updates, removals and any other changes to STRM/NFO files relevant to Jellyfin scanning. Do not infer that a directory changed solely because the manifest timestamp or whole manifest file changed.
- Distinguish successful completion, completion with per-item warnings/errors, fatal run failure, and cancellation/interruption. The current behavior may return success for a run with recoverable per-torrent failures, so the JSON report must not equate exit code zero with a completely clean media run.
- Publish the report atomically and make its schema durable/documented. Write useful failure information even when configuration loading or the source request fails, as far as the invocation context permits. Retain normal logs for diagnostics.
- Ensure the same inter-process lock protects every execution entry point: manual CLI, systemd timer, and plugin-triggered execution. The existing `flock` wrapper in the systemd unit alone does not protect a caller that invokes the Python script directly. Move locking into a common launcher/service entrypoint or another shared mechanism and test contention explicitly.
- If plugin-controlled mode is active, there must be one authoritative automatic scheduler. Avoid an active systemd timer plus a separate plugin timer unless an explicit supported coordination design prevents duplicate runs. Manual standalone execution should remain available where operationally safe.
- Add tests for report schema/versioning, empty and unchanged syncs, additions/updates/removals in each logical subdirectory, uncategorized items, partial item failures, fatal configuration/source failures, cancellation, concurrent launch attempts, and compatibility of existing standalone CLI/timer operation.
- Document the integration contract for `torr2jellyfin`: invocation method, check semantics, exit codes, report path/schema, status meanings, per-directory change semantics, lock behavior, and mode-selection procedure.

**Exit criteria**

- Existing CLI and standalone systemd-timer behavior continue to work unchanged for existing users.
- Plugin-triggered runs execute the same synchronizer exactly once, cannot mutate the output concurrently with another run, and return a durable structured report.
- The report accurately identifies which logical output directories had media-file changes, including removals, and distinguishes clean success, partial errors and fatal failure.
- The plugin can decide whether to scan each Jellyfin library using the report without parsing human-readable logs or independently reimplementing the manifest logic.
- The standalone and plugin-controlled installation modes are documented and covered by regression tests; the Jellyfin plugin is not a runtime requirement for standalone installations.

### Phase 11 — Isolate temporarily unavailable torrents and preserve their existing output

**Status: implemented in v1.4.8; CI passed, mini-PC acceptance pending.**

**Reason**

Currently, `_get_snapshot_list()` raises a fatal error if a torrent appears in the TorrServer inventory but its file structure (`FileStats`) remains absent after the metadata-load request and bounded wait. Because this happens before output reconciliation starts, one problematic torrent aborts the entire synchronization cycle. The intended improvement is to let healthy torrents synchronize while ensuring a temporarily unreadable torrent is not mistaken for a source deletion and its existing Jellyfin/Kodi STRM/NFO tree is not removed.

**Work**

- Separate a globally unusable source inventory/API failure from a recoverable per-torrent metadata failure. A failed list request, structurally invalid inventory, or other error that makes the overall source snapshot untrustworthy may remain fatal; a single hash whose details/FileStats are temporarily unavailable should be isolated.
- Track hashes that are definitely present in the raw TorrServer inventory but were deferred because `action=get`, metadata loading, `FileStats` parsing, or another explicitly recoverable per-item source step failed. Record a structured warning with hash, stage, reason, and retry-on-next-run semantics.
- Continue snapshot parsing, quality processing, and output synchronization for all other healthy torrents. Report deferred/unavailable counts separately from deterministic skips such as a valid torrent with no playable video files.
- Protect each deferred hash during reconciliation. If either output manifest already contains that hash, retain its previous manifest record and its managed directory/STRM/NFO files unchanged; do not reinterpret the absent new snapshot as `torrent_missing_on_torrserver`, and do not prune shared Kodi series/season content still referenced by the retained record.
- If a deferred hash has never been materialized, do not invent a file tree without valid FileStats. Record it as deferred and retry on the next scheduled run.
- Keep the meaning of “source removed” strict: only a hash absent from a successfully retrieved TorrServer inventory is eligible for the normal source-disappearance cleanup path. A hash visible in inventory but missing metadata is not confirmed removed.
- Once valid metadata becomes available on a later run, process the hash normally and reconcile its tree against the new verified file list. Retention while deferred must not prevent legitimate updates or removals after a later successful snapshot.
- Ensure deferred hashes are protected in both Jellyfin and Kodi, including shared Kodi series roots and manifests; do not enable Kodi reverse deletion and do not weaken the existing opt-in Jellyfin deletion policy.
- Add tests for: one failing FileStats hash alongside healthy hashes; preservation of existing STRM/NFO files and manifest records in both outputs; a first-seen deferred hash creating no guessed output; genuinely absent source hashes still being cleaned up; recovery after FileStats become available; and truly global source/API failure remaining fatal. Verify summary counters and exit/report semantics distinguish a partial/deferred run from complete success.

**Exit criteria**

- A single temporary FileStats/metadata failure no longer aborts synchronization of otherwise healthy TorrServer items.
- Existing output files and manifest records for deferred hashes remain byte-for-byte unchanged until a valid later snapshot is available.
- Deferred hashes cannot be removed from output trees merely because the current run could not read their file structure.
- Confirmed source disappearance, intentional Jellyfin reverse deletion, shared Kodi-folder safety, and normal successful reconciliation retain their current behavior.
- Automated regressions pass before the resilience behavior is deployed to scheduled synchronization.

### Phase 15 — Stable Kodi series naming and quality-unresolved retention

**Status: implemented in v1.4.12 and accepted on the reference mini-PC on 2026-10-23 for the tested scenarios.** Identified during reference mini-PC diagnosis on 2026-10-23; the canonical title/year and quality-unresolved retention fixes were verified with a controlled migration and a subsequent idempotent run. The timer was re-enabled after that acceptance.

**Reason**

The Kodi series directory changed between syncs from `The White Lotus (2021)` to `The White Lotus (2022)` and back. The current canonical-name candidate pool is derived from `quality_ready`, so a release whose quality could not be resolved in a run is excluded from naming even if its torrent and trusted series identity remain present in TorrServer. When the candidate pool changes, the directory year can change and reconciliation can move STRM/NFO paths. A related risk is that an in-inventory torrent omitted from `quality_ready` may not appear in the next manifest; it must not be treated as a confirmed source disappearance merely because quality preparation failed.

**Agreed behavior**

- For an identified series, compute its canonical Kodi directory identity/name from the successfully read TorrServer inventory snapshots, not only from `quality_ready`. Use trusted series IDs already present in TorrServer metadata; do not contact TMDb or any other external metadata provider.
- Choose the display title using the existing supported explicit-title priorities and a deterministic tie-breaker. Determine the year separately: collect valid series-year candidates from all available releases of that series, across seasons and quality roots, using the existing metadata/title evidence; choose the numerically smallest valid year. If no valid year is available, omit it. Do not let quality readiness, input order, or quality root select a different canonical year.
- Use the same canonical series name/year for that trusted series identity in each Kodi quality root. Keep the existing collision handling for distinct series IDs that resolve to the same readable name/year.
- Separate source-inventory presence from output materializability. If a hash is present in a successfully read TorrServer inventory but its quality resolution is temporarily unresolved, retain its previous managed manifest records and STRM/NFO files rather than treating its omission from `quality_ready` as a source deletion. It must not cause canonical series naming to fall back to another release for that run.
- When a valid quality-ready snapshot becomes available again, reconcile the item normally against its verified metadata and file list. A hash genuinely absent from a successfully retrieved TorrServer inventory remains eligible for normal source-disappearance cleanup.
- Keep deterministic permanent skips distinct from transient failures. In particular, a valid inventory entry with no playable video files (for example, the known Blu-ray ISO case) must not generate a guessed output tree; define and test its explicit disposition so preservation for transient quality failures does not become indefinite retention of invalid media.
- Preserve the existing output safety contract: Kodi remains read-only with respect to TorrServer, `action=drop` is never used, and Jellyfin path/grouping behavior is not redesigned.

**Regression tests**

- Same trusted series ID appears in multiple seasons/releases with years 2021 and 2022: the canonical directory consistently uses 2021 regardless of input order, which releases are quality-ready, and whether releases are assigned to different quality roots.
- Temporarily unresolved quality for one release does not rename the shared series directory, move healthy episode links between year-named directories, or remove the previous managed files/manifest records for that still-present hash.
- Recovery after quality resolution succeeds reconciles the torrent normally; a hash genuinely absent from a successful source inventory is still cleaned up.
- A permanent no-playable-video skip creates no speculative STRM tree and follows the explicit skip/retention policy; Kodi never triggers TorrServer deletion.
- Repeat sync after recovery is idempotent and leaves canonical paths unchanged.

**Exit criteria**

- The canonical Kodi series title/year is deterministic over the complete available inventory and cannot fluctuate solely because `quality_ready` membership changes.
- A torrent still present in a valid TorrServer inventory cannot lose previously managed output solely because quality resolution failed for that run.
- Temporary unresolved quality, permanent non-playable skips, global inventory failures, and confirmed source disappearance have distinct, tested reconciliation semantics.
- Automated regressions pass and a reference mini-PC dry run confirms stable Kodi paths and manifests before `torr2strm.timer` is re-enabled.

### Phase 16 — Prevent redundant permission changes from triggering Jellyfin filesystem events

**Status: implemented in v1.4.12 and accepted on the reference mini-PC on 2026-10-23 for the tested scenarios.**

**Permission-policy decision**

Keep the current explicit per-object mode policy: `0666` for managed files and `0777` for managed directories. Inherited permissions using `setgid` or default ACLs are out of scope for this phase. Phase 16 must prevent redundant `chmod` calls without narrowing these target modes.

**Problem and evidence**

- Jellyfin's library monitor watches filesystem attribute changes in addition to file/directory creation, deletion, rename, write time, and size. A permission change can therefore emit a watched event even when file contents and modification time are unchanged.
- In the installed source, `_write_strm()` and `_write_nfo()` call `os.chmod(path, 0o666)` for existing files whose content already matches the planned content. `_mkdir()` calls `os.chmod(path, 0o777)` for existing directories. These calls are unconditional outside dry-run mode, even when the current permission bits already match the requested mode.
- During the 2026-10-23 no-op run, the summary was `created=0 updated=0 unchanged=304 failed=1`. Representative series/season directories and existing NFOs had old `mtime` values but `ctime` values clustered around the run. This points to metadata-only churn rather than content or directory-tree changes.
- Periodic completion messages for Webhook item-added/item-deleted notifier tasks do not prove that new media were added or that Jellyfin ran a full-library scan. Use explicit watcher/scan entries to assess the scope of any refresh.

**Implementation**

- In all three sites (`_write_strm()`, `_write_nfo()`, and `_mkdir()`), compare the current permission bits with the desired mode before calling `chmod`. Apply `chmod` only when they differ. Preserve target modes: `0666` for managed STRM/NFO files and `0777` for managed directories.
- Keep normal file creation/content replacement, atomic writes, reconciliation, and output paths unchanged. This is a narrowly scoped filesystem-event fix, not a redesign of Jellyfin monitoring or of torr2strm synchronization.
- Add regression tests for each of the three paths: no `chmod` when the target mode is already correct, and `chmod` still occurs when the mode differs and must be corrected.

**Reference mini-PC validation**

- The timer remained stopped during installation, preflight, dry run, and controlled sync. Source/version checks and the pre-release CI checks passed.
- The second live sync reported `created=0 updated=0 unchanged=304 failed=1`; a snapshot comparison across 3,401 filesystem objects found no added/missing paths, timestamp/type changes, or permission corrections.
- Jellyfin logs for 12:10–12:39 showed periodic Webhook notifier completions roughly every 91 seconds, but no explicit scan/refresh/watcher entries in the filtered lines. The dashboard showed the last full-library scan was about four hours earlier and took about one minute. This is adequate practical evidence for acceptance, while not proving that no incremental refresh happened.
- Phase 16 is accepted for the tested scenarios. `torr2strm.timer` was enabled at 12:52:38 +05. The immediate catch-up run (12:52:38–12:53:05) and two normal periodic runs (12:57:39–12:58:05 and 13:02:40–13:03:07) all exited 0 with `created=0 updated=0 unchanged=304 failed=1`. Jellyfin's filtered log showed the same ~91-second Webhook notifier cadence, including one message before the 13:02:40 run, and no explicit scan/refresh/watcher entries. No scan/watch anomaly was observed across the two periodic cycles; this is practical observation, not proof that incremental refreshes never occur.

**Exit criteria**

- [x] Automated tests cover all three call sites and pass in CI.
- [x] Correct permission bits are retained without invoking `chmod`; incorrect bits are repaired.
- [x] A reference mini-PC no-op sync with no created/updated outputs caused no mass metadata-only timestamp churn in existing managed paths.
- [x] Jellyfin scan/refresh evidence was reviewed and the version accepted for the tested scenarios; limitations of the log evidence are documented.

### Phase 17 — Preserve category across metadata reloads and defer uncertain category changes

**Status: implemented and published in v1.4.14; reference mini-PC installation and first live-sync verification completed on 2026-10-27 for the observed inventory.** The 56-test suite and GitHub Actions CI/release workflow succeeded. Triggered by the Baywatch (1989) directory churn observed on the reference mini-PC on 2026-10-25. The missing fallback in the post-metadata-reload code path is confirmed; the historical category value returned by TorrServer during the incident was not logged, so a transient TorrServer-side category omission remains a hypothesis rather than a proven source-side fault.

**Problem and evidence**

- Two consecutive torr2strm runs, at approximately 11:07 and 11:12 +05, logged METADATA_LOAD and METADATA_READY for the same Baywatch S03 torrent hash.
- In the first run, Jellyfin output moved that hash from tv/... to _uncategorized/... with reason=identity_changed. Kodi then removed 22 previously managed episode STRMs as unreferenced and pruned the empty Season 03 directory. The next run moved the same hash back to tv/... and recreated the season directory and episode links.
- arr2torr reported candidates=0 and submitted=0 around the event; TorrServer's listed torrent count remained 271. These observations do not support a newly imported torrent as the trigger.
- In _get_snapshot_list(), the ordinary action=get path already fell back to the category from action=list when the detail response had no category. The post-load refreshed action=get path did not apply that fallback and passed an empty category to parse_snapshot(), which mapped it to _uncategorized. Candidate v1.4.14 applies the fallback consistently and preserves managed output if neither current response supplies a category and prior manifests provide a trusted category.
- Current read-only queries returned category=tv for the Baywatch hash. They cannot establish what TorrServer returned during the two historical runs.

**Implementation**

- Retain the category from the original action=list item throughout per-torrent snapshot acquisition. Validate fallback values against the existing supported set (movie, tv); do not infer category from titles, file paths, external providers, or import provenance.
- After the metadata-loading/retry loop obtains a refreshed action=get response, apply the same missing-category fallback before parse_snapshot(). Use the refreshed response when it supplies a supported category; use the validated list category only when the refreshed category is absent or blank. Do not silently overwrite an explicit unsupported category with a different category.
- If neither response supplies a trusted category and the hash already has a managed manifest record with a trusted movie/tv category, treat the category as temporarily unresolved for that run. Defer that hash and preserve its previous manifest records and output files instead of moving it to _uncategorized or cleaning up its old paths. Reconcile normally when a trusted category becomes available again.
- Keep the existing policy for explicit unsupported categories: they map to _uncategorized; do not add title-based guesses or external category enrichment.
- Add concise diagnostics that distinguish list-category fallback, refreshed-response category, and category-unresolved deferral, without dumping full torrent metadata into logs. Ensure a deferred category cannot be mistaken for a confirmed source disappearance.

**Regression tests**

- A torrent whose list entry has category=tv, whose initial action=get has no FileStats, and whose post-load response has FileStats but no category retains tv and its normalized Kodi/Jellyfin paths.
- The same fallback behavior works for movie; a supported category returned by the refreshed detail response takes precedence over the fallback.
- An explicitly unsupported category remains _uncategorized; category is never guessed from the title or file tree.
- When all category responses are missing and a previous manifest record has a trusted category, both output manifests and managed files are retained for that run. A later valid response recovers normally; a hash genuinely absent from a successfully retrieved inventory still follows normal source-disappearance cleanup.
- A repeat sync after recovery is idempotent and does not emit a spurious RENAME ... reason=identity_changed.

**Exit criteria**

- [x] The complete local suite passes: 56 tests, including regressions for both movie and TV category fallback, refreshed-category precedence, explicit unsupported categories, and retention/recovery when category is unresolved.
- [x] GitHub Actions passed the complete suite for PR #18 (56 tests); the v1.4.14 release workflow also passed.
- [x] The reference mini-PC dry run planned no renames or managed-file removals for the current inventory; a category fallback was observed. The exceptional Baywatch-style category transition was not replayed as a live mutation.
- [x] Automated validation confirms that unresolved category metadata preserves both prior manifest records and managed files; explicit unsupported categories remain _uncategorized.
- [x] Confirmed a further live no-op run after installation and restoration of the monotonic timer: `torrents=431 quality_ready=431 quality_unresolved=0 created=0 updated=0 unchanged=862 failed=0`. The source inventory count differed from the first live run, so this confirms a clean run for the then-current inventory, not identical source inventory across both snapshots.

### Phase 18 — Structured, user-facing Apprise notifications

**Status: implemented in v1.4.13 and carried forward into installed v1.4.14.** The shared Apprise route was delivery-tested on the reference mini-PC on 2026-10-22; the upgrade preserved the existing notification configuration and state. The implementation preserves current media-tree, manifest, NFO, TorrServer-removal, and Apprise gateway contracts.

**Problem**

Apprise currently forwards most captured `WARNING` and `ERROR` log lines directly. Exact-string deduplication is not semantic grouping: a single problem affecting multiple files or torrents appears as multiple technical bullets, while different reasons may be collapsed into an unhelpful generic presentation. Some recoverable or successful operations are logged at warning level, and some exception messages omit the actual failure detail. The `TORRENT_DEFERRED` example can repeat the torrent hash as both `hash` and `reason` (notably when `action=get` returns HTTP 404), leaving the user unable to tell what happened.

**Planned behavior**

- Separate diagnostic logging from user-facing notification rendering. Preserve technical event codes, paths, hashes, file IDs, tracebacks, and detailed per-file records in the local journal; do not use raw log lines as the notification model.
- Build structured notification events with an event type, severity, output tree, canonical torrent identity, media/release title when available, concise reason, consequence, and retry/next-action information where applicable.
- Group events by problem type and media/torrent. Emit one plain-language description of the problem, followed by affected media; within a media block, list affected source files and conflicting destination names when relevant. Multiple distinct problems for the same media remain separate bullets under the same media heading.
- Reuse inventory/list metadata to resolve torrent titles even when a later TorrServer request fails. Use the canonical hash as an explicit fallback when no reliable title is available; never invent a media name.
- Preserve actionable causes for source failures. Distinguish at least HTTP 404/not-found, connection errors, timeouts, invalid responses/JSON, metadata/FileStats unavailability, and unknown errors. Do not allow an exception string that is only a hash to masquerade as the cause.
- Make user-facing copy natural and explicit about outcome: temporarily deferred and retrying; output preserved; media skipped and managed output cleaned; collision safely resolved; or synchronization blocked by a path/write conflict. A successful cleanup or collision resolution must not be worded as a failed operation solely because its journal record uses `WARNING`.
- Keep the existing severity routing (`warnings` and `critical` tags), Apprise HTTP payload contract, optional-notifications behavior, per-run aggregation, and suppression of unchanged notification fingerprints unless tests demonstrate a necessary compatibility fix.
- Bound the message to the downstream chat channel's practical size. If content must be truncated, preserve group headings, media names, problem summaries, and a count of omitted items.
- Update README notification documentation to describe the user-facing format and the diagnostic-journal split.

**Regression coverage and acceptance**

- Test each renderer/grouping rule with multiple torrents, multiple files for one media, multiple issue types for one media, and deterministic ordering.
- Test title resolution and hash fallback when `action=get` fails, including HTTP 404, timeout, connection failure, and an exception whose text is only a hash.
- Test that resolved STRM collisions produce one user-facing summary per torrent/output while per-file technical diagnostics remain in the journal; unresolved collisions remain critical and clearly identify the media and conflicting paths.
- Test that successful removal/cleanup warnings are not presented as failures, while genuine write/path conflicts and fatal errors retain critical routing and useful context.
- Test unchanged-fingerprint suppression, clean-run reset, message-size limits, and existing Apprise gateway fields/tags.
- Run the full automated suite and CI. Before live acceptance, use a dry run or controlled fixtures and confirm existing STRM/NFO trees and manifests are unchanged by notification-only changes.

### Phase 19 — Reliable season and episode parsing from file paths and torrent titles

**Status: implemented in v1.4.15, published, installed, and accepted on reference MINI-Ubuntu on 2026-10-29.** See [PR #22](https://github.com/evgendman/torr2strm/pull/22) and the [v1.4.15 release](https://github.com/evgendman/torr2strm/releases/tag/v1.4.15).

**Problem**

The season/episode parser combines the file path and torrent title into one string and searches it as if both sources had the same semantics. For filenames such as `Black.Mirror.S03E01_Dub_...` and `Squid.Game.S02E01_RHS_...`, the word-boundary assertion after the episode number fails because underscore is a Python word character. The season-only fallback can then backtrack and match only `S0` inside `S03E01` or `S02E01`. Since the first match is accepted, the parser returns season 0 and never reaches the valid season marker in the torrent title.

This was confirmed on 2026-10-29 for four releases: Black Mirror S03 (6 episodes), Black Mirror S04 (6), Squid Game S02 (7), and Squid Game S03 (6). Their Jellyfin and Kodi outputs were placed in `Season 00` despite the release titles containing explicit season markers. Ordinary dotted names such as `Lucifer.S02E01.1080p...` currently parse correctly and must remain so.

**Planned behavior**

- Parse sources independently. Do not concatenate a file path and torrent title into one undifferentiated string before determining which source supplied a match.
- First parse complete season/episode coordinates from the file path. Support common separators after the episode number, including underscores, dots, and hyphens, without requiring a word boundary that rejects valid release filenames. Prevent partial-number matches and preserve safeguards against matching extra digits.
- Parse season-only markers in the file path separately, including supported English forms such as `Season 3` and Russian directory names such as `3 сезон`. A season-only pattern must not partially match the season digits of an episode token such as `S03E01`.
- Independently parse an explicit season marker from the TorrServer torrent title, including normalized forms such as `S03`, `[S03]`, and other deliberately supported title forms. Use this as a fallback when the file path does not provide a valid season, including when the episode number remains unknown. Do not allow a malformed or partial file-path match such as `S0` to suppress a valid title-level season.
- Define and test deterministic behavior when valid season evidence conflicts between a file path and the torrent title. Do not silently treat an invalid partial match as stronger evidence than a valid explicit marker.
- Keep torr2strm autonomous: all evidence must come from the TorrServer file paths and torrent title. Do not require the adapter to have produced the release, call the adapter, or add network/external metadata lookups. The adapter-normalized torrent title is a useful source when present, not a mandatory dependency.
- Keep missing-season behavior unchanged when neither source supplies a valid season: use the existing `Season 00` fallback and retain a clear warning. Do not invent season or episode numbers.
- Apply the shared parsing result consistently to Jellyfin season placement/NFO coordinates and Kodi season placement/episode basenames. Preserve the existing Jellyfin path/grouping algorithm otherwise, the Kodi tree and playback contract, manifests, and the NFO schema unless a separately justified format change is demonstrated.

**Regression coverage**

- Black Mirror S03/S04 paths with `S03E01_Dub_...` and `S04E01_...`; Squid Game S02/S03 paths with `S02E01_RHS_...` and `S03E01_RHS_...`. Assert seasons 3, 4, 2, and 3 respectively and correct episode numbers for all 25 reported episodes.
- Normalized torrent titles containing `S03`, `[S03]`, `S04`, `[S02]`, or `[S03]` still provide the correct season when a filename does not supply one.
- File paths containing Russian `N сезон` and English `Season N`; dotted filenames and the existing `Lucifer.S02E01.1080p...` control.
- Standalone season markers must not partially match episode coordinates; explicitly cover the former `S03E01` → `S0` failure, adjacent digits, unsupported/ambiguous tokens, and conflicts between valid file-level and title-level season evidence.
- Verify the inferred season is used consistently by Jellyfin and Kodi while the parser does not fabricate an episode number when only the season is known. Missing evidence must still reach `Season 00`.
- Run the full regression suite and GitHub Actions. For later deployment acceptance, inspect a dry run before any live sync; no change to MINI-Ubuntu is part of this planning task.

**Exit criteria**

- All four reported release patterns resolve to their correct seasons, and their episodes no longer fall into `Season 00`.
- A valid season in the torrent title is used when filename parsing cannot provide a valid season, without depending on the adapter or external services.
- No partial-match regressions occur for existing valid filenames and season-only fallback cases.
- Jellyfin/Kodi outputs, manifests, playback URLs, and current NFO contracts remain compatible; all regression tests and CI pass.

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
- [x] Phase 8b — Bound Kodi path components; 36 CI tests passed, including Windows/SMB and UTF-8 length regressions
- [x] Phase 8c — Mini-PC clean rebuild/path acceptance and scheduled-operation verification: regenerated both output trees, bounded Prestige directory to 255 UTF-8 bytes, passed both path audits, enabled timer, and confirmed first timer-triggered run completed with 98 unchanged entries
- [x] Phase 8d — Publish the formal `v1.4.7` release/tag (published 2026-10-13; mini-PC acceptance passed 2026-10-14)
- [x] Phase 9 — Implement expanded quality-root evidence precedence (v1.4.8; CI passed, mini-PC acceptance pending)
- [ ] Phase 10 — Deferred: add backward-compatible orchestration/check/report interface for torr2jellyfin (not part of v1.4.8 by decision)
- [x] Phase 13 — Make Apprise collision warnings actionable with release titles, affected-file counts and representative source paths (v1.4.10 published and accepted on the mini-PC 2026-10-22; see delivery-verification note below)
- [x] Phase 14 — Decouple NFO content/schema versioning from the application release version. GitHub regression tests passed; v1.4.12 controlled live sync and the subsequent no-op run passed on the mini-PC on 2026-10-23. NFO format remains v3, and no application-version-only NFO rewrites or no-op metadata churn were observed.
- [x] Phase 15 — Stabilize canonical Kodi TV series title/year using all available inventory snapshots, without changing movie naming; preserve output for in-inventory torrents with transient quality-resolution failures and distinguish recovery from true source disappearance. GitHub regressions passed in v1.4.12; on 2026-10-23 the mini-PC dry run and live migration confirmed 10 episode STRMs moved to the canonical `Widow's Bay (2026)` path with identical contents, no planned movie-path removals, and a verified Kodi manifest. A second run was idempotent.
- [x] Phase 16 — Avoid redundant `chmod` on unchanged STRM/NFO files, output roots and manifest directories; preserve modes `0666`/`0777`, add permission-mode regressions, and perform controlled no-op sync/Jellyfin watcher acceptance. CI passed. On 2026-10-23, the second live run reported `created=0 updated=0 unchanged=304 failed=1`; 3,401 filesystem objects showed no path, mode, `mtime`, or `ctime` changes. Jellyfin logs for 12:10–12:39 showed only periodic Webhook notifier completions in the filtered output; the dashboard showed a full-library scan about four hours earlier, lasting about one minute. Accepted for the tested scenarios; periodic Webhook messages do not prove repeated media additions or a full-library scan.
- [x] Phase 17 — Implemented and published in v1.4.14; installation and first live-run verification completed on reference MINI-Ubuntu on 2026-10-27. The dry run and live sync exited 0; the first live run was idempotent (`created=0 updated=0 unchanged=794 failed=0`). `CATEGORY_FALLBACK` was observed in the dry run. The fully missing-category/prior-manifest branch was not exercised by current live inventory and remains regression-tested; another scheduled no-op run remains pending.
- [x] Phase 18 — Implemented in v1.4.13 and carried forward into installed v1.4.14. Apprise warning/critical routing was previously delivery-tested on the reference mini-PC on 2026-10-22; this upgrade preserved the existing notification configuration and state.
- [x] Phase 19 — Implemented and accepted in v1.4.15 on reference MINI-Ubuntu (2026-10-29). CI passed all 59 tests; the full dry-run and controlled live sync both passed. Live result: `torrents=635 quality_ready=635 quality_unresolved=0 created=0 updated=15 unchanged=1255 failed=0` in 311.28 seconds. The validator confirmed the corrected Black Mirror, Squid Game, and The Witcher season paths in both outputs, with no target `Season 00` paths; Kodi X-Files S03 and The Umbrella Academy S03 episode numbering also passed.
- [x] Phase 11 — Isolate temporary per-torrent metadata/FileStats failures while preserving existing output trees and synchronizing healthy torrents (v1.4.8; CI passed, mini-PC acceptance pending)
- [x] Phase 12 — Resolve Jellyfin STRM filename collisions without redesigning its directory tree (v1.4.9 implementation, CI and mini-PC acceptance completed 2026-10-22)

The v1.4.9 release is published and the collision fix is accepted on the reference mini-PC. v1.4.10 is also installed and was accepted on 2026-10-22. At that acceptance point, the live sync completed (`torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=154 unchanged=150 failed=1`); the one skipped item was the known Blu-ray ISO with no video files. The follow-up dry run reported `created=0 updated=0 unchanged=304 failed=1`, with no unresolved STRM collision, and the timer-triggered service run succeeded. On 2026-10-23, a later no-op run reported `torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=0 unchanged=304 failed=1`. Despite no STRM/NFO content updates (`mtime` remained older), sampled existing series/season directories and NFO files showed `ctime` changes clustered around that run, consistent with redundant `chmod` calls. Release v1.4.11 is published. GitHub Actions passed compilation and the full test suite, including regressions for Phases 14–16. v1.4.11 was superseded without live use by the accepted v1.4.12 correction on 2026-10-23. Current deployment and timer state are recorded in `project/CURRENT_STATE.md`. The service's configured TorrServer API is `127.0.0.1:8092`; a separate process also listens on 8097. Phase 12 preserves the existing Jellyfin directory-building behavior and changes only filenames when destination paths collide. Phase 10 remains intentionally deferred and is not a dependency of standalone torr2strm. Existing CLI, TOML configuration semantics, systemd oneshot service and timer remain available.


## 8. Collision-safe Jellyfin filenames (v1.4.9)

**Status: completed and accepted on the reference mini-PC on 2026-10-22.** CI passed (41 tests); the installed release passed compile/version/source checks, the initial dry run and live sync, a repeat dry run, and a timer-triggered service run.

### Decision

Do not redesign the existing Jellyfin TV folder-building algorithm in this change. Files that the current implementation places under Season NN remain there. The importer does not inspect DVD/Blu-ray navigation structures, infer episode identities, or try to repair the releaser's layout.

### Behavior

- Plan all per-torrent STRM destinations before writing any STRM/NFO, so a collision cannot be resolved by keeping the first file and silently skipping later files.
- If multiple files map to the same destination, add a deterministic identity suffix to every participant: short torrent hash plus the exact TorrServer file_id (the ID used in /play/{hash}/{file_id}, not the zero-based Elementum oindex).
- Re-check the complete planned destination set. If a collision remains, extend the hash portion to the full torrent hash while retaining the TorrServer file ID.
- Keep the final .strm basename at or below 255 UTF-8 bytes. Truncate only the readable source-name portion on UTF-8 boundaries when required; retain the stable identity suffix and preserve the source extension where possible. Paired .nfo names follow the resolved STRM basename.
- Leave all ordinary non-colliding Jellyfin filenames unchanged. Keep manifest schema v5 and NFO format v3; no clean rebuild or schema migration is required.
- If an impossible residual collision remains after the full-hash pass, fail the affected torrent visibly rather than omit a video file or write a misleading manifest.

### Regression and mini-PC acceptance

- Include a Lexx-like multi-DVD fixture with repeated VTS_01_0.VOB / VTS_01_1.VOB names under different DVD directories and a non-colliding control file.
- Assert that every playable input file has exactly one stable STRM and paired NFO, every STRM URL uses the corresponding TorrServer file_id, manifest records preserve every source_path, ordinary names remain unchanged, no component exceeds 255 bytes, and a repeated sync leaves paths unchanged.
- [x] On the mini-PC (2026-10-22): installed v1.4.9 with configuration, output roots and manifests preserved; compile and 41 tests passed; dry-run showed collision resolution and no unresolved-collision guard errors; real sync completed; all 22 colliding Lexx STRM entries exist with unique filenames; a subsequent dry run reported 304 unchanged outputs; timer-triggered service run completed successfully and `torr2strm.timer` was restored to `active`/`enabled`.

## 9. Informative Apprise collision summaries (v1.4.10)

**Status: accepted on the reference mini-PC on 2026-10-22.**

**Reason**

The first live Apprise delivery on the mini-PC contained 22 `STRM_COLLISION_RESOLVED` warning bullets for the Lexx multi-DVD release, but those technical records did not include its release title. The separate `QUALITY_SKIP` critical message was easier to act on because it already contained the title.

**Behavior**

- Emit one `STRM_COLLISION_SUMMARY` warning for each torrent/output whose collision planner resolved one or more destination collisions. Include the release title, full torrent hash, affected-file count, count using short-hash suffixes, count escalated to the full hash, and representative source paths/file IDs.
- Retain the existing per-file `STRM_COLLISION_RESOLVED` and `STRM_COLLISION_HASH_EXTENDED` warnings in the local journal so troubleshooting still has exact source and suffix detail.
- Exclude those repetitive per-file records from Apprise aggregation when the summary is available; do not lose detailed diagnostics from systemd/journal logs.
- Include the release title in `STRM_COLLISION_UNRESOLVED` critical logs as well.
- Add regressions that verify the summary derives from real collision planning, carries the release title and example paths, and is what Apprise receives instead of 22 separate resolution bullets.
- Keep notification fingerprint suppression, warning/error tags and the HTTP gateway contract unchanged. No media-tree, manifest or NFO schema change is required.

**Acceptance**

- [x] CI passed all 41 tests, and the official v1.4.10 release archive plus SHA256SUMS were published on 2026-10-22.
- [x] On the mini-PC, the v1.4.10 dry run emitted the title-bearing `STRM_COLLISION_SUMMARY` for Lexx with 22 affected files and representative source paths; the detailed 22 per-file entries remained in the local log.
- [x] A direct test through the same Apprise gateway/config and `warnings` tag reached Telegram. The run log also recorded `APPRISE_SENT tag=warnings status=200`. The specific collision-summary message was not visually confirmed in Telegram during this check; the user accepted v1.4.10 and chose to proceed.
- [x] Notification fingerprint suppression remains configured as before; no notification config or state was reset as part of this release.

### Follow-up discovered during v1.4.10 acceptance

The post-install dry run returned `created=0 updated=152 unchanged=152 failed=1` and logged `NFO_UPDATE` for 152 items because the generated-file header embedded application `VERSION`. In candidate v1.4.11, generated NFO headers omit that software version; the comparator also treats the legacy version token as non-semantic while preserving comparison of actual content and `NFO_FORMAT_VERSION`. Automated regression tests cover both an app-only bump and an intentional NFO format-version change; CI passed. Controlled reference mini-PC acceptance is pending.

## 10. v1.4.14 reference deployment acceptance — 2026-10-27

- [x] Installed the official v1.4.14 release on MINI-Ubuntu; verified the official archive checksum, `--version`, source-file equality, compilation, and all 56 regression tests. Preserved `/etc/torr2strm/config.toml`, both manifests, and both output roots.
- [x] Installed-code dry run and first live sync completed successfully. Both reported `torrents=397 quality_ready=397 quality_unresolved=0 created=0 updated=0 unchanged=794 failed=0`; the live service returned exit status 0. A category fallback from `action=list` was observed during the dry run. The fully missing category with a trusted previous manifest fallback/defer path was not exercised by current inventory and remains regression-tested.
- [x] Reviewed the timer evidence. `NEXT=-` and `NextElapseUSecMonotonic=infinity` were observed while the Type=oneshot service was still in `activating` state; no post-completion snapshot of the old timer was captured, so a re-arming defect was not established. Restored the monotonic schedule (`OnBootSec=2min`, `OnUnitActiveSec=5min`) with a 15-second random delay and one-second accuracy on both the repository template and reference host. `systemd-analyze verify` passed.
- [x] Applied and verified the monotonic timer restoration on MINI-Ubuntu. The service completed at 22:42:57 +05 with exit status 0; `torr2strm.timer` was active/enabled and the next trigger was scheduled for 22:45:20 +05.
- The timer-template reversion is merged on main and the original monotonic schedule is restored on MINI-Ubuntu. The published v1.4.14 tag/archive already contains this timer schedule; no new application release is needed to restore it on the host. This is an operational scheduling choice, not a confirmed fix for a proven defect. No Python app version, manifest schema v5, or NFO format v3 change is made.

## 11. Season and episode parser reliability — Phase 19 (v1.4.15, released and accepted 2026-10-29)

The implementation is published and installed on reference MINI-Ubuntu. Automated CI, the full reference-host dry-run, the controlled live sync, and explicit post-sync path/episode validation passed. The live sync exited successfully after 311.28 seconds with zero failed torrents.

### Evidence

- The installed parser's coordinate function combines `file.path` and `snap.title` into one string, then tests regular expressions over the combined text. The file path occurs first, so a bad match in the filename can prevent a valid season marker in the TorrServer title from being considered.
- The current full-coordinate pattern requires a word boundary after the episode digits. This rejects valid patterns where the digits are followed by underscore, for example `S03E01_Dub_...` and `S02E01_RHS_...`.
- The season-only fallback `S(\d{1,2})(?!\s*E\d)` can backtrack and accept only the first digit of the season, producing season 0 from `S03E01` or `S02E01`.
- On 2026-10-29, local logs showed `SEASON_FALLBACK ... season=00` for 6 Black Mirror S03 files, 6 Black Mirror S04 files, 7 Squid Game S02 files, and 6 Squid Game S03 files. Corresponding `CREATE` records showed their Jellyfin and Kodi outputs under `Season 00`.
- A dotted control filename, `Lucifer.S02E01.1080p.WEB-DL.5xRus.Eng.mkv`, parsed as season 2, episode 1 and must remain valid.

### Implementation decision to preserve

The torrent title is an independent fallback source, not a replacement for filename parsing and not an adapter dependency. Parse each source separately. Prefer complete, valid episode coordinates from the file path. If the path has an explicit season but no episode, keep that season and leave the episode unknown; do not combine it with a title-level episode number. Only when the path provides no valid season may complete coordinates or a season marker from the TorrServer title be used. Recognize underscore-delimited coordinates such as `S03E01_Dub` and `S02E01_RHS`, reject partial digit matches such as `S0`, and keep deterministic precedence when valid evidence conflicts. If neither source gives a valid season, retain the existing `Season 00` fallback.

### Acceptance constraints

- Add regressions for underscore-delimited coordinates, partial/adjacent digits, valid dotted controls, title-only season fallback, path/title conflicts, and path season-only cases. Verify season/episode extraction itself as well as the planned paths and Jellyfin NFO coordinates/Kodi episode names.
- Keep all parsing local to TorrServer-provided title/path data. Do not add dependencies on the adapter, tracker names, remote metadata providers, or new HTTP calls.
- Preserve current missing-season semantics, the Jellyfin grouping algorithm, Kodi/Elementum playback contract, manifest behavior, and NFO format unless testing establishes a separate necessary change.
- The v1.4.15 full dry-run on MINI-Ubuntu completed without writing output files or manifests, but it did not report planned Jellyfin removals. The code path logs Jellyfin STRM/NFO removals only when not in dry-run mode, so the output did not expose those planned operations for review. This is a known dry-run observability gap; it does not invalidate the parser checks, but leaves the Jellyfin removal plan less complete than the Kodi plan.
- Live deployment acceptance was completed on 2026-10-29: the controlled sync returned exit status 0 and `failed=0`; both output manifests and the corrected season paths passed post-sync validation. A separate second no-op/idempotency run has not been included in this acceptance record.

### Post-release follow-up — expose planned Jellyfin removals in dry-run (non-blocking for v1.4.15)

During the full reference-host dry-run on 2026-10-29, the Kodi reconciler logged its planned removal of old managed STRM paths, while the Jellyfin reconciler did not log planned STRM/NFO removals because those log statements are currently inside the non-dry-run branch.

- [ ] Improve dry-run logging so it reports the Jellyfin STRM and NFO removals that a live sync would perform, without performing those removals.
- [ ] Add regression tests asserting that dry-run emits the planned Jellyfin removal events and does not mutate files or manifests.
- [ ] Use the complete dry-run plan (including removals for both outputs) as a pre-live review artifact for a future release that needs the improved auditability.

This follow-up is intentionally **non-blocking for v1.4.15**: the current candidate passed CI and the full reference-host dry-run, the parser/path results were reviewed, and the dry-run did not write output trees or manifests. Complete Jellyfin removal visibility before a future release that needs the improved auditability; do not silently present the current dry-run log as a complete inventory of planned Jellyfin operations.

