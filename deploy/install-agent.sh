#!/bin/sh
# Run as root from a release checkout already copied to /opt/vm-platform.
set -eu
cd /opt/vm-platform
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
# v1.1.2：ipdiscovery.py 是 vmops 的硬依赖（局域网 ARP 地址发现），缺失直接失败。
[ -f worker.py ] && [ -f vmops.py ] && [ -f ipdiscovery.py ] && [ -f deploy/vm-platform-agent.service ]
[ -f /etc/vm-platform/agent.json ] || { echo 'Prepare /etc/vm-platform/agent.json first' >&2; exit 1; }
install -d -m 700 /var/lib/vm-platform /etc/vm-platform
chmod 600 /etc/vm-platform/agent.json
install -m 644 deploy/vm-platform-agent.service /etc/systemd/system/vm-platform-agent.service
systemctl daemon-reload
systemctl enable --now vm-platform-agent.service
/usr/bin/python3 - <<'PY'
import json, os, socket, time
with open('/etc/vm-platform/agent.json') as source:
    path = json.load(source).get('socket_path', '/run/vm-platform/agent.sock')
for attempt in range(50):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
        break
    except OSError:
        if attempt == 49:
            raise SystemExit('Agent socket did not become ready')
        time.sleep(0.1)
    finally:
        connection.close()
PY
systemctl is-active vm-platform-agent.service
systemctl is-enabled vm-platform-agent.service
