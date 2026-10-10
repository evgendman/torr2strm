# torr2strm v1.4.5

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

The two quality roots are 4K and 1080p. 4K is used only when usable ffprobe data already embedded in TorrServer torrent metadata reports a video dimension of at least 3840 pixels. If ffprobe is absent or does not prove 4K, the root is 1080p. Structured quality fields and title markers already stored in TorrServer data may supply a visible basename label, but never promote a torrent into 4K. Cached NFOs may preserve technical stream details for the exact source file, but do not determine category, root, or display label.

torr2strm does not call JacRed, Prowlarr, /ffp/, or any other metadata/probing service. It does not run local ffprobe. A 1080p root does not claim every release in it is actually 1080p.

Kodi remains independent from Jellyfin's existing tree. Identified releases share canonical logical folders only when trusted movie/series IDs already exist in TorrServer metadata. Unknown IDs are never fabricated and title similarity never merges torrents.

## Magnet handling for Kodi

TorrServer's standard status may not expose the original magnet link. The service uses a magnet URI already present in the TorrServer torrent's custom data. If none is available, it constructs a BTIH magnet from the TorrServer hash and title. No external lookup is performed. The URI is URL-encoded as Elementum's uri parameter.

For TV, oindex is the zero-based original FileStats order, not the one-based TorrServer /play file ID and not the sorted path order.

## NFO responsibility boundary

NFOs contain only reliable identifiers and technical stream data. They deliberately do not contain human-readable display names: Kodi was observed to replace its localized scraper title with the NFO title after scraping, and Jellyfin should retrieve localized names from provider IDs.

- **Movie sidecar NFO:** trusted provider IDs, stream details only when already available in a matching release ffprobe payload or cached NFO, and a Kodi Combination NFO scraper URL when a trustworthy TMDb ID is available.
- **Series-root `tvshow.nfo`:** trusted series IDs, a Combination NFO scraper URL when a trustworthy TMDb ID is available, and `fileinfo/streamdetails` from a deterministic representative release only when those details were already available. No display title is written.
- **Episode sidecar NFO:** ordinary `episodedetails` format, trusted IDs and known season/episode coordinates; include stream details only from an already-available matching source or cached NFO. No Combination NFO URL.
- **No NFO type writes** `title`, `originaltitle`, `sorttitle`, `showtitle`, `name`, year/date display metadata, or any other human-facing title field.
- Any pre-existing per-file ffprobe details remain tied to their own STRM/NFO. torr2strm does not run additional per-file probes; where data is unavailable, the NFO remains identity-only.

Useful video/audio/subtitle stream fields include codec, bitrate, dimensions, aspect ratio, frame rate, scan type, bit depth, HDR type, stereomode, audio language/channels/sampling rate, and subtitle codec/language/title flags when available.

A valid reusable NFO may be copied between output trees for the same source file. No replacement ffprobe call is made when it is missing. Jellyfin may read the stream details but does not necessarily use them as its effective stream information; their retention is still useful to Kodi and other consumers.

### NFO format and clean rebuild

NFO format version is independent from the software and manifest versions. Version 3 uses the `<torr2strm formatversion="3" />` marker, title-free metadata, and the Combination NFO URL after the XML root for movie and `tvshow.nfo` files where a TMDb ID is known. Episode NFOs remain ordinary XML without a trailing scraper URL.

The output trees are still under development and are intentionally rebuilt cleanly for v1.4.0; no directory-tree migration is performed. The parser still strips title fields if it upgrades an older cached NFO, but operators should follow the clean-reset procedure below instead of relying on legacy NFOs.

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
- Legacy `[quality]` timeout/retry settings and old `[jacred]` sections are ignored in v1.4.5; they may be removed from the config file.
- No external indexer/provider configuration exists in v1.4.5. Older `[jacred]` sections are ignored and may be removed from `/etc/torr2strm/config.toml`.
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

Recoverable media-information failures no longer prevent the STRM from being materialized if quality can be determined from structured metadata/title, or defaulted to the `1080p` root. An identity-only NFO is written if per-file ffprobe data is unavailable. Fatal configuration/source errors still exit non-zero.

## Upgrade and deployment

Software version: v1.4.5 candidate; manifest format v5; NFO format v3. No release tag has been created yet, and the candidate must pass the mini-PC dry-run before being treated as production-approved.

For an in-place upgrade, stop/disable the timer, back up the application script and configuration file, then run the repository installer. Preserve both configured media roots and their .torr2strm state. Do not clear or recreate either output tree as part of this upgrade. The installer leaves an already-disabled timer disabled; perform a manual --dry-run first and review the logs before any real sync.
