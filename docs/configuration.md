# Configuration reference

Production file: `/etc/torr2strm/config.toml`.
Example shipped with the repository: [`etc/config.toml.example`](../etc/config.toml.example).

`torr2strm` reads one TOML file at startup. Unknown tables such as a legacy `[jacred]` section are ignored; v1.4.5 has no external-source settings or network lookup flags. Unknown keys do not provide additional features unless the program explicitly reads them.

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
| `video_extensions` | See `etc/config.toml.example` | List of file extensions eligible for video STRM generation and cached-NFO lookup. Extensions may be written with or without the leading dot. Audio, subtitle and image files do not get their own video STRM. |

### Quality decision rules

- Category comes only from the category field already present in TorrServer. Missing, blank, or unsupported values remain _uncategorized.
- The 4K root is selected only when usable ffprobe stream data already embedded in TorrServer metadata proves a video dimension of at least 3840 pixels.
- If that payload is missing, invalid, or does not prove 4K, the root is 1080p.
- Structured quality fields and explicit markers in the TorrServer title may supply a visible filename label, but never select the 4K root.
- Cached NFOs may be reused for exact-file streamdetails only; they do not affect category, root, or display label.
- No local ffprobe is run and TorrServer /ffp/ is never called. No JacRed, Prowlarr, or other external-source requests are made.

Legacy [quality] timeout/retry keys and [jacred] configuration tables in existing config files are ignored by v1.4.5 and can be removed.

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


## ffprobe is optional

v1.4.1 does not make any `/ffp/` requests. The TorrServer ffprobe executable and `/ffp/status` endpoint are not prerequisites for torr2strm. Technical stream details are carried into NFO only when they already exist in a matching release payload or a valid cached NFO; otherwise, the NFO contains identity data only.

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

# Legacy [quality] timeout/retries keys are intentionally omitted in v1.4.1.

[logging]
level = "INFO"
```

Use the complete [`etc/config.toml.example`](../etc/config.toml.example) for the `sync` extension list and the full set of supported options.

Never commit a production config containing API keys, passwords or other secrets.
