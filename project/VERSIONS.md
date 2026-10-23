# Version model

Software version: 1.4.12.

v1.4.12 narrows canonical title/year selection to Kodi TV series. Movie directory naming remains unchanged from the prior per-quality-root policy; a regression guards this after the v1.4.11 mini-PC dry run exposed unplanned movie path reconciliation.

Manifest schema: v5.

NFO format: v3.

These version domains are intentionally independent. In v1.4.11, NFO serialization no longer embeds the application release version in its generated-by header, preventing app-only upgrades from rewriting sidecars. NFO content and `NFO_FORMAT_VERSION` remain significant and must trigger updates when they actually change. Manifest v5 and NFO format v3 are unchanged; no data migration is required. Canonical Kodi series names/years and transient quality-failure retention are synchronization behavior changes, not manifest schema changes. Keep any future schema version change explicit and independent of the software version.
