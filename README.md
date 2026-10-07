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

1. A non-empty explicit TorrServer category `movie` or `tv` wins.
2. If TorrServer category is actually blank, one exact JacRed BTIH/infoHash match may supply the category from that same search result.
3. JacRed `movie` -> `movie`.
4. JacRed `tv`/series -> `tv`.
5. JacRed `anime` -> `_uncategorized` (never forced into TV).
6. If category is unknown, output stays under `_uncategorized`.

No category is guessed from title text.

## JacRed and media information

JacRed is optional. The service uses `/api/v1/search`, performs a title search, then compares exact BTIH/infoHash.

The exact matched JacRed result can provide, without a second request:

- full `ffprobe` stream payload;
- category information;
- full `magnetUrl` when present;
- additional provider IDs when present.

Only a real `ffprobe` payload with a usable video stream is accepted for technical media information. `quality`, `videotype`, `voices` and title tokens are not substitutes for ffprobe.

Media-info order:

```text
valid existing NFO in any output
        ↓
exact JacRed match with full ffprobe
        ↓
TorrServer /ffp/{hash}/{file_id}
```

Quality is always derived from real video dimensions:

```text
max(width, height) >= 2160 -> 4K
otherwise                  -> 1080p
```

No `1080p`/`2160p` title fallback exists.

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

## Configuration

The main config is `/etc/torr2strm/config.toml`.

```toml
[outputs.jellyfin]
root = "/mnt/torr2strm-media"
manifest = ".torr2strm/manifest.json"
enabled = true
remove_torrent_on_strm_delete = false
max_torrent_removals_per_run = 1

[outputs.kodi]
root = "/mnt/torr2strm-media-kodi"
manifest = ".torr2strm/manifest.json"
enabled = true
```

JacRed:

```toml
[jacred]
url = "https://jac.red"
api_key = ""
indexer_id = 1
limit = 100
timeout_sec = 10
retries = 0
```

The command line still supports one-run JacRed overrides:

```text
--jacred URL
--no-jacred
--jacred-api-key KEY
--jacred-indexer-id ID
--jacred-limit N
```

## Service behavior

The service is a systemd oneshot triggered by `torr2strm.timer`.

Recoverable failures for individual torrents are logged in `DONE` as `quality_unresolved`/`failed`, but the process exits `0` so systemd does not mark the oneshot as failed. Fatal configuration/source errors still exit non-zero.

## Upgrade / clean start

Manifest format is v4. Old v3 state is intentionally rejected. For a clean start, stop the service/timer, clear the contents of both output roots, keep the root directories themselves, install the new version, and run one manual sync.
