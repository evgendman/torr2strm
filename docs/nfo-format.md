# NFO format

Current NFO format: v2.

NFO format version is independent from software and manifest versions.

Generated NFOs contain `<torr2strm formatversion="2" />` inside the XML root. When a trustworthy TMDb ID is known, v2 appends one canonical Kodi Combination URL after the XML root: `/movie/<tmdbid>` for movies and `/tv/<tmdbid>` for TV roots. Episode NFOs remain XML-only.

Legacy/unversioned torr2strm NFOs are treated as v1 and migrated to v2 during normal synchronization without a new media probe. Existing useful metadata and stream details are preserved.

Future migrations must be explicit version-to-version transformations.
