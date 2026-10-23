# Current state

Software version: 1.4.11 (candidate branch; not yet deployed).
Manifest schema: v5.
NFO format: v3.

Reference deployment paths:
- /opt/torr2strm/torr2strm.py
- /etc/torr2strm/config.toml
- /mnt/torr2strm-media
- /mnt/torr2strm-media-kodi

Release v1.4.9 was installed and accepted on the reference mini-PC on 2026-10-22. Release v1.4.10 was then installed and accepted on the same day; CI passed all 41 tests. The v1.4.10 dry run exited 0 and emitted the expected title-bearing Lexx `STRM_COLLISION_SUMMARY` with 22 affected files and three representative source paths. `/etc/torr2strm/config.toml`, TorrServer inventory, generated output roots and manifests were preserved. No clean rebuild or manifest/NFO schema migration was required.

Collision acceptance: the initial dry run reported 22 `STRM_COLLISION_RESOLVED` entries for the Lexx multi-DVD release and no `STRM_COLLISION_UNRESOLVED`. The real sync exited 0 and finished with `torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=154 unchanged=150 failed=1`. All 22 Lexx collision files were then found under `/mnt/torr2strm-media` with unique names containing the short torrent hash and exact TorrServer `file_id`. A follow-up dry run reported `created=0 updated=0 unchanged=304 failed=1`, confirming stable output paths. The single `QUALITY_SKIP` is the known Blu-ray ISO for *Monsters, Inc.* with no video files; it is unrelated to collision resolution and is still counted in `failed` by the current summary semantics.

Timer state and Phase 16 diagnosis: the v1.4.10 timer-triggered service on 2026-10-22 completed at 21:58:01 +05 with `Result=success`, exit status 0 and `DONE torrents=153 quality_ready=152 quality_unresolved=1 created=0 updated=152 unchanged=152 failed=1 nfo_reused=0 outputs=jellyfin,kodi`. It rewrote existing NFOs because the generated header embedded the application version. On 2026-10-23, a subsequent no-op sync reported `created=0 updated=0 unchanged=304 failed=1`; sampled directories and NFOs had `ctime` changes clustered around the run while their `mtime` values remained older, consistent with redundant `chmod` calls. The user stopped `torr2strm.timer` after diagnosis. The timer must remain stopped until v1.4.11 CI and controlled reference mini-PC acceptance pass. Current candidate keeps manifest schema v5 and NFO format v3. The configured TorrServer API is `http://127.0.0.1:8092`; port 8097 belongs to a separate process. Phase 10, the separate `torr2jellyfin` orchestration/report interface, remains deferred and is excluded from this release.

Apprise notification acceptance: on 2026-10-22, `[notifications].enabled` was changed from `false` to `true` in `/etc/torr2strm/config.toml`. The existing gateway settings were retained (`http://192.168.0.14:8000`, config key `arr-stack`, warning tag `warnings`, critical tag `critical`). A service run returned `APPRISE_SENT tag=warnings status=200` and `APPRISE_SENT tag=critical status=200`; the user confirmed both messages arrived in the intended Telegram topics. `/var/lib/torr2strm/notification_state.json` was created with mode `0600` (`root:root`) for warning/error fingerprint suppression. A subsequent run remained idempotent (`created=0 updated=0 unchanged=304`) at that time. The timer was later stopped on 2026-10-23 after the Phase 16 metadata-churn diagnosis; it remains stopped pending v1.4.11 acceptance. Apprise delivery failures are logged locally and do not fail synchronization; unchanged warning/error fingerprints are suppressed on recurring timer runs.

Notification usability follow-up (v1.4.10, accepted on 2026-10-22): the release sends one concise `STRM_COLLISION_SUMMARY` per affected torrent with title, full hash, affected-file counts and representative file paths; per-file warnings remain in the local journal but are excluded from Apprise aggregation, and unresolved-collision diagnostics include the title. The v1.4.10 dry run logged `APPRISE_SENT tag=warnings status=200`; the specific run-generated collision summary was not visually confirmed in Telegram. A separate test POST through the same gateway/config and warnings tag was delivered to Telegram, confirming that the route works.


NFO/versioning follow-up (Phase 14): candidate v1.4.11 removes the application version from generated NFO headers and normalizes legacy version-only header differences during content comparison. Regression tests cover app-only upgrades and a deliberate `NFO_FORMAT_VERSION` change. NFO format remains v3; no manifest/NFO schema migration or clean rebuild is planned. CI and reference mini-PC acceptance remain pending.
