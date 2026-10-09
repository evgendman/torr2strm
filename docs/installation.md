# Installation and prerequisites

The standalone package installs `/opt/torr2strm/torr2strm.py`, `/etc/torr2strm/config.toml`, and the systemd service/timer units.

Requirements:

- Python 3.11 or newer;
- a reachable TorrServer HTTP API;
- a working `ffprobe` binary available to TorrServer for technical media probing.

## Verify TorrServer ffprobe

`torr2strm` does not execute or install ffprobe on the local host. When a usable cached NFO or exact JacRed ffprobe payload is not available, it requests media-stream JSON from TorrServer using `GET /ffp/{hash}/{file_id}`. The binary must therefore be configured and usable on the TorrServer host, and the TorrServer endpoint must be reachable from torr2strm.

TorrServer documents `GET /ffp/status` as the availability check. For the default local deployment in the example configuration:

```bash
curl -i http://127.0.0.1:8097/ffp/status
```

Use the actual host and port from `[torrserver].url`. The current torr2strm client does not expose TorrServer HTTP Basic Auth credentials in its config. If endpoints are protected by authentication, ensure the client can reach them through a supported deployment arrangement.

JacRed is optional. The public `https://jac.red` service is the configured example/test backend. An empty `[jacred].url` or `--no-jacred` disables enrichment; quality probing can still use TorrServer.

The service is a systemd oneshot and the timer runs it periodically. The service uses `flock` to prevent overlapping synchronization runs.

The installer preserves an existing production config and preserves whether the timer was enabled before reinstall.

See [configuration reference](configuration.md) for every supported TOML key and command-line option.
