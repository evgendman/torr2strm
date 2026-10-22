# Installation and upgrade

The standalone package installs `/opt/torr2strm/torr2strm.py`, the example and production TOML files under `/etc/torr2strm/`, and the systemd service/timer units.

Requirements: Python 3.11 or newer, systemd, and a reachable TorrServer HTTP API. torr2strm does not run local ffprobe, request TorrServer `/ffp/`, or call external metadata providers.

## Upgrade to v1.4.8

This is an in-place code/configuration upgrade. It does not require deleting or rebuilding output trees for a schema migration. Preserve `/etc/torr2strm/config.toml`, both output roots, and TorrServer data. The new `[notifications]` section is optional and disabled by default; merge it into the production config only when enabling Apprise.

1. Download the reviewed source, compile it and run tests:

   ```bash
   set -euo pipefail
   SRC="$(mktemp -d /tmp/torr2strm-v1.4.8.XXXXXX)"
   git clone https://github.com/evgendman/torr2strm.git "$SRC"
   git -C "$SRC" checkout work/torr2strm-v1.4.8-quality-recovery-apprise
   python3 -m compileall -q "$SRC/torr2strm.py" "$SRC/tests"
   (cd "$SRC" && python3 -m pytest -q)
   python3 "$SRC/torr2strm.py" --version
   printf '%s\n' "$SRC" > /tmp/torr2strm-v1.4.8-source-path
   ```

   After the changes pass CI and are published, use the official `v1.4.8` tag instead of the review branch.

2. Back up the current program, configuration and units under `/tmp`, record timer state, then stop the timer and wait for a running oneshot:

   ```bash
   set -euo pipefail
   BACKUP="$(mktemp -d /tmp/torr2strm-backup.XXXXXX)"
   chmod 0700 "$BACKUP"
   sudo cp -a /opt/torr2strm/torr2strm.py "$BACKUP/"
   sudo cp -a /etc/torr2strm "$BACKUP/config"
   sudo cp -a /etc/systemd/system/torr2strm.service /etc/systemd/system/torr2strm.timer "$BACKUP/"
   if systemctl is-enabled --quiet torr2strm.timer 2>/dev/null; then echo enabled > "$BACKUP/timer-state"; else echo disabled > "$BACKUP/timer-state"; fi
   printf '%s\n' "$BACKUP" | sudo tee /tmp/torr2strm-v1.4.8-backup-path >/dev/null
   sudo systemctl stop torr2strm.timer
   while systemctl is-active --quiet torr2strm.service; do sleep 1; done
   ```

3. Install the new source and units. The installer preserves the existing production configuration and prior timer enablement; it does not clear media roots:

   ```bash
   SRC="$(cat /tmp/torr2strm-v1.4.8-source-path)"
   sudo "$SRC/install.sh"
   /usr/bin/python3 /opt/torr2strm/torr2strm.py --version
   sudo cmp "$SRC/torr2strm.py" /opt/torr2strm/torr2strm.py
   ```

4. Only after confirming that the shared gateway has the `arr-stack` key, merge these lines into `/etc/torr2strm/config.toml`. Do not replace the existing config:

   ```toml
   [notifications]
   enabled = true
   apprise_url = "http://192.168.0.14:8000"
   apprise_config_key = "arr-stack"
   service_name = "torr2strm / MINI-Ubuntu"
   timeout_sec = 5
   tag_warning = "warnings"
   tag_critical = "critical"
   state_path = "/var/lib/torr2strm/notification_state.json"
   ```

   Leave `enabled = false` until the first dry run succeeds and the endpoint/key are confirmed. Telegram tokens, chat IDs and topic IDs stay in Apprise. Fingerprints are stored separately from media manifests and prevent identical messages from repeating every five minutes.

5. Validate configuration via dry run before starting the timer:

   ```bash
   sudo /usr/bin/python3 /opt/torr2strm/torr2strm.py --config /etc/torr2strm/config.toml --dry-run
   sudo systemctl daemon-reload
   sudo systemctl start torr2strm.timer
   systemctl list-timers torr2strm.timer --all --no-pager
   sudo journalctl -u torr2strm.service -n 100 --no-pager
   ```

   The dry run does not write output files or remove source torrents. The existing timer remains the standalone scheduler; no torr2jellyfin integration is installed.

## Rollback

Stop `torr2strm.timer`, wait for `torr2strm.service` to finish, and restore source/config/unit backups from the path saved in `/tmp/torr2strm-v1.4.8-backup-path`. Run `sudo systemctl daemon-reload`; restore the prior timer state and start it only if it was running before the upgrade. Do not delete or replace either generated output root, either manifest, TorrServer data, or the notification fingerprint file during a code rollback.
