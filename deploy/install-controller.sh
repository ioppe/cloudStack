#!/bin/sh
set -eu
cd /opt/vm-platform
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
[ -f controller.py ] && [ -f static/index.html ]
[ -f /etc/vm-platform/controller.json ] || { echo 'Prepare private controller.json first' >&2; exit 1; }
[ -f /etc/vm-platform/id_ed25519 ] && [ -f /etc/vm-platform/known_hosts ]
install -d -m 700 /var/lib/vm-platform /etc/vm-platform
chmod 600 /etc/vm-platform/controller.json /etc/vm-platform/id_ed25519 /etc/vm-platform/known_hosts
install -m 644 deploy/vm-platform.service /etc/systemd/system/vm-platform.service
systemctl daemon-reload
systemctl enable --now vm-platform.service
systemctl is-active vm-platform.service
systemctl is-enabled vm-platform.service
