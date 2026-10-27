# Version model

Software version: 1.4.14 (official release published 2026-10-27; installed on reference MINI-Ubuntu on 2026-10-27).

v1.4.14 implements Phase 17 category stability. It retains the validated category from TorrServer action=list when a refreshed action=get response omits it and defers a torrent with an unresolved category if a trusted prior manifest category exists, preserving the previous outputs for that run. Explicit unsupported categories still map to _uncategorized. The structured Apprise notification behavior introduced in v1.4.13 is retained.

Manifest schema: v5.

NFO format: v3.

These version domains are intentionally independent. In v1.4.11, NFO serialization no longer embeds the application release version in its generated-by header, preventing app-only upgrades from rewriting sidecars. NFO content and `NFO_FORMAT_VERSION` remain significant and must trigger updates when they actually change. Manifest v5 and NFO format v3 are unchanged; no data migration is required. Canonical Kodi series names/years and transient quality-failure retention are synchronization behavior changes, not manifest schema changes. Keep any future schema version change explicit and independent of the software version.

Reference deployment follow-up (2026-10-27): the installed v1.4.14 source matches the verified official release source; the first live sync completed successfully with no output changes. The systemd timer template was changed on the reference host to use `OnCalendar=*:0/5` and `Persistent=true` after an ambiguous `NEXT=-` observation while the Type=oneshot service was still running. A post-completion state check of the former monotonic timer was not captured, so a true failure to re-arm was not established. The corrected timer is active and enabled, and systemd reported a concrete next calendar event. The timer template correction is now recorded on the main development branch but is not part of the already-published v1.4.14 tag/archive; include it in the next release. This does not change the application version, manifest schema v5, or NFO format v3.
