# Architecture

Source of truth:
TorrServer

Processing pipeline:
TorrServer enumeration -> FileStats -> optional exact JacRed enrichment -> media-info resolution -> independent output reconciliation.

Media-info priority:
1. usable existing NFO cache
2. exact JacRed match with usable ffprobe payload
3. TorrServer ffprobe

Category priority:
1. explicit TorrServer movie/tv category
2. exact JacRed match only when TorrServer category is blank
3. uncategorized when category remains unknown

JacRed anime remains uncategorized; it is never forced into TV.

Quality:
max video dimension >= 2160 maps to 4K. Everything below 2160 maps to 1080p. Release-title quality tokens are not used as a fallback.

Outputs:
Jellyfin is the authoritative read/write projection.
Kodi/Elementum is an independent read-only projection with different STRM URI semantics.

Source removal:
TorrServer disappearance is reflected in every enabled output. A user deletion can trigger source removal only from the authoritative Jellyfin projection and only when the explicit reverse-deletion setting is enabled.