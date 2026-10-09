# Configuration reference

Production file: `/etc/torr2strm/config.toml`.
Example shipped with the repository: [`etc/config.toml.example`](../etc/config.toml.example).

`torr2strm` reads one TOML file at startup. Command-line flags can override selected JacRed values for a single run; they do not write back to the file. Unknown keys do not provide additional features unless the program explicitly reads them.

## Complete parameter map

### `[torrserver]`

| Parameter | Default | Meaning |
|---|---:|---|
| `url` | required by configuration loader; example `http://127.0.0.1:8097` | Base URL of TorrServer, without `/torrents`, `/playlist` or `/ffp/...`. Must begin with `http://` or `https://`. |
| `timeout_sec` | `20` | General TorrServer request timeout in seconds. |
| `remove_timeout_sec` | `60` | Timeout budget for source-torrent removal and its confirmation. |
| `metadata_wait_sec` | `10` | Maximum time allowed to wait for torrent metadata/FileStats to become available. |
| `metadata_poll_sec` | `0.5` | Polling interval while waiting for metadata. |

torr2strm obtains the torrent list and file inventory from TorrServer. It does not use a local media-directory scan as the source of truth.

### `[outputs.jellyfin]` and `[outputs.kodi]`

| Parameter | Jellyfin default | Kodi default | Meaning |
|---|---|---|---|
| `root` | `/mnt/torr2strm-media` | `/mnt/torr2strm-media-kodi` | Absolute root of this independently generated output tree. |
| `manifest` | `.torr2strm/manifest.json` | `.torr2strm/manifest.json` | Manifest path relative to that output root. Keep it a safe relative path; each output must have its own manifest. |
| `enabled` | `true` | `true` | Whether this output is generated and synchronized. At least one output must be enabled. |
| `remove_torrent_on_strm_delete` | `false` | not applicable (forced off) | Jellyfin-only reverse-deletion switch. If enabled, deleting a tracked Jellyfin STRM may remove its source torrent from TorrServer using `action=rem`. `action=drop` is not used. |
| `max_torrent_removals_per_run` | `1` | not applicable (forced to `0`) | Safety cap on source-torrent removals caused by missing managed Jellyfin STRM files during one run. `0` disables such removals even if reverse deletion is enabled. |

Jellyfin is the authoritative/read-write output. Kodi/Elementum is read-only: deleting Kodi STRM files never removes a TorrServer torrent. Disappearing torrents are reflected in all enabled outputs on synchronization.

### `[sync]`

| Parameter | Default | Meaning |
|---|---|---|
| `tv_unmatched_season` | `0` | Season number used when a TV file's season cannot be inferred from its path/title. `0` corresponds to Specials-style season semantics. |
| `video_extensions` | See `etc/config.toml.example` | List of file extensions eligible for video STRM and per-file media-info processing. Extensions may be written with or without the leading dot. Audio, subtitle and image files do not get their own video STRM. |

### `[quality]`

| Parameter | Default | Meaning |
|---|---:|---|
| `timeout_sec` | `12` | Timeout in seconds for a TorrServer `/ffp/{hash}/{file_id}` request. |
| `retries` | `0` | Additional attempts after a failed/unusable TorrServer ffprobe response. `0` means one attempt total, without retries. Must be non-negative. |

Quality root is based on the primary eligible video file (largest eligible video by size, then path). The root is `4K` when `max(width, height) >= 2160`, otherwise `1080p`. The display label is resolved independently: usable ffprobe data (including cached stream details for that exact file), then a structured quality field from TorrServer/exact-hash JacRed, then explicit resolution/HDR/DV markers in the release title, then unknown. Unknown quality uses the `1080p` root but does not add a quality suffix to the Kodi basename. Other video files may still be probed to keep their own NFO stream details correct.

### `[jacred]`

| Parameter | Default | Meaning |
|---|---:|---|
| `url` | `https://jac.red` in the example; empty disables JacRed | Base URL of the JacRed/Prowlarr-compatible search service, not a release-page URL. torr2strm appends `/api/v1/search`. |
| `api_key` | empty | Optional API key sent as `X-Api-Key`. |
| `indexer_id` | `1` in the example; code default `0` | Indexer ID filter. A value greater than `0` is passed as `indexerIds`; `0` means search all indexers. |
| `limit` | `100` | Maximum result count per search request. Allowed range: `1`–`1000`. |
| `timeout_sec` | `10` | HTTP timeout per JacRed search request. Must be greater than zero. |
| `retries` | `0` | Number of extra attempts for a failed JacRed request. Must be non-negative. |

JacRed is enrichment only. It is optional, and a timeout or miss does not make a title-based approximate result acceptable. See [Architecture](ARCHITECTURE.md) for matching and category rules.

### `[logging]`

| Parameter | Default | Meaning |
|---|---|---|
| `level` | `INFO` | Python logging threshold; use values such as `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |

## Command-line parameters

Run the program directly, for example:

```bash
python3 /opt/torr2strm/torr2strm.py --config /etc/torr2strm/config.toml --dry-run
```

| Option | Argument | Meaning |
|---|---|---|
| `--config` | `PATH` | Read this TOML file instead of `/etc/torr2strm/config.toml`. |
| `--dry-run` | none | Perform discovery and planning but avoid output filesystem mutations and real source-torrent removals. Use it to review logs before a normal sync. |
| `--version` | none | Print the application version and exit. |
| `--jacred` | `URL` | Override `[jacred].url` for this invocation. Supply a service base URL such as `https://jac.red`, not a URL to one release. |
| `--no-jacred` | none | Disable JacRed enrichment for this invocation. |
| `--jacred-api-key` | `KEY` | Override `[jacred].api_key` for this invocation. |
| `--jacred-indexer-id` | integer | Override `[jacred].indexer_id`; `0` means all indexers. |
| `--jacred-limit` | integer | Override `[jacred].limit`; must be in the range `1`–`1000`. |

`--jacred` and `--no-jacred` are mutually exclusive. CLI overrides are in-memory only and are not persisted to the TOML file.

## ffprobe prerequisite on TorrServer

`torr2strm` does **not** run or install the `ffprobe` executable itself. It requests stream JSON from TorrServer using `GET /ffp/{hash}/{file_id}`. The TorrServer instance therefore must have a working ffprobe binary and expose the `/ffp/...` endpoint to the torr2strm process.

Check the endpoint documented by TorrServer for ffprobe availability:

```bash
curl -i http://127.0.0.1:8097/ffp/status
```

Replace the address and port with the actual `[torrserver].url`. The current torr2strm client has no TorrServer username/password options; if the API is protected by HTTP Basic Auth, arrange access in the deployment so the endpoints can be reached by torr2strm without unsupported client-side credentials.

## Minimal example

```toml
[torrserver]
url = "http://127.0.0.1:8097"
timeout_sec = 20
remove_timeout_sec = 60
metadata_wait_sec = 10
metadata_poll_sec = 0.5

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

[quality]
timeout_sec = 12
retries = 0

[jacred]
url = "https://jac.red"
api_key = ""
indexer_id = 1
limit = 100
timeout_sec = 10
retries = 0

[logging]
level = "INFO"
```

Use the complete [`etc/config.toml.example`](../etc/config.toml.example) for the `sync` extension list and the full set of supported options.

Never commit a production config containing API keys, passwords or other secrets.
