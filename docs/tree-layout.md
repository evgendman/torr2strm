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

The category level comes from TorrServer's `movie`/`tv` category, or from JacRed only when TorrServer's category field is blank and JacRed has an exact hash match. Unknown categories remain under `_uncategorized`; anime is deliberately not forced into TV. Title text is never used by itself to guess movie versus TV.

The quality level is based on real video dimensions, using an existing valid NFO cache, a usable ffprobe payload from an exact JacRed match or TorrServer's `/ffp/{hash}/{file_id}` endpoint. Maximum dimension `>= 2160` maps to `4K`; all smaller dimensions map to `1080p`. Release-name quality labels are not a fallback.

Within `tv`, content is organized below `Season NN`. When the season cannot be inferred, `[sync].tv_unmatched_season` controls the fallback (default `0`).

Jellyfin output creates one STRM per playable video file. Kodi/Elementum creates torrent-level STRM for movies and file-level STRM for TV; TV playback passes the original zero-based FileStats order as `oindex`.

Long UTF-8 basenames are bounded safely and use a stable identity suffix when truncation is necessary.
