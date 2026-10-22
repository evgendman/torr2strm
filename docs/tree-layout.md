# Tree layout

Each output tree has its own root, root marker and manifest. The default Jellyfin root is `/mnt/torr2strm-media`; the default Kodi/Elementum root is `/mnt/torr2strm-media-kodi`.

Both use the same top-level category and quality axes:

```text
<root>/
├── movie/
│   ├── 1080p/
│   └── 4K/
├── tv/
│   ├── 1080p/
│   └── 4K/
└── _uncategorized/
    ├── 1080p/
    └── 4K/
```

The category level comes only from TorrServer's `movie`/`tv` category field. Unknown categories remain under `_uncategorized`; anime is deliberately not forced into TV. Title text is never used by itself to guess movie versus TV.

For v1.4.7 candidate acceptance, use a clean rebuild instead of data migration: stop the service/timer and clear only the configured Jellyfin and Kodi generated output roots, including manifests and root markers. Keep TorrServer inventory and the configuration file intact.

The quality root is selected from usable video dimensions already embedded in TorrServer metadata (maximum dimension `>= 3840` means `4K`; lower dimensions mean `1080p`). Only when those dimensions are unavailable may an explicit `4K` or `2160p` release-title marker select `4K`; otherwise the root defaults to `1080p`. No NFO cache, external provider, local probe, or TorrServer `/ffp/` request can classify the root.

Within `tv`, content is organized below `Season NN`. When the season cannot be inferred, `[sync].tv_unmatched_season` controls the fallback (default `0`).

Jellyfin's existing TV folder layout is intentionally unchanged. When flattening files into a Season NN directory causes repeated basenames to target the same path, torr2strm resolves the entire collision group before writing: each participant receives a stable short-torrent-hash plus TorrServer file_id suffix. If a destination still collides, it retries with the full hash plus file ID. The final .strm component is limited to 255 UTF-8 bytes, preserving the identity suffix and source extension where possible. Ordinary non-colliding names remain unchanged, and torr2strm does not infer what episode a DVD/Blu-ray file contains.

Jellyfin output creates one STRM per playable video file. Kodi/Elementum creates torrent-level STRM for movies and file-level STRM for TV; TV playback passes the original zero-based FileStats order as `oindex`.

Every generated Kodi path component—including normalized movie/series directories and nested folders retained from unidentified torrents—is Windows/SMB-safe and bounded to at most 255 UTF-8 bytes. Short names stay unchanged; long components are truncated only at UTF-8 boundaries and receive a deterministic identity suffix. Collisions are resolved after sanitization and truncation. STRM/NFO basename helpers still preserve extensions and the quality/hash suffix contract. This is a per-component limit, not a guarantee about the maximum total Windows path length.

For Kodi TV, identified series have one `tvshow.nfo` at the series root. No season-folder or per-episode NFO sidecars are written. Kodi movie NFOs keep the existing format; Jellyfin's per-item NFO output is unchanged.
