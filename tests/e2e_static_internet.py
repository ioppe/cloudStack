#!/usr/bin/env python3
"""Dedicated node150 guest acceptance; existing VMs are read-only throughout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

HOST = '192.168.0.150'
NAME = 'platform-static-net-accept-20261103'
DISK_DIR = '/home/vm-disks'
PROFILE = {'cidr': '192.168.0.0/24', 'gateway': '192.168.0.1', 'dns': ['192.168.0.1', '8.8.8.8']}
BASELINE = Path(__file__).resolve().parents[1] / 'docs/evidence/static-internet-baseline.json'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def remote(code, host=HOST, timeout=40):
    proc = subprocess.run(SSH + ['root@' + host, '/usr/bin/python3 -'], input=code.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    assert proc.returncode == 0, 'Read-only SSH probe failed on ' + host + ': ' + proc.stderr.decode(errors='replace')[-800:]
    return json.loads(proc.stdout)


def rpc(operation, payload=None, expect_ok=True):
    request = json.dumps({'operation': operation, 'payload': payload or {}}).encode() + b'\n'
    proc = subprocess.run(SSH + ['root@' + HOST, '/usr/bin/python3 /opt/vm-platform/worker.py --rpc'], input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=160)
    assert proc.returncode == 0, 'Private agent transport failed'
    response = json.loads(proc.stdout)
    if expect_ok:
        assert response.get('ok'), str(response.get('error', 'Agent operation failed'))
        return response['data']
    return response


def existing_vms(host):
    return remote("""import hashlib,json,subprocess

def virsh(*args):
    return subprocess.check_output(['virsh','-c','qemu:///system']+list(args))

rows=[]
for uuid in virsh('list','--all','--uuid').decode().split():
    rows.append({'uuid':uuid,'name':virsh('domname',uuid).decode().strip(),'state':virsh('domstate',uuid).decode().strip(),'inactive_xml_sha256':hashlib.sha256(virsh('dumpxml',uuid,'--inactive')).hexdigest()})
print(json.dumps(sorted(rows,key=lambda row:row['uuid'])))
""", host=host)


def assert_baseline():
    baseline = json.loads(BASELINE.read_text())
    result = {}
    for host, expected in baseline.items():
        actual = existing_vms(host)
        assert actual == sorted(expected['existing_vms'], key=lambda row: row['uuid']), 'Existing VM state/configuration changed on ' + host
        result[host] = {'existing_vm_count': len(actual), 'states_and_inactive_xml_hashes_unchanged': True}
    return result


def probe_address():
    # RFC 5227-style probes use sender IP 0.0.0.0 and never configure an address.
    return remote("""import json,socket,struct,time
iface='br0'
mac=bytes.fromhex(open('/sys/class/net/'+iface+'/address').read().strip().replace(':',''))
sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0806))
sock.bind((iface,0)); sock.settimeout(0.2)
probes=[]
for candidate in ['192.168.0.248','192.168.0.247','192.168.0.246']:
    target=socket.inet_aton(candidate)
    arp=struct.pack('!HHBBH',1,0x0800,6,4,1)+mac+b'\\x00'*4+b'\\x00'*6+target
    frame=b'\\xff'*6+mac+struct.pack('!H',0x0806)+arp
    conflicts=[]
    for attempt in range(3):
        sock.send(frame)
        deadline=time.monotonic()+1.0
        while time.monotonic()<deadline:
            try: packet=sock.recv(2048)
            except socket.timeout: continue
            if len(packet)<42 or packet[12:14]!=b'\\x08\\x06' or packet[22:28]==mac: continue
            if packet[28:32]==target or (packet[38:42]==target and packet[28:32]==b'\\x00'*4):
                conflicts.append(':'.join('%02x'%b for b in packet[22:28]))
    probes.append({'address':candidate,'arp_probe_count':3,'conflicting_macs':sorted(set(conflicts))})
    if not conflicts:
        print(json.dumps({'selected':candidate,'interface':iface,'probes':probes})); break
else: raise RuntimeError('No unoccupied acceptance address found')
sock.close()
""", timeout=25)


def own_files(name):
    return remote('import json,os; print(json.dumps([p for p in ' + repr([DISK_DIR + '/' + name + '.qcow2', DISK_DIR + '/' + name + '-seed.iso']) + ' if os.path.lexists(p)]))')


GUEST_PROBE = """import getpass,json,os,socket,ssl,subprocess
finished=os.path.isfile('/var/lib/cloud/instance/boot-finished')
result={'hostname':socket.gethostname(),'user':getpass.getuser(),'cloud_init_finished':finished}
if finished:
    result['addresses']=json.loads(subprocess.check_output(['ip','-j','-4','address','show']).decode())
    result['links']=json.loads(subprocess.check_output(['ip','-j','link','show']).decode())
    result['routes']=json.loads(subprocess.check_output(['ip','-j','-4','route','show']).decode())
    result['dns']=subprocess.check_output(['resolvectl','dns']).decode().strip()
    attempts=[]
    for hostname in ['example.com','www.baidu.com']:
        addresses=[]
        try:
            addresses=sorted(set(row[4][0] for row in socket.getaddrinfo(hostname,443,socket.AF_INET,socket.SOCK_STREAM)))
            for address in addresses[:3]:
                try:
                    with socket.create_connection((address,443),8) as tcp:
                        with ssl.create_default_context().wrap_socket(tcp,server_hostname=hostname) as tls:
                            tls.sendall(('HEAD / HTTP/1.1\\r\\nHost: '+hostname+'\\r\\nConnection: close\\r\\n\\r\\n').encode())
                            status=tls.recv(2048).split(b'\\r\\n',1)[0].decode('ascii','replace')
                            assert status.startswith('HTTP/') and 200<=int(status.split()[1])<400,status
                            result['external']={'hostname':hostname,'resolved_ipv4':addresses,'connected_ipv4':address,'tcp_port':443,'certificate_verified':True,'http_status_line':status}
                    break
                except Exception as exc:
                    attempts.append({'hostname':hostname,'address':address,'error':type(exc).__name__})
            if result.get('external'): break
        except Exception as exc:
            attempts.append({'hostname':hostname,'resolved_ipv4':addresses,'error':type(exc).__name__})
    result['external_attempts']=attempts
print(json.dumps(result))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report_path = Path(args.report)
    report = {'passed': False, 'checks': [], 'time': int(time.time()), 'scope': 'Dedicated temporary node150 VM only; no HTTP login/password changes'}

    def record(check, data):
        report['checks'].append({'check': check, 'evidence': data})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    record('existing VM baseline matches before testing', assert_baseline())
    networks = rpc('networks')
    assert networks['static_network_defaults'].get('br0') == PROFILE
    record('node150 exposes the explicit br0 static network profile', networks['static_network_defaults'])
    assert not any(vm['name'] in (NAME, NAME + '-invalid') for vm in rpc('vms')['vms'])
    assert own_files(NAME) == [] and own_files(NAME + '-invalid') == [], 'Acceptance paths already exist'
    key = Path(os.environ['PI_SCRATCH_DIR']) / 'vm-platform-static-internet-guest'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'vm-platform-static-internet-only', '-f', str(key)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    public_key = Path(str(key) + '.pub').read_text().strip()
    payload = {'name': NAME, 'kind': 'disk', 'source': '/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img', 'disk_dir': DISK_DIR, 'vcpus': 2, 'memory_gb': 2, 'disk_gb': 8, 'network_type': 'bridge', 'network_source': 'br0', 'cloud_init_enabled': True, 'ci_username': 'netcheck', 'ci_ssh_key': public_key, 'ci_dhcp': False}
    negative = dict(payload, name=NAME + '-invalid', ci_ip='198.18.0.248/24')
    rejected = rpc('create', negative, expect_ok=False)
    assert rejected.get('ok') is False and '网关' in str(rejected.get('error', '')), 'Missing matching profile was not rejected'
    assert own_files(NAME + '-invalid') == []
    assert not any(vm['name'] == NAME + '-invalid' for vm in rpc('vms')['vms'])
    record('unmatched subnet is rejected before any disk seed or domain exists', {'error': rejected.get('error'), 'remaining_files': []})
    probe = probe_address()
    address = probe['selected']
    assert address != '192.168.0.160'
    record('temporary guest address passes three ARP conflict probes', probe)
    payload['ci_ip'] = address + '/24'
    assert 'ci_gateway' not in payload and 'ci_dns' not in payload
    vm_uuid = None
    try:
        created = rpc('create', payload)
        vm_uuid = created['uuid']
        record('static guest created with IP only and no gateway DNS payload', {'name': NAME, 'uuid': vm_uuid, 'ci_ip': payload['ci_ip'], 'gateway_dns_omitted': True, 'summary': created['summary']})
        deadline = time.monotonic() + 200
        guest = None
        while time.monotonic() < deadline:
            try:
                proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(key.parent / ('static-internet-guest-known-hosts-' + vm_uuid)), '-i', str(key), 'netcheck@' + address, 'python3 -'], input=GUEST_PROBE.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100)
                if proc.returncode == 0:
                    guest = json.loads(proc.stdout)
                    if guest['cloud_init_finished']:
                        break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(2)
        report['guest_probe'] = guest
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        assert guest and guest['cloud_init_finished'], 'Temporary guest did not finish cloud-init'
        assert guest['hostname'] == NAME and guest['user'] == 'netcheck'
        vm = next(vm for vm in rpc('vms')['vms'] if vm['uuid'] == vm_uuid)
        interfaces = [interface for interface in vm['interfaces'] if interface['type'] == 'bridge' and interface['source'] == 'br0']
        assert len(interfaces) == 1
        mac = interfaces[0]['mac'].lower()
        links = [iface for iface in guest['links'] if iface.get('address', '').lower() == mac]
        assert len(links) == 1, 'Guest link MAC does not match libvirt'
        configured = [iface for iface in guest['addresses'] if iface['ifname'] == links[0]['ifname']]
        assert len(configured) == 1 and any(row['local'] == address and row['prefixlen'] == 24 for row in configured[0]['addr_info'])
        assert any(row['dst'] == 'default' and row.get('gateway') == PROFILE['gateway'] for row in guest['routes'])
        assert all(dns in guest['dns'] for dns in PROFILE['dns'])
        record('guest MAC static IP default route and DNS match the bridge profile', {'uuid': vm_uuid, 'guest': guest, 'libvirt_mac': mac})
        assert guest.get('external'), 'Guest has route/DNS but no verified external HTTPS connection: ' + json.dumps(guest.get('external_attempts'))
        record('guest resolves an external hostname and completes verified HTTPS', guest['external'])
    finally:
        candidates = [vm for vm in rpc('vms')['vms'] if vm['name'] == NAME]
        if candidates:
            assert len(candidates) == 1 and (vm_uuid is None or candidates[0]['uuid'] == vm_uuid)
            cleanup = rpc('action', {'uuid': candidates[0]['uuid'], 'action': 'delete', 'force': True, 'remove_disks': True})
            assert cleanup['removed_count'] == 2 and cleanup['skipped_count'] == 0
            record('only the temporary domain disk and seed ISO are deleted', cleanup)
        assert own_files(NAME) == [] and own_files(NAME + '-invalid') == []
        record('all existing VM states and inactive configurations remain unchanged', assert_baseline())
    report['passed'] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
