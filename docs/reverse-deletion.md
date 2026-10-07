# Reverse deletion

Normal source removal is reflected from TorrServer into every enabled output.

Reverse deletion is intentionally one-way:

`managed Jellyfin STRM deleted` -> `if explicitly enabled` -> `TorrServer action=rem` -> `source disappears` -> `all stale reflections removed`.

Only Jellyfin is authoritative for this operation. Kodi/Elementum is permanently read-only with respect to source management.

`action=drop` is not used and is not implemented.

The current configuration also supports an explicit enable/disable switch and a maximum number of source removals per run.
