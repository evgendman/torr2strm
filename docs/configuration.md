# Configuration reference

Production file: `/etc/torr2strm/config.toml`.
Example shipped with the repository: [`etc/config.toml.example`](../etc/config.toml.example).

`torr2strm` reads one TOML file at startup. Unknown tables such as a legacy `[jacred]` section are ignored; v1.4.8 has no external-source settings or network lookup flags. Unknown keys do not provide additional features unless the program explicitly reads them.

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

Jellyfin is the authoritative/read-write output. Kodi/Elementum is read-only: deleting Kodi STRM files never removes a TorrServer torrent. Disappearing torrents are reflected in all enabled outputs on synchronization. Kodi path components are sanitized for Windows/SMB compatibility; Jellyfin path naming remains unchanged.

### `[sync]`

| Parameter | Default | Meaning |
|---|---|---|
| `tv_unmatched_season` | `0` | Season number used when a TV file's season cannot be inferred from its path/title. `0` corresponds to Specials-style season semantics. |
| `video_extensions` | See `etc/config.toml.example` | List of file extensions eligible for video STRM generation and cached-NFO lookup. Extensions may be written with or without the leading dot. Audio, subtitle and image files do not get their own video STRM. |

### Quality decision rules

- Category comes only from the category field already present in TorrServer. Missing, blank, or unsupported values remain _uncategorized.
- Usable ffprobe video dimensions already embedded in TorrServer metadata take priority: a maximum dimension of at least 3840 selects `4K`; lower dimensions select `1080p`.
- If usable dimensions are unavailable, recognized structured quality/resolution fields describing the release determine the root; a recognized lower resolution selects `1080p` and outranks conflicting title/path text.
- If structured resolution is unavailable, a standalone `4K`/`2160p` marker in the TorrServer title selects `4K`; explicit release markers in file paths are the next fallback. Token boundaries prevent partial matches.
- Only if no higher-priority evidence is usable does the root default to `1080p`. Import route/provenance (including the Sonarr-1080 instance), codec and source labels are never resolution evidence.
- Root classification remains separate from the displayed basename quality label.
- Cached NFOs may be reused for exact-file streamdetails only; they do not affect category, root, or display label.
- No local ffprobe is run, TorrServer `/ffp/` is never called, and no JacRed, Prowlarr, or other external-source requests are made.

For Kodi TV, an identified series receives one root-level `tvshow.nfo`; season folders and episode STRMs do not get NFO sidecars. Jellyfin's existing per-item NFO output and Kodi movie NFO behavior are unchanged.

Legacy [quality] timeout/retry keys and [jacred] configuration tables in existing config files are ignored by v1.4.8 and can be removed.

### [notifications]

| Parameter | Default | Meaning |
|---|---|---|
| enabled | false | Enable aggregated Apprise notifications. |
| apprise_url | empty | Base URL of the shared Apprise API, e.g. `http://192.168.0.14:8000`; do not add `/notify/...`. |
| apprise_config_key | arr-stack | Apprise API configuration key appended as `/notify/{key}`. |
| service_name | torr2strm | Human-readable service label included in message titles/bodies. |
| timeout_sec | 5 | Bounded notification HTTP timeout; valid range is 1–30 seconds. |
| tag_warning | warnings | Apprise tag for warning events. |
| tag_critical | critical | Apprise tag for error events. |
| state_path | /var/lib/torr2strm/notification_state.json | Persistent warning/error fingerprints, separate from media manifests and TorrServer state. |

The API contract is `POST {apprise_url}/notify/{apprise_config_key}` with JSON fields `title`, `body`, `type`, and `tag`. Warnings use type `warning` and tag `warnings`; errors use type `failure` and tag `critical`. Telegram credentials and topic IDs stay in the shared gateway. Events are aggregated by severity; unchanged fingerprints are not resent on every timer run, and a clean cycle resets the fingerprint. Delivery failures are logged but never fail synchronization.

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
