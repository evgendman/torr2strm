#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ "$(id -u)" -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }
command -v python3 >/dev/null
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python >= 3.11 is required")
PY

TIMER_WAS_ENABLED=0
if systemctl is-enabled --quiet torr2strm.timer 2>/dev/null; then
  TIMER_WAS_ENABLED=1
fi
systemctl stop torr2strm.timer >/dev/null 2>&1 || true
systemctl stop torr2strm.service >/dev/null 2>&1 || true

install -d -m 0755 /opt/torr2strm /etc/torr2strm
install -m 0755 "$SRC_DIR/torr2strm.py" /opt/torr2strm/torr2strm.py
install -m 0644 "$SRC_DIR/etc/config.toml.example" /etc/torr2strm/config.toml.example
install -m 0644 "$SRC_DIR/systemd/torr2strm.service" /etc/systemd/system/torr2strm.service
install -m 0644 "$SRC_DIR/systemd/torr2strm.timer" /etc/systemd/system/torr2strm.timer

if [[ ! -e /etc/torr2strm/config.toml ]]; then
  install -m 0644 "$SRC_DIR/etc/config.toml.example" /etc/torr2strm/config.toml
else
  chmod 0644 /etc/torr2strm/config.toml
fi

systemctl daemon-reload
if [[ "$TIMER_WAS_ENABLED" -eq 1 ]]; then
  systemctl enable torr2strm.timer >/dev/null
else
  systemctl disable torr2strm.timer >/dev/null 2>&1 || true
fi

printf 'Installed torr2strm v1.4.5\n'
printf 'Code:   /opt/torr2strm/torr2strm.py\n'
printf 'Config: /etc/torr2strm/config.toml\n'
printf 'Metadata enrichment: external lookups disabled; TorrServer data only\n'
