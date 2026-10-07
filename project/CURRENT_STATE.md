# Current state

Baseline: torr2strm 1.3.2.

Reference deployment uses:
/opt/torr2strm/torr2strm.py
/etc/torr2strm/config.toml
/mnt/torr2strm-media
/mnt/torr2strm-media-kodi

Current capabilities include multi-output synchronization, manifest v4, NFO v2, automatic legacy NFO v1 to v2 migration, TMDb Combination NFO support, exact BTIH JacRed enrichment, ffprobe-based quality resolution, UTF-8-safe long basename handling, independent manifests, and authoritative Jellyfin reverse deletion.

The current package is already installed and tested on the reference media server. The 1.3.2 package is the implementation source baseline for this repository.