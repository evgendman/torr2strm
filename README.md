# torr2strm v1.4.1

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

The second directory level is a fixed two-way split: `4K` when the primary eligible video stream has a maximum dimension of at least 3840 pixels; otherwise `1080p`. This is the torrent-level quality root and does not claim that every file is 1080p.

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

### Quality classification without new ffprobe requests

\torr2strm does **not** run local ffprobe and does **not** call TorrServer's \`/ffp/{hash}/{file_id}\` endpoint. This intentionally avoids probing every new movie/episode just to decide which of the two output roots it belongs in.

Only data that is already available is considered:

1. A valid NFO already tracked for this exact torrent file, if present.
2. ffprobe JSON already attached to a hash-exact JacRed release result or embedded in torrent/release metadata.
3. Explicit structured quality/resolution fields in TorrServer metadata or an exact-hash JacRed result.
4. Explicit resolution/interlace/HDR/Dolby Vision markers in the release title.

A metadata field or release title cannot establish quality from source/codec words alone: \`WEB-DL\`, \`BluRay\`, \`HEVC\` and \`HD\` are not proof of a particular resolution. An exact JacRed hash match is required before its ffprobe or quality fields may be used.

#### Quality root and display label are separate

There are only two roots: \`4K\` and \`1080p\`. The root uses an affirmative-evidence rule: **if any available source supplies evidence of 4K-class resolution, the torrent goes into \`4K\`; otherwise it goes into \`1080p\`**. A lower-quality value from one source does not cancel a 4K claim found in another source.

The display label is selected from the highest-priority available evidence, in the order listed above. For example, a cached ffprobe label of \`720p\` and a release title containing \`2160p\` can produce a \`4K\` root but retain \`720p\` as the display label. The root is a grouping/access branch, not a promise that every release in it is actually 4K.

If no source contains a usable quality value, the item goes to \`1080p\` and its basename has **no quality suffix**. Unknown quality is never relabelled as \`1080p\` merely because it lives in that root.

When an existing ffprobe payload is available for the release, its technical stream details may be written into the matching primary-file NFO. Existing per-file NFOs are reused when valid. If no ffprobe data already exists for an item, torr2strm writes the NFO's identity fields only; it does not probe the media to fill in stream details.

Legacy \`[quality]\` timeout/retry settings may remain in an existing TOML file, but v1.4.1 no longer uses them and no ffprobe binary or \`/ffp/status\` check is required.

## Magnet handling for Kodi

TorrServer status does not expose the original magnet in its standard status structure. The service therefore prefers, in order:

1. a magnet found in TorrServer custom `data`;
2. an exact JacRed match `magnetUrl`/magnet-bearing URL;
3. a constructed BTIH magnet with `xt=urn:btih:<hash>&dn=<title>`.

The complete magnet is URL-encoded as the value of the Elementum `uri` parameter.

For TV the `oindex` value is the zero-based original FileStats order, not the one-based TorrServer `/play` file id and not the sorted human/path order.

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
