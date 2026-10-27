# Version model

Software version: 1.4.14 (candidate).

v1.4.14 implements Phase 17 category stability. It retains the validated category from TorrServer action=list when a refreshed action=get response omits it and defers a torrent with an unresolved category if a trusted prior manifest category exists, preserving the previous outputs for that run. Explicit unsupported categories still map to _uncategorized. The structured Apprise notification behavior introduced in v1.4.13 is retained.

Manifest schema: v5.

NFO format: v3.

These version domains are intentionally independent. In v1.4.11, NFO serialization no longer embeds the application release version in its generated-by header, preventing app-only upgrades from rewriting sidecars. NFO content and `NFO_FORMAT_VERSION` remain significant and must trigger updates when they actually change. Manifest v5 and NFO format v3 are unchanged; no data migration is required. Canonical Kodi series names/years and transient quality-failure retention are synchronization behavior changes, not manifest schema changes. Keep any future schema version change explicit and independent of the software version.
