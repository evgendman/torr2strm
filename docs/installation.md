# Installation

Current standalone package installs `/opt/torr2strm/torr2strm.py`, `/etc/torr2strm/config.toml`, and the systemd service/timer units.

Requirement: Python >= 3.11 and a reachable TorrServer.

The service is a systemd oneshot and the timer runs it periodically. The service uses `flock` to prevent overlapping synchronization runs.

The installer preserves an existing production config and preserves whether the timer was enabled before reinstall.
