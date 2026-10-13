# torr2strm v1.4.7

TorrServer -> multiple materialized STRM/NFO trees.

Development plan: [ROADMAP.md](ROADMAP.md).

TorrServer is the sole source of truth. The service reads its category, metadata and FileStats, then materializes independent Jellyfin and Kodi/Elementum output trees. No external metadata provider is queried.

## Output model

Two output trees are supported now:

### Jellyfin output

- `movie`: one `.strm` per playable video file.
- `tv`: one `.strm` per playable video file under `Season NN`.
- Playback URL is TorrServer `/play/{hash}/{file_id}`.
- This is the authoritative output and the only output allowed to reverse-delete the source torrent.

Default root:

```text
/mnt/torr2strm-media/
├── movie/
│   ├── 1080p/
│   └── 4K/
└── tv/
    ├── 1080p/
    └── 4K/
```

### Kodi/Elementum output

- `movie`: one torrent-level `.strm` per torrent; no `oindex`.
- `tv`: one `.strm` per playable video file, with the original zero-based FileStats order passed as `oindex`.
- Playback URI uses `plugin://plugin.video.elementum/play?uri=<url-encoded-full-magnet>`.
- The Kodi output is read-only: deleting a Kodi STRM never removes the TorrServer source.
- Kodi path components are sanitized for Windows/SMB clients: forbidden punctuation and reserved device names are handled while `.strm`/`.nfo` extensions and readable names are preserved. Jellyfin's historical path naming is unchanged.

Default root:

```text
/mnt/torr2strm-media-kodi/
├── movie/
│   ├── 1080p/
│   └── 4K/
└── tv/
    ├── 1080p/
    └── 4K/
```

Each output has its own:

```text
.torr2strm/manifest.json
.torr2strm/root.marker
```

Manifest state is never copied between roots.

## Category and quality roots

Category is taken only from the category field already present in TorrServer. Supported movie and tv values are preserved. Missing, blank, or unsupported values—including anime—go to _uncategorized. Category is never guessed from title, filename, or external lookup.

The two quality roots are `4K` and `1080p`. Usable video dimensions already embedded in TorrServer metadata take priority: a maximum dimension of at least 3840 pixels selects `4K`, while lower usable dimensions select `1080p`. Only when usable dimensions are unavailable may a clear `4K` or `2160p` marker in the release title select the `4K` root. Title markers never override usable non-4K dimensions. Structured quality fields do not select the root. Cached NFOs may preserve stream details for an exact source file, but do not determine category, root, or display label.

torr2strm does not call JacRed, Prowlarr, /ffp/, or any other metadata/probing service. It does not run local ffprobe. A 1080p root does not claim every release in it is actually 1080p.

Kodi remains independent from Jellyfin's existing tree. Identified releases share canonical logical folders only when trusted movie/series IDs already exist in TorrServer metadata. Unknown IDs are never fabricated and title similarity never merges torrents.

## Magnet handling for Kodi

TorrServer's standard status may not expose the original magnet link. The service uses a magnet URI already present in the TorrServer torrent's custom data. If none is available, it constructs a BTIH magnet from the TorrServer hash and title. No external lookup is performed. The URI is URL-encoded as Elementum's uri parameter.

For TV, oindex is the zero-based original FileStats order, not the one-based TorrServer /play file ID and not the sorted path order.

## NFO responsibility boundary

NFOs contain only reliable identifiers and technical stream data. They deliberately do not contain human-readable display names: Kodi was observed to replace its localized scraper title with the NFO title after scraping, and Jellyfin should retrieve localized names from provider IDs.

- **Movie sidecar NFO:** trusted provider IDs, stream details only when already available in a matching release ffprobe payload or cached NFO, and a Kodi Combination NFO scraper URL when a trustworthy TMDb ID is available.
- **Series-root `tvshow.nfo`:** trusted series IDs, a Combination NFO scraper URL when a trustworthy TMDb ID is available, and `fileinfo/streamdetails` from a deterministic representative release only when those details were already available. No display title is written.
- **Kodi TV episode sidecars are not generated.** This leaves episode STRMs eligible for Kodi's internet scraper rather than pinning metadata through local `episodedetails` NFOs. A `tvshow.nfo` is generated only in the root of an identified series; no NFO is emitted in season folders.
- **Jellyfin episode NFO behavior is unchanged.** Jellyfin continues to receive its existing per-file NFO output, including season/episode coordinates and technical details when already available.
- **No generated NFO writes** `title`, `originaltitle`, `sorttitle`, `showtitle`, `name`, year/date display metadata, or any other human-facing title field. Kodi movie NFOs continue to use trusted IDs and available streamdetails plus a Combination URL when a trustworthy TMDb ID exists.

Useful video/audio/subtitle stream fields include codec, bitrate, dimensions, aspect ratio, frame rate, scan type, bit depth, HDR type, stereomode, audio language/channels/sampling rate, and subtitle codec/language/title flags when available.

A valid reusable NFO may be copied between output trees for the same source file. No replacement ffprobe call is made when it is missing. Jellyfin may read the stream details but does not necessarily use them as its effective stream information; their retention is still useful to Kodi and other consumers.

### NFO format and clean rebuild

NFO format version 3 remains independent from software and manifest versions. Kodi movie NFOs and series-root `tvshow.nfo` files use the `<torr2strm formatversion="3" />` marker and a Combination NFO URL after the XML root when a trustworthy TMDb ID is known. Kodi TV episode sidecar NFOs are not generated; Jellyfin's existing episode NFOs remain ordinary XML.

The v1.4.7 tree-layout/NFO policy change is deployed with a clean rebuild rather than a directory-tree migration. Stop the service/timer and clear only the two configured output-root contents (including each `.torr2strm` manifest and root marker) before the first real sync. Keep `/etc/torr2strm/config.toml`; do not clear TorrServer data or any media/cache directories outside the configured output roots.

## Reverse deletion

Source removal direction is one-way and always applies to all enabled outputs:

```text
TorrServer torrent removed
        ↓
next sync
        ↓
reflection removed from Jellyfin + Kodi trees
```

Only Jellyfin is authoritative in the reverse direction:

```text
Jellyfin managed STRM deleted by user
        ↓
if remove_torrent_on_strm_delete = true
        ↓
TorrServer action=rem
        ↓
all output reflections are removed
```

Kodi is read-only and never performs reverse deletion.

`action=drop` is not used or implemented.

## Configuration and command-line options

The production configuration is `/etc/torr2strm/config.toml`; the repository ships `etc/config.toml.example`. The full parameter reference, defaults and examples are in [docs/configuration.md](docs/configuration.md).

Main sections:

- `[torrserver]`: TorrServer base URL and API/metadata timeouts.
- `[outputs.jellyfin]` and `[outputs.kodi]`: output roots, independent manifests and enable flags. Only Jellyfin can enable reverse deletion.
- `[sync]`: eligible video extensions and the fallback season for TV files whose season cannot be inferred.
- Legacy `[quality]` timeout/retry settings and old `[jacred]` sections are ignored in v1.4.7; they may be removed from the config file.
- No external indexer/provider configuration exists in v1.4.7. Older `[jacred]` sections are ignored and may be removed from `/etc/torr2strm/config.toml`.
- `[logging]`: log verbosity.

Command-line parameters (there are no external-source override flags):

```text
--config PATH              Use a configuration file other than /etc/torr2strm/config.toml
--dry-run                  Log planned output changes without writing/deleting output files or removing torrents
--version                  Print the installed version and exit
```


Example:

```bash
sudo /usr/bin/python3 /opt/torr2strm/torr2strm.py --config /etc/torr2strm/config.toml --dry-run
```

## Service behavior

The service is a systemd oneshot triggered by `torr2strm.timer`.

Recoverable media-information failures no longer prevent STRMs from being materialized. Quality uses available embedded dimensions first, then the explicit `4K`/`2160p` title fallback only when dimensions are unavailable, and otherwise defaults to `1080p`. When technical details are unavailable, Jellyfin and Kodi movie NFOs may be identity-only; Kodi TV episode sidecars are not created. Fatal configuration/source errors still exit non-zero.

## Upgrade and deployment

Software version: v1.4.7 candidate; manifest format v5; NFO format v3. GitHub Actions passed all 33 tests. Mini-PC acceptance—clean rebuild, Kodi scraper discovery, Elementum playback, and shared-series reconciliation—remains pending before publishing a formal `v1.4.7` release tag.

For v1.4.7, back up `/opt/torr2strm/torr2strm.py` and `/etc/torr2strm/config.toml`, stop the timer/service, and clean the contents of the configured Jellyfin and Kodi output roots because their NFO/tree policies are changing. Keep the config and the TorrServer source inventory. Run `install.sh`, verify the installed version, perform a manual dry-run and inspect the log before the first real synchronization. Start the timer only after Kodi scraping and Elementum playback have been checked.
