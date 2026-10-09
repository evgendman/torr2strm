# torr2strm Development Roadmap

> **Status: planning only.** This document records the agreed direction for the next development stages. It does not implement or authorize code changes by itself.
> Baseline: repository release `v1.3.2`.
> Scope: STRM/NFO tree generation for Jellyfin and Kodi/Elementum. The separate `hotcached` project is out of scope.

## 1. Goal

Keep Jellyfin's existing directory-building behavior intact while adding a normalized, human-readable Kodi tree that groups all identified releases of the same series into one series directory per quality root. Preserve torrent identity, technical media information, independent output manifests, and safe synchronization.

## 2. Current baseline (`v1.3.2`)

### Jellyfin output

- Jellyfin is the authoritative output and the only output that can initiate reverse deletion of a TorrServer torrent.
- It creates file-level STRM entries for playable video files, using TorrServer `/play/{hash}/{file_id}` URLs.
- Reverse deletion is opt-in and uses `action=rem` only. `action=drop` is not used.
- Jellyfin and Kodi have independent output roots, root markers, and manifests.

### Kodi/Elementum output

- Kodi output is currently read-only: removing a Kodi STRM must never remove the source torrent.
- Movies currently use a torrent-level STRM and do not use `oindex`.
- TV output currently creates one STRM per playable file and passes the original zero-based FileStats order as Elementum `oindex`.
- Playback uses an Elementum plugin URI. The tree-layout work must not accidentally break this playback contract.

### Shared media information

- TorrServer remains the source of truth for torrent inventory, BTIH/info hash, and FileStats.
- JacRed is optional enrichment and a match is accepted only when its hash exactly matches the TorrServer torrent.
- The current implementation obtains ffprobe JSON through TorrServer `/ffp/{hash}/{file_id}`; it does not run a local ffprobe process.
- The current quality root has only two values: `4K` and `1080p` (everything else). It is presently determined from the primary eligible video's real dimensions.
- NFO format version 2 includes technical stream details and a Combination NFO URL for movie NFOs and `tvshow.nfo`. The current implementation reuses/copies NFO data across outputs.

## 3. Target invariants

These are the constraints that all later implementation stages must preserve.

1. **Jellyfin directory layout is not being redesigned.** Its existing folder-building/grouping behavior remains the compatibility baseline. The Kodi tree is developed independently.
2. **Only two quality roots exist:** `4K` and `1080p`. `1080p` means every release not classified into the `4K` root; it does not claim that every contained file is actually 1080p.
3. **Root and display quality are separate values.** `quality_root` chooses `4K` or `1080p`; `quality_label` describes the actual media item, for example `480p`, `720p`, `1080p`, or `2160p HDR` when those properties are known.
4. **Quality is resolved before the output path and filename are constructed.** The resolved values are reused throughout path construction; the name builder must not independently reclassify quality.
5. **One Kodi series directory per logical series per quality root.** The display name is based on the series name and year. A torrent hash is not routinely added to the series-directory name.
6. **Multiple releases remain separate items.** Two torrents representing the same episode must not overwrite or collapse into one STRM. A short hash is included in each release's basename for human-visible disambiguation; the full torrent hash remains in the manifest.
7. **Kodi episode names expose both identity and quality.** The basename includes the readable series/episode name, `SxxEyy`, a short hash, and the final human-readable quality label when known.
8. **Kodi movie items are grouped under a readable movie name/year directory.** Each distinct release remains independently addressable and includes a short hash and quality label in its item name.
9. **Identifiers belong in metadata, not routinely in display names.** Trusted movie/series/episode identifiers should be written to NFO where applicable; the series directory remains human-readable.
10. **NFO must retain useful ffprobe-derived stream details.** Do not remove technical fields simply because their interpretation differs between Kodi and Jellyfin.
11. **Do not force display titles unnecessarily.** Avoid writing a title field when it would override a scraper/provider's localized title and the field is not required for identification or matching.
12. **Movie and series NFOs may use Combination NFO; episode NFOs remain ordinary episode NFOs.** The exact field set and the real Jellyfin behavior must be validated before finalizing the implementation.
13. **Unknown items use a safe fallback.** If there is not enough trustworthy information to identify a movie or series, preserve the source torrent's hierarchy as closely as practical. Do not invent IDs or guess movie-versus-TV from a title. `_uncategorized` remains meaningful.
14. **The manifest, not filenames, is the source of truth for reconciliation and deletion.** It maps physical paths to the full torrent hash and file identity.
15. **Safety boundaries remain unchanged.** Kodi is read-only; only the explicitly enabled Jellyfin reverse-delete path may call TorrServer `action=rem`; never implement `action=drop`.

## 4. Development phases

Phases are ordered by dependency. Each phase must meet its exit criteria before the next phase is considered complete.

### Phase 0 — Freeze the behavior contract

**Work**

- Record representative current Jellyfin and Kodi output trees as regression fixtures before changing path generation.
- Write down the exact target layouts for identified TV, identified movies, and unidentified torrent fallback cases.
- Define the short-hash presentation length and collision behavior. The full hash must always be retained in the manifest.
- Confirm how a genuine directory-name collision between different series IDs is handled without routinely adding hashes to series directory names.
- Reconcile one discrepancy between the target specification and the current `v1.3.2` behavior: the target text allows a release-quality fallback when ffprobe is unavailable, while current project documentation explicitly rejects title-based quality inference. If a fallback is accepted, it must use a trustworthy structured field; do not parse free-form title tokens as a silent substitute for ffprobe.
- Define the display behavior when the actual per-item quality cannot be determined (for example, omit the label or use an explicit unknown label). Never silently label an unknown item `1080p` merely because its root is `1080p`.

**Exit criteria**

- Target examples and edge cases are documented.
- Quality fallback and unknown-quality display rules are explicit.
- The Jellyfin baseline and Kodi playback contract are captured for regression testing.

### Phase 1 — Separate quality-root from per-item quality

**Work**

- Keep the existing two-value root classification: `4K` or `1080p`.
- Introduce a separately resolved per-item label based on the best available, trustworthy technical stream data, including resolution and HDR information when available.
- Preserve the current primary-eligible-video rule for the root unless testing identifies a specific correctness problem.
- Resolve the quality label for the actual movie/episode file rather than copying the root name into every filename.
- Make the priority and fallback rules explicit and testable; never infer quality from a torrent title alone.

**Exit criteria**

- A release classified into the `1080p` root can correctly carry a `480p`, `720p`, or `1080p` label.
- HDR is only shown when supported by usable metadata.
- The same resolved quality object is used for the root, basename, and any metadata decisions.

### Phase 2 — Validate and finalize the NFO contract

**Work**

- Preserve ffprobe-derived `<fileinfo>/<streamdetails>` data, including usable video, audio, and subtitle properties.
- Keep trusted provider identifiers in the relevant movie, series, or episode NFO.
- Use Combination NFO for movie/series identification where appropriate; keep episode NFO in ordinary episode format.
- Review title fields so that NFO does not unnecessarily lock the media item to a language-specific title.
- Test the existing NFO format against Kodi and Jellyfin independently. Kodi has already shown that it can interpret the current ffprobe NFO; Jellyfin's actual consumption of these fields remains to be verified.
- On a real Jellyfin test library, inspect which stream details are accepted from NFO for `.strm` items and which are independently probed or ignored.
- Decide whether one shared NFO representation is sufficient or whether small output-specific differences are required. Do not introduce separate formats unless testing demonstrates a need.
- Define any NFO format-version change and migration behavior before code is written.

**Exit criteria**

- Fixture NFOs are accepted by Kodi and Jellyfin without malformed XML or unexpected title overrides.
- The supported/ignored ffprobe fields are documented from observed behavior, not assumed from XML presence alone.
- Existing NFO cache reuse and current version-2 migration behavior remain covered by tests.

### Phase 3 — Define logical identity and Kodi series grouping

**Work**

- Resolve the logical movie or series identity using trustworthy metadata and provider IDs, preferably from an exact torrent-hash match where JacRed enrichment is involved.
- Use a stable logical series identity to group releases whose source torrent names or internal folder structures differ.
- Within each Kodi quality root, materialize one `<Show Name> (<Year>)` directory for the logical series, with `Season NN` subdirectories and a `tvshow.nfo`.
- Do not add a release hash to the normal series-directory name. IDs belong in NFO, not in routine display names.
- Put each identified episode release into the common series/season directory with a basename containing `SxxEyy`, short torrent hash, and the quality label.
- Group movies by readable title/year and keep each release as a separate physical item.
- Preserve the original torrent tree for cases without sufficient identity metadata instead of guessing or inventing IDs.

**Exit criteria**

- Different releases with the same trusted series ID converge on one Kodi series folder within a quality root.
- Different episodes and multiple releases of the same episode are distinguishable.
- Unknown items follow the documented fallback and do not get incorrectly normalized.

### Phase 4 — Implement the Kodi tree builder as an independent output

**Work**

- Separate Kodi path planning from Jellyfin path generation; do not rewrite Jellyfin's directory-grouping rules as part of this change.
- Build the normalized Kodi TV tree: `tv/<quality-root>/<series> (year)/Season NN/`.
- Build the normalized Kodi movie tree: `movie/<quality-root>/<movie> (year)/`.
- Write the `.strm` and corresponding sidecar `.nfo` for each managed release item.
- Keep Elementum playback URIs intact. For TV, preserve the original zero-based FileStats order for `oindex`; do not substitute a sorted filename index or TorrServer `/play` file ID.
- Retain the current torrent-level Kodi movie playback model unless a specific tested requirement proves it incompatible with the target tree.
- Use deterministic path sanitization and filename-length handling. The short hash is a presentation aid; lookup and lifecycle decisions use the full manifest identity.

**Exit criteria**

- Repeated syncs produce deterministic paths without duplicate folders or overwritten release files.
- The Kodi series grouping and quality suffixes match the approved examples.
- Kodi playback URIs and TV `oindex` semantics are unchanged.
- Jellyfin output paths and grouping pass their regression fixtures unchanged.

### Phase 5 — Manifest, reconciliation, and safe transition

**Work**

- Keep independent manifests and root markers for Jellyfin and Kodi.
- Record enough data to map each managed Kodi path to its full torrent hash and original file identity; never recover source identity by parsing the visible basename.
- Determine whether the Kodi layout change requires a manifest-version bump or an explicit migration. Do not mix old and new path models silently.
- Provide a safe transition procedure: dry-run first, back up the current Kodi output/manifest, then perform a controlled one-time rebuild or documented migration.
- Ensure stale old Kodi paths can be cleaned without invoking any source-torrent removal.
- Keep Jellyfin reverse-delete detection safe. Program-generated path changes must never be mistaken for user deletion of a managed Jellyfin STRM.
- Preserve the rule that source torrents disappearing from TorrServer are reflected in all enabled output trees on the next sync.

**Exit criteria**

- Migration/rebuild is idempotent and does not create duplicate managed entries.
- Removing a Kodi STRM has no source-side effect.
- Only an explicitly enabled deletion of a tracked Jellyfin STRM can trigger `action=rem`.
- No code path invokes `action=drop`.

### Phase 6 — Automated tests and integration fixtures

**Work**

- Add unit fixtures for quality-root versus quality-label, including 480p/720p/1080p and HDR examples.
- Test grouping of the same series across several torrents and different source directory trees.
- Test multiple releases for the same episode, distinct episodes, multiple movies/releases, path collisions, long Unicode names, and safe filename sanitization.
- Test missing ffprobe data, exact versus non-matching JacRed hashes, missing provider IDs, and the unidentified-torrent fallback.
- Test NFO generation/parsing, title omission rules, stream-detail preservation, and NFO-version migration.
- Test independent manifests, idempotent reconciliation, dry-run behavior, and deletion direction/safety.
- Add regression tests asserting Jellyfin's existing paths, output semantics, and reverse-delete safeguards remain unchanged.

**Exit criteria**

- All unit and integration tests pass.
- No test permits approximate JacRed title matches to override torrent identity.
- No test permits Kodi-side source deletion or `action=drop`.

### Phase 7 — Real-player validation, documentation, and release

**Work**

- Test the new Kodi tree on the mini-PC using an isolated/test root first. Rescan the library and verify that each identified series appears as one series entry per quality root.
- Confirm multiple releases of the same episode remain separately visible and their quality labels can be read without opening technical details.
- Confirm Kodi obtains localized display names from the configured scraper/provider when NFO does not force a title.
- Verify that STRM playback still opens the intended Elementum torrent/file.
- Validate the real Jellyfin scan against the baseline and investigate the NFO stream-detail behavior observed in Phase 2.
- Update README, architecture, NFO/configuration references, migration instructions, and CHANGELOG as warranted by the final implementation.
- Bump software/manifest/NFO format versions only when the associated code and migration are ready; do not change versions for this planning-only commit.

**Exit criteria**

- Kodi, Jellyfin, metadata, and reverse-delete checks pass on the real mini-PC.
- Upgrade/rebuild instructions are complete and have been followed successfully on a test root.
- Release notes distinguish changed Kodi tree behavior from unchanged Jellyfin behavior.

## 5. Explicit non-goals

- Do not redesign Jellyfin's existing directory-grouping behavior as part of Kodi normalization.
- Do not add more quality-root categories: the only roots remain `4K` and `1080p`.
- Do not merge different torrent releases into one STRM or discard a release because another release maps to the same episode.
- Do not run/install local ffprobe from torr2strm; technical probing continues through TorrServer's ffprobe endpoint.
- Do not use title similarity as proof of torrent identity.
- Do not make Kodi output authoritative for deletion.
- Do not implement or use TorrServer `action=drop`.
- Do not include `hotcached` work in this roadmap.

## 6. Definition of done

The work is complete only when the Kodi tree is normalized and human-readable; each series is represented by one directory per quality root; per-release quality and short-hash labels are visible; NFO retains verified technical data and trustworthy IDs; unidentified content remains safe; manifests remain authoritative; synchronization is deterministic; real Kodi and Jellyfin behavior has been checked; and all Jellyfin path/grouping and deletion-safety invariants remain intact.

## 7. Current status

- [ ] Phase 0 — Freeze the behavior contract
- [ ] Phase 1 — Separate quality-root from per-item quality
- [ ] Phase 2 — Validate and finalize the NFO contract
- [ ] Phase 3 — Define logical identity and Kodi series grouping
- [ ] Phase 4 — Implement the Kodi tree builder
- [ ] Phase 5 — Manifest, reconciliation, and safe transition
- [ ] Phase 6 — Automated tests and integration fixtures
- [ ] Phase 7 — Real-player validation, documentation, and release

This roadmap is a planning artifact only. No source code, runtime behavior, configuration defaults, or version numbers are changed by adding it.
