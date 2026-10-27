# Version model

Software version: 1.4.14 (official release published 2026-10-27; installed on reference MINI-Ubuntu on 2026-10-27).

v1.4.14 implements Phase 17 category stability. It retains the validated category from TorrServer action=list when a refreshed action=get response omits it and defers a torrent with an unresolved category if a trusted prior manifest category exists, preserving the previous outputs for that run. Explicit unsupported categories still map to _uncategorized. The structured Apprise notification behavior introduced in v1.4.13 is retained.

Manifest schema: v5.

NFO format: v3.

These version domains are intentionally independent. In v1.4.11, NFO serialization no longer embeds the application release version in its generated-by header, preventing app-only upgrades from rewriting sidecars. NFO content and `NFO_FORMAT_VERSION` remain significant and must trigger updates when they actually change. Manifest v5 and NFO format v3 are unchanged; no data migration is required. Canonical Kodi series names/years and transient quality-failure retention are synchronization behavior changes, not manifest schema changes. Keep any future schema version change explicit and independent of the software version.

Reference deployment follow-up (2026-10-27): the installed v1.4.14 source matches the verified official release source; the first live sync completed successfully with no output changes. The calendar-timer experiment was reverted because the `NEXT=-` observation occurred while the Type=oneshot service was still running and did not establish a timer defect. The reference host now uses `OnBootSec=2min` plus `OnUnitActiveSec=5min`, retaining the 15-second randomized delay and one-second accuracy. `systemd-analyze verify` passed; after the run completed at 22:42:57 +05 with exit status 0, the timer was active/enabled and scheduled its next trigger for 22:45:20 +05. This does not change application version 1.4.14, manifest schema v5, or NFO format v3.
