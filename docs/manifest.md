# Manifest

Each enabled output root owns an independent `.torr2strm/manifest.json` and `.torr2strm/root.marker`.

The manifest records the relationship between TorrServer source torrents and filesystem reflections and is operational reconciliation state.

Current schema: v5. Older incompatible state is intentionally rejected by the current implementation; follow the release-specific migration/rebuild instructions when upgrading.

The TorrServer FileStats source cache is separate from these output manifests. It does not authorize source removal and can be discarded/rebuilt without deleting Jellyfin or Kodi output trees.

Jellyfin and Kodi manifests are independent. Kodi state can never authorize TorrServer source removal.
