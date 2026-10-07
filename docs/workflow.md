# Synchronization workflow

Conceptual run:

1. Load configuration.
2. Enumerate TorrServer.
3. Prepare FileStats/source metadata.
4. Optionally perform JacRed enrichment.
5. Require exact BTIH/infoHash match for JacRed data.
6. Resolve category.
7. Resolve usable media information.
8. Classify 1080p/4K from real dimensions.
9. Build desired independent output projections.
10. Reconcile manifests and filesystem state.
11. Generate or migrate NFO.
12. Write STRM references.
13. Remove stale reflections of disappeared TorrServer sources.
14. Process authoritative Jellyfin reverse deletions when enabled.
15. Persist independent manifests.

Per-torrent recoverable failures are logged and do not make the oneshot exit non-zero. Fatal configuration/source errors may fail the service.
