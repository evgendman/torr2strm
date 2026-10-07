# Tree layout

Jellyfin default root:

`movie/1080p`, `movie/4K`, `tv/1080p`, `tv/4K`.

TV content is organized below `Season NN`.

Kodi/Elementum uses an independent root with the same broad category/quality structure but different STRM URI semantics. Movies are torrent-level STRM; TV is file-level STRM with the original zero-based FileStats order passed as `oindex`.

Unknown category remains under `_uncategorized`; title text is never used to guess movie versus TV.

Long UTF-8 basenames are bounded safely and use a stable identity suffix when truncation is necessary.
