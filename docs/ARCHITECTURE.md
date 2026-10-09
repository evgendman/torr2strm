# Architecture

## Source of truth and processing path

TorrServer is the source of truth for torrent membership, info hash and FileStats. A synchronization pass enumerates TorrServer, prepares a stable snapshot of each torrent and its files, optionally enriches that snapshot through JacRed, resolves technical stream information and reconciles independent output trees.

```text
TorrServer torrent list + FileStats
        ↓
optional JacRed search + exact BTIH/infoHash validation
        ↓
resolve category and provider IDs
        ↓
reuse valid NFO → usable ffprobe payload from exact JacRed match → TorrServer /ffp/
        ↓
classify primary video as 4K or 1080p
        ↓
materialize Jellyfin and Kodi/Elementum trees independently
```

## JacRed matching

JacRed is optional enrichment, never a substitute for TorrServer's torrent inventory. torr2strm searches `/api/v1/search` using up to six deduplicated candidates derived from torrent/series title and known title metadata. Search type is `tvsearch` for TV, `movie` for movies and generic `search` when the TorrServer category is unknown.

A result is accepted only if its `infoHash`, `guid` or BTIH extracted from its magnet/download URL exactly matches the TorrServer hash. Approximate title similarity alone is not sufficient. When more than one exact result is available, the code ranks results by usable ffprobe payload, magnet availability and recognized category, and keeps enrichment data from the selected result together.

## Category resolution

1. A non-empty TorrServer category equal to `movie` or `tv` after normalization wins.
2. JacRed category may be used only when the TorrServer category field is blank and the result hash is an exact match.
3. If the category remains unknown, use `_uncategorized`.

Anime stays `_uncategorized`; it is never forced into TV. A non-empty but unsupported TorrServer category is treated as `_uncategorized` and is not overridden by JacRed. The code does not guess category from a title string.

Category determines the first directory level (`movie`, `tv` or `_uncategorized`). The quality directory is a separate decision (`4K` or `1080p`).

## Media information and quality

Media-info priority:

1. Usable existing NFO from either output tree.
2. Exact JacRed hash match with a usable ffprobe stream payload.
3. TorrServer's `GET /ffp/{hash}/{file_id}` endpoint.

torr2strm does not execute a local ffprobe process. TorrServer must have a working ffprobe binary and provide the `/ffp/...` endpoint. `GET /ffp/status` is the documented availability check. The `[quality].timeout_sec` and `[quality].retries` settings control TorrServer ffprobe requests.

Quality comes from the primary eligible video's real dimensions, not release-name tokens:

- `max(width, height) >= 2160` → `4K`;
- all smaller dimensions → `1080p`.

A missing/unusable stream payload is not replaced by the words `1080p`, `2160p`, `quality` or `videotype` in a title/JacRed record.

## Output responsibility

Jellyfin is the authoritative read/write projection. Kodi/Elementum is an independent read-only projection with different STRM URI semantics. Each output has its own root, marker and manifest. Source-torrent removal via reverse deletion is only possible from the Jellyfin output and only when explicitly enabled; `action=drop` is not used.
