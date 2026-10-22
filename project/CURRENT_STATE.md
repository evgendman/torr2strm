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

Apprise notification acceptance: on 2026-10-22, `[notifications].enabled` was changed from `false` to `true` in `/etc/torr2strm/config.toml`. The existing gateway settings were retained (`http://192.168.0.14:8000`, config key `arr-stack`, warning tag `warnings`, critical tag `critical`). A service run returned `APPRISE_SENT tag=warnings status=200` and `APPRISE_SENT tag=critical status=200`; the user confirmed both messages arrived in the intended Telegram topics. `/var/lib/torr2strm/notification_state.json` was created with mode `0600` (`root:root`) for warning/error fingerprint suppression. A subsequent run remained idempotent (`created=0 updated=0 unchanged=304`), and `torr2strm.timer` remained `active` and `enabled`. Apprise delivery failures are logged locally and do not fail synchronization; unchanged warning/error fingerprints are suppressed on recurring timer runs.

Notification usability follow-up (planned in v1.4.10, not yet deployed): the first warning message showed per-file Lexx collision diagnostics without a release title. The proposed patch will send one concise `STRM_COLLISION_SUMMARY` per affected torrent with title, full hash, affected-file counts and representative file paths. Per-file warning records will remain available in the local journal but will not be repeated as separate Apprise bullets. Critical unresolved-collision diagnostics will include the title. The deployed mini-PC remains on v1.4.9 until this patch is released and accepted.
