# Current state

Software version: 1.4.9.
Manifest schema: v5.
NFO format: v3.

Reference deployment paths:
- /opt/torr2strm/torr2strm.py
- /etc/torr2strm/config.toml
- /mnt/torr2strm-media
- /mnt/torr2strm-media-kodi

Release v1.4.9 was installed and accepted on the reference mini-PC on 2026-10-22. GitHub Actions passed 41 tests. The release was compiled and version/source-match checks passed before installation; `/etc/torr2strm/config.toml`, TorrServer inventory, generated output roots and manifests were preserved. No clean rebuild or manifest/NFO schema migration was required.

Collision acceptance: the initial dry run reported 22 `STRM_COLLISION_RESOLVED` entries for the Lexx multi-DVD release and no `STRM_COLLISION_UNRESOLVED`. The real sync exited 0 and finished with `torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=154 unchanged=150 failed=1`. All 22 Lexx collision files were then found under `/mnt/torr2strm-media` with unique names containing the short torrent hash and exact TorrServer `file_id`. A follow-up dry run reported `created=0 updated=0 unchanged=304 failed=1`, confirming stable output paths. The single `QUALITY_SKIP` is the known Blu-ray ISO for *Monsters, Inc.* with no video files; it is unrelated to collision resolution and is still counted in `failed` by the current summary semantics.

Timer acceptance: `torr2strm.timer` is `active` and `enabled`. Its timer-triggered service run on 2026-10-22 started at 20:06:16 +05, completed at 20:06:44 +05 with `Result=success` and exit status 0, and reported 304 unchanged outputs. The configured TorrServer API for torr2strm is `http://127.0.0.1:8092`; port 8097 is also listening but belongs to a separate process. Software version is 1.4.9, manifest schema v5 and NFO format v3. Phase 10, the separate `torr2jellyfin` orchestration/report interface, remains deferred.
