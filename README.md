# torr2strm v1.4.0

TorrServer -> multiple materialized STRM/NFO trees.

Development plan: [ROADMAP.md](ROADMAP.md).

TorrServer is the source of truth. The service reads TorrServer FileStats, resolves category and real media information, then materializes independent output trees.

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

## Category resolution

Category controls the first directory level of each output tree: `movie`, `tv` or `_uncategorized`.

Resolution order:

1. A non-empty TorrServer category that normalizes to `movie` or `tv` wins.
2. If the TorrServer category is genuinely blank, torr2strm may use the category from an exact JacRed BTIH/infoHash match.
3. If no usable category is found, the torrent stays under `_uncategorized`.

Important details:

- JacRed movie categories map to `movie`; TV/series categories map to `tv`.
- Anime is deliberately kept in `_uncategorized`; it is never silently treated as TV.
- A non-empty TorrServer category other than `movie` or `tv` is treated as unknown and remains `_uncategorized`. Because the source field was non-empty, JacRed does not override it.
- The program never guesses movie versus TV from title text.

The second directory level is a fixed two-way split: `4K` when the primary eligible video stream has a maximum dimension of at least 2160 pixels; otherwise `1080p`. This is the torrent-level quality root and does not claim that every file is 1080p.

Kodi's normalized tree is separate from Jellyfin's existing tree. Identified releases of one movie/series share a canonical logical directory under each quality root. Identified TV episodes are placed under `Season NN`; unidentified series keep the torrent release-title directory and original internal file hierarchy. Unknown IDs are never fabricated and title similarity alone never merges torrents.

## JacRed and media information

JacRed is optional enrichment, not the source of truth. TorrServer remains authoritative for the torrent list, hash and FileStats. The `--jacred` option accepts a **base service URL** (for example `https://jac.red`), not a page URL for a particular release and not a hash.

### How a JacRed match is found

1. torr2strm prepares up to six distinct candidate title queries from the TorrServer title, the normalized series title for TV, and available metadata title fields.
2. It queries JacRed's Prowlarr-compatible search endpoint, `/api/v1/search`. The search type is `tvsearch` for known TV, `movie` for known movies, or general `search` when TorrServer category is unknown. The configured indexer ID, result limit and optional API key are applied to the request.
3. Search results are not accepted merely because their titles look similar. The result's `infoHash`, `guid`, or BTIH extracted from its magnet/download URL must equal the TorrServer hash.
4. If several exact matches exist, the implementation prefers the result with usable ffprobe data, then a magnet link, then recognizable category information. All enrichment fields are taken from that same selected result.

An exact JacRed result can provide:

- a full ffprobe stream payload, if the result contains usable video-stream data;
- category information, but only when TorrServer's category field is blank;
- a magnet URL for the Kodi/Elementum output, when present;
- additional trustworthy provider IDs, when present.

JacRed cannot change the category when TorrServer already supplies a non-empty category. Anime remains `_uncategorized`.

### Where ffprobe data comes from

`torr2strm` does **not** execute a local `ffprobe` process and does not install or configure the binary. It reads JSON returned by the TorrServer HTTP API:

```text
GET /ffp/{hash}/{file_id}
```

TorrServer must itself have a working, available `ffprobe` binary and its `/ffp/{hash}/{file_id}` endpoint must be reachable from torr2strm. TorrServer documents `GET /ffp/status` as the ffprobe-availability check. For a local instance, for example:

```bash
curl -i http://127.0.0.1:8097/ffp/status
```

Use the actual TorrServer base URL and port configured on your host. The torr2strm configuration currently has no TorrServer HTTP-authentication fields, so its API endpoints must be accessible to the process without credentials that only a browser supplies.

Media-info priority:

```text
valid reusable NFO in either output tree
        ↓ if missing or unusable
exact JacRed hash match with a usable ffprobe payload
        ↓ if missing or unusable
TorrServer GET /ffp/{hash}/{file_id}
```

The primary eligible video file (largest playable video by size, with path as a stable tie-breaker) determines one quality root and one quality label for the entire torrent. Do not probe every episode merely to classify quality; the existing per-file probe work is retained when needed to place accurate stream details in each file's NFO.

Quality-resolution priority:

1. Real ffprobe data for the primary video file, including a valid cached NFO for that exact source file.
2. An explicit structured quality/resolution field from TorrServer metadata or an exact-hash JacRed result.
3. Explicit resolution/interlace/HDR/Dolby Vision markers in the torrent release title.
4. Unknown quality.

The root is always either `4K` (primary dimensions with max(width, height) >= 2160) or `1080p` (everything else, including unknown quality). The display label is independent: for example, a 720p torrent is stored under the `1080p` root but its Kodi item name says `720p`. If quality is unknown, the root is `1080p` and the name has no quality suffix. Codec/source tokens such as `WEB-DL`, `BluRay`, `HEVC` or `HD` alone are not treated as a resolution.

## Magnet handling for Kodi

TorrServer status does not expose the original magnet in its standard status structure. The service therefore prefers, in order:

1. a magnet found in TorrServer custom `data`;
2. an exact JacRed match `magnetUrl`/magnet-bearing URL;
3. a constructed BTIH magnet with `xt=urn:btih:<hash>&dn=<title>`.

The complete magnet is URL-encoded as the value of the Elementum `uri` parameter.

For TV the `oindex` value is the zero-based original FileStats order, not the one-based TorrServer `/play` file id and not the sorted human/path order.

## NFO responsibility boundary

NFOs contain only reliable identifiers and technical stream data. They deliberately do not contain human-readable display names: Kodi was observed to replace its localized scraper title with the NFO title after scraping, and Jellyfin should retrieve localized names from provider IDs.

- **Movie sidecar NFO:** trusted provider IDs, ffprobe stream details for the specific movie media item, and a Kodi Combination NFO scraper URL when a trustworthy TMDb ID is available.
- **Series-root `tvshow.nfo`:** trusted series IDs and a Combination NFO scraper URL when a trustworthy TMDb ID is available. No title or arbitrary episode stream details.
- **Episode sidecar NFO:** ordinary `episodedetails` format, trusted IDs, known season/episode coordinates, and ffprobe stream details for the exact source file. No Combination NFO URL.
- **No NFO type writes** `title`, `originaltitle`, `sorttitle`, `showtitle`, `name`, year/date display metadata, or any other human-facing title field.
- Per-file ffprobe details remain tied to their own STRM/NFO. The primary file determines torrent-level quality only; it does not replace the episode/file technical details.

Useful video/audio/subtitle stream fields include codec, bitrate, dimensions, aspect ratio, frame rate, scan type, bit depth, HDR type, stereomode, audio language/channels/sampling rate, and subtitle codec/language/title flags when available.

A valid reusable NFO may be copied between output trees for the same source file so another ffprobe call can be avoided. Jellyfin may read the stream details but does not necessarily use them as its effective stream information; their retention is still useful to Kodi and other consumers.

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
- `[quality]`: timeout and retry policy for TorrServer ffprobe requests.
- `[jacred]`: optional base URL, API key, indexer ID, result limit, timeout and retries.
- `[logging]`: log verbosity.

Command-line parameters:

```text
--config PATH              Use a configuration file other than /etc/torr2strm/config.toml
--dry-run                  Log planned output changes without writing/deleting output files or removing torrents
--version                  Print the installed version and exit
--jacred URL               Override the configured JacRed base URL for this run
--no-jacred                Disable JacRed for this run
--jacred-api-key KEY       Override the JacRed API key for this run
--jacred-indexer-id ID     Override the JacRed indexer ID; 0 searches all indexers
--jacred-limit N           Override JacRed result limit (1–1000)
```

`--jacred` and `--no-jacred` are mutually exclusive. Command-line overrides apply only to that invocation and do not rewrite the TOML file. JacRed is disabled when its configured URL is empty.

Example:

```bash
sudo /usr/bin/python3 /opt/torr2strm/torr2strm.py --config /etc/torr2strm/config.toml --dry-run
sudo /usr/bin/python3 /opt/torr2strm/torr2strm.py --jacred https://jac.red --jacred-indexer-id 1 --jacred-limit 100 --dry-run
```

## Service behavior

The service is a systemd oneshot triggered by `torr2strm.timer`.

Recoverable media-information failures no longer prevent the STRM from being materialized if quality can be determined from structured metadata/title, or defaulted to the `1080p` root. An identity-only NFO is written if per-file ffprobe data is unavailable. Fatal configuration/source errors still exit non-zero.

## Upgrade / clean start

Software version is `1.4.0`, manifest format is v5, and NFO format is v3. Manifest v4 and older schemas are intentionally rejected. For the development rollout, do not migrate old trees: stop the timer/service, disable Jellyfin reverse deletion temporarily, clear all contents (including `.torr2strm` state) of the explicitly configured Jellyfin and Kodi roots, install the new program, recreate state and run a manual sync. Never clear any parent directory outside the configured roots. See [ROADMAP.md](ROADMAP.md) for the implemented behavior contract and tests.
