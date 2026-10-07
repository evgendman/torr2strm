# Goals

`torr2strm` is a synchronization/materialization layer between TorrServer and filesystem-based media consumers.

Primary goals:
- keep TorrServer as source of truth;
- expose selected content as normal media-library trees;
- provide STRM playback references instead of copying media;
- provide reliable NFO identity and technical stream information;
- support independent consumer-specific projections;
- support deterministic synchronization and source-removal reflection;
- support controlled reverse deletion from the authoritative Jellyfin projection.

Non-goals: torrent downloading, media transcoding, duplicate media storage, or becoming a general metadata scraper.
