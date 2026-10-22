# Current state

Software version: 1.4.10.
Manifest schema: v5.
NFO format: v3.

Reference deployment paths:
- /opt/torr2strm/torr2strm.py
- /etc/torr2strm/config.toml
- /mnt/torr2strm-media
- /mnt/torr2strm-media-kodi

Release v1.4.9 was installed and accepted on the reference mini-PC on 2026-10-22. Release v1.4.10 was then installed and accepted on the same day; CI passed all 41 tests. The v1.4.10 dry run exited 0 and emitted the expected title-bearing Lexx `STRM_COLLISION_SUMMARY` with 22 affected files and three representative source paths. `/etc/torr2strm/config.toml`, TorrServer inventory, generated output roots and manifests were preserved. No clean rebuild or manifest/NFO schema migration was required.

Collision acceptance: the initial dry run reported 22 `STRM_COLLISION_RESOLVED` entries for the Lexx multi-DVD release and no `STRM_COLLISION_UNRESOLVED`. The real sync exited 0 and finished with `torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=154 unchanged=150 failed=1`. All 22 Lexx collision files were then found under `/mnt/torr2strm-media` with unique names containing the short torrent hash and exact TorrServer `file_id`. A follow-up dry run reported `created=0 updated=0 unchanged=304 failed=1`, confirming stable output paths. The single `QUALITY_SKIP` is the known Blu-ray ISO for *Monsters, Inc.* with no video files; it is unrelated to collision resolution and is still counted in `failed` by the current summary semantics.

Timer acceptance: after installing v1.4.10, the user started `torr2strm.timer` on 2026-10-22. The timer-triggered service started at 21:57:31 +05 and completed at 21:58:01 +05 with `Result=success`, exit status 0 and `DONE torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=152 unchanged=152 failed=1 nfo_reused=0 outputs=jellyfin,kodi`. This run performed NFO updates (including Kodi `tvshow.nfo`) due the application-version header issue recorded below; no errors caused the service to fail. After completion, `torr2strm.service` was `inactive` / `dead` with `Result=success`, `ExecMainStatus=0`, while `torr2strm.timer` remained `active` / `waiting` / `enabled`. The configured TorrServer API for torr2strm is `http://127.0.0.1:8092`; port 8097 belongs to a separate process. Current manifest schema is v5 and NFO format is v3. Phase 10, the separate `torr2jellyfin` orchestration/report interface, remains deferred.

Apprise notification acceptance: on 2026-10-22, `[notifications].enabled` was changed from `false` to `true` in `/etc/torr2strm/config.toml`. The existing gateway settings were retained (`http://192.168.0.14:8000`, config key `arr-stack`, warning tag `warnings`, critical tag `critical`). A service run returned `APPRISE_SENT tag=warnings status=200` and `APPRISE_SENT tag=critical status=200`; the user confirmed both messages arrived in the intended Telegram topics. `/var/lib/torr2strm/notification_state.json` was created with mode `0600` (`root:root`) for warning/error fingerprint suppression. A subsequent run remained idempotent (`created=0 updated=0 unchanged=304`), and `torr2strm.timer` remained `active` and `enabled`. Apprise delivery failures are logged locally and do not fail synchronization; unchanged warning/error fingerprints are suppressed on recurring timer runs.

Notification usability follow-up (v1.4.10, accepted on 2026-10-22): the release sends one concise `STRM_COLLISION_SUMMARY` per affected torrent with title, full hash, affected-file counts and representative file paths; per-file warnings remain in the local journal but are excluded from Apprise aggregation, and unresolved-collision diagnostics include the title. The v1.4.10 dry run logged `APPRISE_SENT tag=warnings status=200`; the specific run-generated collision summary was not visually confirmed in Telegram. A separate test POST through the same gateway/config and warnings tag was delivered to Telegram, confirming that the route works.


NFO/versioning follow-up (Phase 14, identified 2026-10-22): the v1.4.10 dry run logged 152 Jellyfin `NFO_UPDATE` items and reported `created=0 updated=152 unchanged=152 failed=1`. The NFO header embeds application `VERSION`, so changing only the app release version currently makes NFO text differ even though `NFO_FORMAT_VERSION=3` is unchanged. The user accepts this one-time rewrite during development. Implement and test separate version semantics: application-version-only changes must not rewrite NFOs; an intentional NFO format version change must trigger the required rewrite. The generated STRM playback URLs/paths are not expected to change from the header-only rewrite, but Jellyfin may notice changed sidecar NFO files and refresh affected metadata depending on library monitoring/settings.
