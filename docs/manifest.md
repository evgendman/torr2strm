# Manifest

Each enabled output root owns an independent `.torr2strm/manifest.json` and `.torr2strm/root.marker`.

The manifest records the relationship between TorrServer source torrents and filesystem reflections and is operational reconciliation state.

Current schema: v4. Old v3 state is intentionally rejected by the current implementation; a clean rebuild is required when upgrading from the incompatible older state.

Jellyfin and Kodi manifests are independent. Kodi state can never authorize TorrServer source removal.
