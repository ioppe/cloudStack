#!/usr/bin/env python3
"""Read-only existing-VM checks plus one dedicated IP-discovery guest."""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import time

HOST = '192.168.0.150'
NAME = 'platform-ip-discovery-accept'
DISK_DIR = '/home/vm-disks'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']


def remote(code, host=HOST, timeout=35):
    p = subprocess.run(SSH + ['root@' + host, 'PYTHONIOENCODING=utf-8 /usr/bin/python3 -'], input=code.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    assert p.returncode == 0, p.stderr.decode('utf-8', 'replace')[-800:]
    return json.loads(p.stdout)


def rpc(operation, payload=None, host=HOST):
    request = json.dumps({'operation': operation, 'payload': payload or {}}).encode() + b'\n'
    p = subprocess.run(SSH + ['root@' + host, 'PYTHONIOENCODING=utf-8 /usr/bin/python3 /opt/vm-platform/worker.py --rpc'], input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=65)
    assert p.returncode == 0, p.stderr.decode('utf-8', 'replace')[-500:]
    response = json.loads(p.stdout)
    assert response.get('ok'), response.get('error')
    return response['data']


def environment(host):
    return remote("""import hashlib,json,os,stat,subprocess,urllib.request
from pathlib import Path
base=['virsh','-c','qemu:///system']; rows=[]
for uid in subprocess.check_output(base+['list','--all','--uuid']).decode().split():
    rows.append({'uuid':uid,'name':subprocess.check_output(base+['domname',uid]).decode().strip(),'state':subprocess.check_output(base+['domstate',uid],env=dict(os.environ,LC_ALL='C')).decode().strip(),'inactive_xml_sha256':hashlib.sha256(subprocess.check_output(base+['dumpxml',uid,'--inactive'])).hexdigest()})
cfg=Path('/etc/vm-platform/agent.json'); config=json.loads(cfg.read_text())
sock=os.stat(config.get('socket_path','/run/vm-platform/agent.sock'))
assert stat.S_ISSOCK(sock.st_mode) and sock.st_uid==0 and stat.S_IMODE(sock.st_mode)==0o600
assert config.get('bridge_ip_discovery') is True and config.get('allow_writes') is True
services=['vm-platform-agent.service']
if Path('/etc/vm-platform/controller.json').exists(): services+=['vm-platform.service','vm-panel.service']
states={unit:{'active':subprocess.check_output(['systemctl','is-active',unit]).decode().strip(),'enabled':subprocess.run(['systemctl','is-enabled',unit],stdout=subprocess.PIPE).stdout.decode().strip()} for unit in services}
assert all(value['active']=='active' and (unit=='vm-panel.service' or value['enabled']=='enabled') for unit,value in states.items())
report={'existing_vms':rows,'agent_config_sha256':hashlib.sha256(cfg.read_bytes()).hexdigest(),'read_only_uuid_count':len(config.get('read_only_uuids',[])),'bridge_ip_discovery':config.get('bridge_ip_discovery'),'bridge_ipv4':subprocess.check_output(['ip','-o','-4','addr','show','dev','br0']).decode().strip(),'default_route':subprocess.check_output(['ip','-4','route','show','default']).decode().strip(),'resolver_sha256':hashlib.sha256(Path('/etc/resolv.conf').read_bytes()).hexdigest(),'services':states,'agent_socket_mode':'0600'}
if 'vm-panel.service' in services:
    report['old_files']={name:hashlib.sha256((Path('/opt/vm-panel')/name).read_bytes()).hexdigest() for name in ['server.py','config.json','vm-panel.db']}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report['old_http']=opener.open('http://127.0.0.1:8088/',timeout=10).getcode()
    assert report['old_http']==200
    assert report['old_files']=={'server.py':'e4027995a691c9ef420a637a5b6fb73e079f3bdbb44eb1deb9ae6d20e9e7942f','config.json':'a0e01be27f87d541cbf7762b3953d9cbfaf07426e230257c2b0612725d32ae48','vm-panel.db':'d07aeea64ffd46c502b3d7b0b0dd60f7a9fd5a2036293b5356c470886f57844c'}
else:
    report['dns']=subprocess.check_output(['resolvectl','dns','br0']).decode().strip()
    report['netplan_sha256']=hashlib.sha256(Path('/etc/netplan/01-netcfg.yaml').read_bytes()).hexdigest()
    report['mount_dependency']=subprocess.check_output(['systemctl','show','vm-platform-agent.service','-p','RequiresMountsFor','-p','Requires']).decode().strip()
    assert '192.168.0.1' in report['dns'] and '8.8.8.8' in report['dns']
    assert '/home/vm-disks' in report['mount_dependency'] and 'home.mount' in report['mount_dependency']
    assert config['disk_dirs']==['/var/lib/libvirt/images/vms','/home/vm-disks']
print(json.dumps(report))
""", host)


def files():
    paths = [DISK_DIR + '/' + NAME + '.qcow2', DISK_DIR + '/' + NAME + '-seed.iso']
    return remote('import json,os; print(json.dumps([p for p in ' + repr(paths) + ' if os.path.lexists(p)]))')


def choose_address():
    return remote("""import json,socket,struct,time
bridge='br0'; mac=bytes.fromhex(open('/sys/class/net/'+bridge+'/address').read().strip().replace(':',''))
sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(0x0806)); sock.bind((bridge,0)); sock.settimeout(0.2)
probes=[]
try:
    for address in ['192.168.0.248','192.168.0.247','192.168.0.246']:
        target=socket.inet_aton(address); conflicts=[]
        frame=b'\\xff'*6+mac+b'\\x08\\x06'+struct.pack('!HHBBH',1,0x0800,6,4,1)+mac+b'\\x00'*4+b'\\x00'*6+target
        for _ in range(3):
            sock.send(frame); deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                try: packet=sock.recv(2048)
                except socket.timeout: continue
                if len(packet)<42 or packet[12:14]!=b'\\x08\\x06' or packet[22:28]==mac: continue
                if packet[28:32]==target or (packet[38:42]==target and packet[28:32]==b'\\x00'*4): conflicts.append(packet[22:28].hex())
        probes.append({'address':address,'probe_count':3,'conflicting_macs':sorted(set(conflicts))})
        if not conflicts:
            print(json.dumps({'address':address,'probes':probes})); break
    else: raise RuntimeError('No free acceptance address')
finally: sock.close()
""", timeout=20)


def raw_arp(host, mac, expected):
    result = remote('import json,sys,time; sys.path.insert(0,"/opt/vm-platform"); from ipdiscovery import BridgeScanner; from vmops import run_command; start=time.monotonic(); result=BridgeScanner(runner=run_command).scan("br0",' + repr([mac]) + '); result["elapsed_seconds"]=round(time.monotonic()-start,3); print(json.dumps(result))', host)
    assert result.get('status') == 'ok', result
    addresses = result['addresses'].get(mac, [])
    if isinstance(addresses, str):
        addresses = [addresses]
    assert set(expected).issubset(addresses), result
    assert set(result['addresses']) == {mac}, 'Non-target MAC returned'
    return result


def wait_ip(uuid, ip, host=HOST, deadline_seconds=100):
    deadline = time.monotonic() + deadline_seconds
    observed = []
    while time.monotonic() < deadline:
        rows = rpc('vms', host=host)['vms']
        vm = next(row for row in rows if row['uuid'] == uuid)
        status = {'state': vm['state'], 'ip_status': vm.get('ip_status'), 'ip_status_reason': vm.get('ip_status_reason'), 'ip_addresses': vm.get('ip_addresses'), 'interfaces': vm.get('interfaces')}
        if not observed or observed[-1] != status:
            observed.append(status)
        if vm['state'] == 'running' and ip in vm.get('ip_addresses', []):
            assert vm.get('ip_status') == 'resolved'
            return {'uuid': uuid, 'observations': observed, 'resolved': status}
        time.sleep(2)
    raise AssertionError('IP discovery did not resolve: ' + json.dumps(observed, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    output = Path(args.report)
    report = {'passed': False, 'checks': [], 'scope': 'Existing guests metadata only; only dedicated acceptance guest lifecycle changes'}

    def record(check, data):
        report['checks'].append({'check': check, 'evidence': data})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        baseline = dict(zip(['192.168.0.30', HOST], pool.map(environment, ['192.168.0.30', HOST])))
    assert all(value['read_only_uuid_count'] == 0 for value in baseline.values())
    record('capture current VM configuration and host environment with permissions preserved', baseline)
    known = [('192.168.0.150', '5bf2a360-20d4-4c05-99de-499796069fef', '192.168.0.160'), ('192.168.0.30', '4b5401ef-1ec8-46f4-b966-45b0458fd7e6', '192.168.0.28')]
    for host, uuid, ip in known:
        record('existing bridged VM address resolves on ' + host, wait_ip(uuid, ip, host=host))
    record('real ARP fallback resolves the existing node150 VM without neighbor-cache dependence', raw_arp(HOST, '52:54:00:9c:98:ea', ['192.168.0.160']))
    record('real ARP fallback preserves both observed addresses for the same node30 MAC', raw_arp('192.168.0.30', '52:54:00:3a:56:b5', ['192.168.0.28', '192.168.0.211']))
    assert not any(row['name'] == NAME for row in rpc('vms')['vms']) and files() == []
    address = choose_address()
    record('temporary address passes three ARP conflict probes', address)
    key = Path(os.environ['PI_SCRATCH_DIR']) / 'vm-platform-ip-discovery-guest'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'ip-discovery-accept-only', '-f', str(key)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    uuid = None
    try:
        created = rpc('create', {'name': NAME, 'kind': 'disk', 'source': '/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img', 'disk_dir': DISK_DIR, 'vcpus': 2, 'memory_gb': 2, 'disk_gb': 8, 'network_type': 'bridge', 'network_source': 'br0', 'cloud_init_enabled': True, 'ci_username': 'ipcheck', 'ci_ssh_key': Path(str(key) + '.pub').read_text().strip(), 'ci_dhcp': False, 'ci_ip': address['address'] + '/24'})
        uuid = created['uuid']
        record('new VM IP becomes visible without guest login or ping', wait_ip(uuid, address['address'], deadline_seconds=180))
        stopped = rpc('action', {'uuid': uuid, 'action': 'stop'})
        assert stopped['state'] == '已关机'
        vm = next(row for row in rpc('vms')['vms'] if row['uuid'] == uuid)
        assert vm['state'] == 'shut off' and vm.get('ip_status') == 'not_running'
        record('temporary stopped VM is not shown as acquiring an IP', {'uuid': uuid, 'action_state': stopped['state'], 'state': vm['state'], 'ip_status': vm.get('ip_status')})
        rpc('action', {'uuid': uuid, 'action': 'start'})
        record('restarted temporary VM address appears automatically', wait_ip(uuid, address['address'], deadline_seconds=150))
    finally:
        candidates = [row for row in rpc('vms')['vms'] if row['name'] == NAME]
        if candidates:
            assert len(candidates) == 1 and (uuid is None or candidates[0]['uuid'] == uuid)
            cleanup = rpc('action', {'uuid': candidates[0]['uuid'], 'action': 'delete', 'force': True, 'remove_disks': True})
            assert cleanup['removed_count'] == 2 and cleanup['skipped_count'] == 0
            record('temporary domain disk and seed removed', cleanup)
        assert files() == []
        after = {host: environment(host) for host in baseline}
        for host in baseline:
            before = dict(baseline[host]); current = dict(after[host])
            before['existing_vms'] = sorted(before['existing_vms'], key=lambda row: row['uuid'])
            current['existing_vms'] = sorted(current['existing_vms'], key=lambda row: row['uuid'])
            assert before == current, 'Existing VMs, host networking or agent permissions changed: ' + host
        record('all existing VM states XML host network and permissions remain unchanged', {'vm_counts': {host: len(value['existing_vms']) for host, value in after.items()}, 'temporary_files': []})
    report['passed'] = True
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
