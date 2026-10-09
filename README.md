# torr2strm v1.3.2

TorrServer -> multiple materialized STRM/NFO trees.

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

The second directory level is determined independently from category: `4K` when the primary eligible video stream has a maximum dimension of at least 2160 pixels; otherwise `1080p`. A usable cached NFO may supply this previously resolved quality.

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

A JacRed result is not accepted as technical media information unless its payload contains a usable video stream. `quality`, `videotype`, voice labels and title tokens are not substitutes for ffprobe dimensions. The selected primary eligible video stream determines the torrent's quality directory; the other playable files still receive per-file media data as required by the output trees.

Quality rule:

```text
max(width, height) >= 2160 -> 4K
otherwise                  -> 1080p
```

No title-based `1080p`/`2160p` fallback exists.

## Magnet handling for Kodi

TorrServer status does not expose the original magnet in its standard status structure. The service therefore prefers, in order:

1. a magnet found in TorrServer custom `data`;
2. an exact JacRed match `magnetUrl`/magnet-bearing URL;
3. a constructed BTIH magnet with `xt=urn:btih:<hash>&dn=<title>`.

The complete magnet is URL-encoded as the value of the Elementum `uri` parameter.

For TV the `oindex` value is the zero-based original FileStats order, not the one-based TorrServer `/play` file id and not the sorted human/path order.

## NFO responsibility boundary

`torr2strm` does not try to become a movie/TV metadata scraper. NFO contains only data that the importer can know reliably plus the real media-info payload.

```text
NFO
├── identification and base metadata
│   ├── title
│   ├── originaltitle
│   ├── sorttitle (when known)
│   ├── year
│   ├── premiered / releasedate for known content-level dates
│   ├── aired for known episode-level dates
│   ├── season / episode
│   └── all trustworthy provider IDs available to the importer
│
└── technical file data
    └── fileinfo/streamdetails
        ├── video
        │   ├── codec
        │   ├── bitrate
        │   ├── dimensions
        │   ├── aspect/aspectratio
        │   ├── framerate
        │   ├── scantype
        │   ├── bitdepth (when available)
        │   ├── hdrtype (when available)
        │   └── stereomode (when available)
        ├── audio
        │   ├── codec
        │   ├── bitrate
        │   ├── language
        │   ├── title
        │   ├── channels
        │   └── samplingrate
        └── subtitle
            ├── codec
            ├── language
            └── title/flags
```

TV roots also receive a `tvshow.nfo` with the same known series-level identity/base metadata and provider IDs. Episode NFOs contain the episode identity plus per-file stream details.

A valid NFO found in one output can be copied verbatim into the other output. This avoids a second ffprobe when one tree already has a cached NFO.

### NFO format and migration

NFOs have their own format version, independent from the torr2strm software version. Current `NFO_FORMAT_VERSION` is `2`. Legacy/unversioned torr2strm NFOs are treated as format v1 and are automatically migrated to v2 during normal synchronization. Migration does not probe media again.

Format v2 adds a `<torr2strm formatversion="2" />` marker inside the XML and, when a trustworthy TMDb ID is present, one Kodi Combination NFO URL after the closing XML root:

```text
https://www.themoviedb.org/movie/<tmdbid>
```

For TV root `tvshow.nfo`, the URL uses `/tv/<tmdbid>`. Episode NFOs do not get a Combination URL. Only one scraper URL is written; TMDb is preferred when its ID is known. Existing valid metadata and `fileinfo/streamdetails` are retained during migration. Future format migrations can be added as explicit version-to-version steps without cleaning the output trees.

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

Recoverable failures for individual torrents are logged in `DONE` as `quality_unresolved`/`failed`, but the process exits `0` so systemd does not mark the oneshot as failed. Fatal configuration/source errors still exit non-zero.

## Upgrade / clean start

Manifest format is v4. Old v3 state is intentionally rejected. For a clean start, stop the service/timer, clear the contents of both output roots, keep the root directories themselves, install the new version, and run one manual sync.
