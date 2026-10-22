# Current state

Software version: 1.4.9.
Manifest schema: v5.
NFO format: v3.

Reference deployment paths:
- /opt/torr2strm/torr2strm.py
- /etc/torr2strm/config.toml
- /mnt/torr2strm-media
- /mnt/torr2strm-media-kodi

The v1.4.8 source/release passed 39 GitHub Actions tests. The mini-PC is on v1.4.8; its dry run exposed duplicate Jellyfin destinations for multi-disc TV releases with repeated VOB basenames that become flattened into Season NN. The v1.4.9 change preplans collisions and appends the short torrent hash plus exact TorrServer file_id; a residual collision escalates to the full hash plus file ID. Normal paths and the existing directory layout remain unchanged.

Mini-PC acceptance of v1.4.9 is pending. Keep torr2strm.timer stopped until the new collision behavior is verified; preserve the configuration, TorrServer inventory, outputs and manifests. No manifest/NFO schema change or clean rebuild is required.
