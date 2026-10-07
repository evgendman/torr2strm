# Read-only outputs

The Kodi/Elementum output is a consumer-specific projection, not a source-management interface.

Deleting a Kodi STRM must never trigger TorrServer `action=rem`, source deletion, or any authorization decision affecting the Jellyfin projection.

The tree may simply be regenerated from TorrServer state on a later synchronization.
