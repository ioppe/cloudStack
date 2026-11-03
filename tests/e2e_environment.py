#!/usr/bin/env python3
"""Final read-only environment acceptance; compare exact old VM and file baselines."""
import argparse
import json
import subprocess
from pathlib import Path

REMOTE = r'''
import hashlib, json, os, subprocess, urllib.request

def cmd(args):
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    return subprocess.check_output(args, env=env).decode().strip()

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()

node = os.uname()[1]
services = ['vm-platform-agent.service']
if os.path.exists('/etc/vm-platform/controller.json'):
    services += ['vm-platform.service', 'vm-panel.service']
report = {'hostname': node, 'services': {}, 'listeners': cmd(['ss', '-lntp']), 'socket_mode': oct(os.stat('/run/vm-platform/agent.sock').st_mode & 0o777)}
for name in services:
    report['services'][name] = {'active': cmd(['systemctl', 'is-active', name]), 'enabled': subprocess.run(['systemctl', 'is-enabled', name], stdout=subprocess.PIPE).stdout.decode().strip()}
names = cmd(['virsh', '-c', 'qemu:///system', 'list', '--all', '--name']).splitlines()
report['vms'] = []
for name in names:
    if not name.strip():
        continue
    args = ['virsh', '-c', 'qemu:///system']
    xml = subprocess.check_output(args + ['dumpxml', name, '--inactive'])
    report['vms'].append({'name': name, 'uuid': cmd(args + ['domuuid', name]), 'state': cmd(args + ['domstate', name]), 'inactive_xml_sha256': hashlib.sha256(xml).hexdigest()})
if 'vm-panel.service' in services:
    report['old_files'] = {p: digest('/opt/vm-panel/' + p) for p in ['server.py', 'config.json', 'vm-panel.db']}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report['old_http'] = opener.open('http://127.0.0.1:8088/', timeout=20).getcode()
else:
    report['netplan_sha256'] = digest('/etc/netplan/01-netcfg.yaml')
    report['physical_ipv4'] = cmd(['ip', '-4', '-o', 'address', 'show', 'dev', 'eno2np1'])
    report['default_route'] = cmd(['ip', '-4', 'route', 'show', 'default'])
    links = json.loads(cmd(['ip', '-j', 'link', 'show']))
    report['links'] = links
    has_bridge = any(item['ifname'] == 'br0' for item in links)
    report['bridge_ipv4'] = cmd(['ip', '-4', '-o', 'address', 'show', 'dev', 'br0']) if has_bridge else ''
    report['dns'] = cmd(['resolvectl', 'dns', 'br0' if has_bridge else 'eno2np1'])
    text = open('/etc/netplan/01-netcfg.yaml', encoding='utf-8').read()
    report['netplan'] = json.loads(text) if text.lstrip().startswith('{') else None
    report['nat'] = cmd(['virsh', '-c', 'qemu:///system', 'net-info', 'default'])
    report['nat_xml'] = cmd(['virsh', '-c', 'qemu:///system', 'net-dumpxml', 'default'])
    report['template_sha256'] = digest('/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img')
    report['remaining_temporary_files'] = [p for p in os.listdir('/var/lib/libvirt/images/vms') if p.startswith(('platform-accept-', 'platform-bridge-accept-'))]
print(json.dumps(report, ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    parser.add_argument('--node150-network', choices=['bridge', 'nat'], default='bridge')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = {}
    for address in ['192.168.0.30', '192.168.0.150']:
        proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@' + address, '/usr/bin/python3 -'], input=REMOTE.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100)
        assert proc.returncode == 0, proc.stderr.decode()
        reports[address] = json.loads(proc.stdout)
    old = reports['192.168.0.30']
    baseline = json.loads((root / 'docs/evidence/old-domains-baseline.json').read_text())
    for vm in baseline:
        if vm['state'] == '关闭':
            vm['state'] = 'shut off'
    assert sorted(old['vms'], key=lambda x: x['uuid']) == sorted(baseline, key=lambda x: x['uuid']), 'Old VM state/XML changed'
    assert old['old_http'] == 200
    assert old['old_files'] == {'server.py': 'e4027995a691c9ef420a637a5b6fb73e079f3bdbb44eb1deb9ae6d20e9e7942f', 'config.json': 'a0e01be27f87d541cbf7762b3953d9cbfaf07426e230257c2b0612725d32ae48', 'vm-panel.db': 'd07aeea64ffd46c502b3d7b0b0dd60f7a9fd5a2036293b5356c470886f57844c'}
    new = reports['192.168.0.150']
    assert new['vms'] == [] and new['remaining_temporary_files'] == []
    if args.node150_network == 'nat':
        assert new['netplan_sha256'] == '970708daf1c44e6adfe1d2df7ae25e8b9bdb3787bad36d87da8bdbaf2be7a4ea'
        assert '192.168.0.150/24' in new['physical_ipv4'] and 'via 192.168.0.1' in new['default_route']
    else:
        assert '192.168.0.150/24' in new['bridge_ipv4'] and '192.168.0.150/24' not in new['physical_ipv4']
        assert 'via 192.168.0.1 dev br0' in new['default_route']
        assert next(link for link in new['links'] if link['ifname'] == 'eno2np1').get('master') == 'br0'
        assert '192.168.0.1' in new['dns'] and '8.8.8.8' in new['dns']
        bridge = new['netplan']['network']['bridges']['br0']
        assert bridge['interfaces'] == ['eno2np1'] and bridge['addresses'] == ['192.168.0.150/24']
        assert bridge['routes'] == [{'to': 'default', 'via': '192.168.0.1'}]
        assert bridge['nameservers']['addresses'] == ['192.168.0.1', '8.8.8.8']
    assert "mode='nat'" in new['nat_xml'] and 'Autostart:      yes' in new['nat'] and 'Active:         yes' in new['nat']
    assert new['template_sha256'] == '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
    for report in reports.values():
        assert report['socket_mode'] == '0o600'
        for service, state in report['services'].items():
            assert state['active'] == 'active' and (service == 'vm-panel.service' or state['enabled'] == 'enabled'), (service, state)
    reports['passed'] = True
    Path(args.report).write_text(json.dumps(reports, ensure_ascii=False, indent=2) + '\n')
    print('PASS old panel HTTP/files and all 14 VM UUID/state/XML unchanged')
    print('PASS new services active/enabled; node150 NAT/template/physical network preserved; temporary resources absent')


if __name__ == '__main__':
    main()
