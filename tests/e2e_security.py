#!/usr/bin/env python3
"""Read-only security evidence; never prints credential values or private key content."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from api_client import Client


def remote(address, code):
    proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@' + address, '/usr/bin/python3 -'], input=code.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=55)
    assert proc.returncode == 0, 'Security metadata query failed on ' + address
    return json.loads(proc.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    client = Client('http://192.168.0.30:8089', str(root / 'ADMIN-CREDENTIALS.txt'))
    client.login()
    report = {}
    secret = client.password.encode()
    files = [p for p in (root / 'static').rglob('*') if p.is_file()]
    report['frontend_secret_hits'] = sum(secret in p.read_bytes() for p in files)
    report['frontend_private_key_headers'] = sum(b'-----BEGIN OPENSSH PRIVATE KEY-----' in p.read_bytes() for p in files)
    assert report['frontend_secret_hits'] == report['frontend_private_key_headers'] == 0
    proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@192.168.0.30', 'journalctl -u vm-platform.service -u vm-platform-agent.service --no-pager -o cat'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
    assert proc.returncode == 0
    report['service_log_secret_hits'] = proc.stdout.count(secret)
    report['service_log_private_key_headers'] = proc.stdout.count(b'-----BEGIN OPENSSH PRIVATE KEY-----')
    assert report['service_log_secret_hits'] == report['service_log_private_key_headers'] == 0
    control = remote('192.168.0.30', r'''
import json,os,subprocess
paths=['/etc/vm-platform/controller.json','/etc/vm-platform/agent.json','/etc/vm-platform/id_ed25519','/etc/vm-platform/known_hosts']
config=json.load(open('/etc/vm-platform/agent.json',encoding='utf-8'))
command=['ssh','-T','-i','/etc/vm-platform/id_ed25519','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/etc/vm-platform/known_hosts','root@192.168.0.150','id']
p=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
print(json.dumps({'private_modes':{path:oct(os.stat(path).st_mode & 0o777) for path in paths},'socket_mode':oct(os.stat('/run/vm-platform/agent.sock').st_mode & 0o777),'protected_uuids':config.get('read_only_uuids',[]),'restricted_ssh_exit':p.returncode,'restricted_ssh_stdout_empty':not p.stdout,'restricted_ssh_refused':'拒绝' in p.stderr.decode('utf-8','replace')}))
''')
    assert set(control['private_modes'].values()) == {'0o600'} and control['socket_mode'] == '0o600'
    assert control['restricted_ssh_exit'] == 2 and control['restricted_ssh_stdout_empty'] and control['restricted_ssh_refused']
    baseline = json.loads((root / 'docs/evidence/old-domains-baseline.json').read_text())
    assert set(control['protected_uuids']) == {v['uuid'] for v in baseline}
    report['controller'] = control
    guest = client.call('GET', '/api/hosts/node150/vms')['vms']
    assert len(guest) == 1 and guest[0]['name'] == 'platform-accept-20261103' and guest[0]['state'] == 'running'
    vm_uuid = guest[0]['uuid']
    node = remote('192.168.0.150', r'''
import json,os,subprocess,xml.etree.ElementTree as ET
uuid_text=UUID_VALUE
xml=subprocess.check_output(['virsh','-c','qemu:///system','dumpxml',uuid_text])
graphics=ET.fromstring(xml).find('./devices/graphics')
listen=graphics.get('listen') or graphics.find('listen').get('address')
port=int(graphics.get('port'))
lines=subprocess.check_output(['ss','-lntp']).decode().splitlines()
selected=[line for line in lines if (':'+str(port)+' ') in line]
print(json.dumps({'uuid':uuid_text,'vnc_listen':listen,'vnc_port':port,'vnc_listener_lines':selected,'agent_socket_mode':oct(os.stat('/run/vm-platform/agent.sock').st_mode & 0o777),'agent_config_mode':oct(os.stat('/etc/vm-platform/agent.json').st_mode & 0o777)}))
'''.replace('UUID_VALUE', repr(vm_uuid)))
    assert node['vnc_listen'] == '127.0.0.1' and node['agent_socket_mode'] == node['agent_config_mode'] == '0o600'
    assert node['vnc_listener_lines'] and all('127.0.0.1:' + str(node['vnc_port']) in line for line in node['vnc_listener_lines'])
    report['node150'] = node
    report['passed'] = True
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('PASS current credential/private keys absent from frontend and service logs; private files/socket mode 0600')
    print('PASS restricted SSH rejects arbitrary command; original 14 UUIDs protected; new VNC listens only on 127.0.0.1')


if __name__ == '__main__':
    main()
