#!/usr/bin/env python3
"""Guarded node150 bridge migration. Root on the named Ubuntu host only.

NAT remains unchanged. Apply arms a 180s rollback and schedules netplan apply.
Confirm only after independent SSH/controller checks. Rollback is also manual.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

BASELINE = '970708daf1c44e6adfe1d2df7ae25e8b9bdb3787bad36d87da8bdbaf2be7a4ea'
CONFIG = Path('/etc/netplan/01-netcfg.yaml')
BACKUP = Path('/var/lib/vm-platform/backups/node150-bridge-v1')
UNIT = 'vm-platform-node150-bridge-rollback'


def command(args, timeout=25):
    return subprocess.check_output(args, timeout=timeout).decode().strip()


def atomic_write(path, content):
    temporary = path.with_name(path.name + '.vm-platform-tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def bridge_document(original):
    result = copy.deepcopy(original)
    network = result['network']
    assert network.get('bridges', {}).get('br0') is None, 'br0 already configured'
    ethernet = network['ethernets']['eno2np1']
    assert ethernet.get('addresses') == ['192.168.0.150/24'], 'Unexpected management address'
    assert ethernet.get('routes') == [{'to': 'default', 'via': '192.168.0.1'}], 'Unexpected default route'
    assert ethernet.get('nameservers', {}).get('addresses') == ['192.168.0.1', '8.8.8.8'], 'Unexpected DNS'
    bridge = {'interfaces': ['eno2np1'], 'dhcp4': False, 'dhcp6': False,
              'macaddress': 'ac:1f:6b:eb:a7:b1', 'parameters': {'stp': False, 'forward-delay': 0}}
    for key in ('addresses', 'routes', 'nameservers', 'gateway4', 'gateway6', 'routing-policy'):
        if key in ethernet:
            bridge[key] = ethernet.pop(key)
    ethernet.update(dhcp4=False, dhcp6=False)
    ethernet['link-local'] = []
    network.setdefault('bridges', {})['br0'] = bridge
    return result


def rollback():
    original = (BACKUP / '01-netcfg.yaml.original').read_bytes()
    assert hashlib.sha256(original).hexdigest() == BASELINE
    atomic_write(CONFIG, original)
    subprocess.run(['netplan', 'generate'], check=True, timeout=30)
    subprocess.run(['netplan', 'apply'], check=True, timeout=45)
    # Netplan can leave an unmanaged empty bridge after reverting a definition.
    links = json.loads(command(['ip', '-j', 'link', 'show']))
    physical = next(link for link in links if link['ifname'] == 'eno2np1')
    if physical.get('master') == 'br0':
        subprocess.run(['ip', 'link', 'set', 'eno2np1', 'nomaster'], check=True)
    addresses = json.loads(command(['ip', '-j', '-4', 'address', 'show', 'dev', 'eno2np1']))
    if not any(addr.get('local') == '192.168.0.150' for item in addresses for addr in item['addr_info']):
        subprocess.run(['ip', 'address', 'replace', '192.168.0.150/24', 'dev', 'eno2np1'], check=True)
        subprocess.run(['ip', 'route', 'replace', 'default', 'via', '192.168.0.1', 'dev', 'eno2np1'], check=True)
    print(json.dumps({'rolled_back': True, 'original_sha256': BASELINE}))


def confirm():
    addresses = json.loads(command(['ip', '-j', 'address', 'show']))
    bridge = next(item for item in addresses if item['ifname'] == 'br0')
    physical = next(item for item in addresses if item['ifname'] == 'eno2np1')
    assert any(item.get('local') == '192.168.0.150' and item.get('prefixlen') == 24 for item in bridge['addr_info'])
    assert not any(item.get('family') == 'inet' for item in physical['addr_info']), 'Physical port still has IPv4'
    assert physical.get('master') == 'br0', 'Physical port is not attached to bridge'
    route = json.loads(command(['ip', '-j', '-4', 'route', 'show', 'default']))
    assert len(route) == 1 and route[0]['gateway'] == '192.168.0.1' and route[0]['dev'] == 'br0'
    dns = command(['resolvectl', 'dns', 'br0'])
    assert '192.168.0.1' in dns and '8.8.8.8' in dns
    subprocess.run(['ping', '-I', 'br0', '-c', '2', '-W', '2', '192.168.0.1'], check=True, stdout=subprocess.DEVNULL, timeout=8)
    nat = command(['virsh', '-c', 'qemu:///system', 'net-info', 'default'])
    assert 'Active:         yes' in nat and 'Autostart:      yes' in nat
    subprocess.run(['systemctl', 'stop', UNIT + '.timer'], check=True, timeout=10)
    (BACKUP / 'confirmed.json').write_text(json.dumps({'time': int(time.time()), 'bridge': 'br0', 'address': '192.168.0.150/24', 'gateway': '192.168.0.1', 'dns': ['192.168.0.1', '8.8.8.8'], 'nat_preserved': True}) + '\n')
    print(json.dumps({'confirmed': True, 'backup': str(BACKUP), 'address': '192.168.0.150/24', 'interface': 'br0', 'nat_preserved': True, 'rollback_timer_cancelled': True}))


def apply():
    import yaml  # Ubuntu Netplan's installed python3-yaml; no platform runtime dependency.
    original = CONFIG.read_bytes()
    assert hashlib.sha256(original).hexdigest() == BASELINE, 'Netplan differs from approved baseline; abort'
    assert command(['virsh', '-c', 'qemu:///system', 'list', '--all', '--name']) == '', 'node150 must have no guest VMs during migration'
    links = json.loads(command(['ip', '-j', 'link', 'show']))
    assert not any(item['ifname'] == 'br0' for item in links), 'br0 already exists'
    physical = next(item for item in links if item['ifname'] == 'eno2np1')
    assert physical['address'] == 'ac:1f:6b:eb:a7:b1', 'Physical identity changed'
    result = bridge_document(yaml.safe_load(original))
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=False)
    saved = BACKUP / '01-netcfg.yaml.original'
    shutil.copy2(str(CONFIG), str(saved))
    os.chmod(str(saved), 0o600)
    shutil.copy2(__file__, str(BACKUP / 'prepare-node150-bridge.py'))
    # Arm recovery before changing any network configuration.
    subprocess.run(['systemd-run', '--unit=' + UNIT, '--on-active=180s', '--timer-property=AccuracySec=1s', '/usr/bin/python3', str(BACKUP / 'prepare-node150-bridge.py'), '--rollback'], check=True, timeout=15)
    try:
        atomic_write(CONFIG, (json.dumps(result, indent=2) + '\n').encode())
        subprocess.run(['netplan', 'generate'], check=True, timeout=30)
        subprocess.run(['systemd-run', '--unit=vm-platform-node150-bridge-apply', '--on-active=3s', '--timer-property=AccuracySec=1s', '/usr/sbin/netplan', 'apply'], check=True, timeout=15)
    except Exception:
        atomic_write(CONFIG, original)
        raise
    print(json.dumps({'scheduled': True, 'rollback_after_seconds': 180, 'backup': str(BACKUP), 'bridge': 'br0', 'preserved_address': '192.168.0.150/24', 'nat_preserved': True}))


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--confirm', action='store_true')
    modes.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    assert os.geteuid() == 0 and os.uname().nodename == 'sky', 'Run only as root on node150'
    if args.rollback:
        rollback()
    elif args.confirm:
        confirm()
    else:
        apply()


if __name__ == '__main__':
    main()
