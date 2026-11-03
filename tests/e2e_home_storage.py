#!/usr/bin/env python3
"""Validate node150 /home storage through the root-only agent; no login changes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

HOST = '192.168.0.150'
NAME = 'platform-storage-accept-20261103'
DISK_DIR = '/home/vm-disks'


def rpc(operation, payload=None):
    request = json.dumps({'operation': operation, 'payload': payload or {}}).encode() + b'\n'
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@' + HOST, '/usr/bin/python3 /opt/vm-platform/worker.py --rpc'], input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=160)
    assert result.returncode == 0, 'node150 agent transport failed'
    response = json.loads(result.stdout)
    assert response.get('ok'), str(response.get('error', 'node150 operation failed'))
    return response['data']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report_path = Path(args.report)
    report = {'checks': [], 'time': int(time.time())}

    def record(check, data):
        report['checks'].append({'check': check, 'evidence': data})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    images = rpc('images')
    paths = {item['path']: item for item in images['disk_dirs']}
    assert list(paths) == ['/var/lib/libvirt/images/vms', DISK_DIR]
    assert paths[DISK_DIR]['exists'] and paths[DISK_DIR]['free'] > 6 * 1024 ** 4
    assert paths[DISK_DIR]['free'] > paths['/var/lib/libvirt/images/vms']['free']
    record('both storage options report their actual filesystem capacities', images['disk_dirs'])
    before = rpc('vms')['vms']
    assert not any(vm['name'] == NAME for vm in before), 'Acceptance name already exists'
    original_states = {vm['uuid']: vm['state'] for vm in before}
    key = Path(os.environ['PI_SCRATCH_DIR']) / 'vm-platform-home-storage-guest'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'vm-platform-home-storage-only', '-f', str(key)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    public = Path(str(key) + '.pub').read_text().strip()
    vm_uuid = None
    try:
        result = rpc('create', {'name': NAME, 'kind': 'disk', 'source': '/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img', 'disk_dir': DISK_DIR, 'vcpus': 2, 'memory_gb': 2, 'disk_gb': 8, 'network_type': 'network', 'network_source': 'default', 'cloud_init_enabled': True, 'ci_username': 'storecheck', 'ci_ssh_key': public, 'ci_dhcp': True})
        vm_uuid = result['uuid']
        record('temporary VM created using the home filesystem', {'uuid': vm_uuid, 'name': NAME, 'summary': result['summary']})
        deadline = time.monotonic() + 160
        guest = None
        vm = None
        while time.monotonic() < deadline:
            vm = next(item for item in rpc('vms')['vms'] if item['uuid'] == vm_uuid)
            assert vm['name'] == NAME and vm['state'] == 'running'
            assert vm['config']['primary_disk_path'] == DISK_DIR + '/' + NAME + '.qcow2'
            ips = [ip for ip in vm.get('ip_addresses', []) if ip.startswith('192.168.122.')]
            if ips:
                code = "import json,os,socket,getpass; print(json.dumps({'hostname':socket.gethostname(),'user':getpass.getuser(),'cloud_init_finished':os.path.isfile('/var/lib/cloud/instance/boot-finished')}))"
                try:
                    result = subprocess.run(['ssh', '-J', 'root@' + HOST, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(key.parent / ('home-guest-known-hosts-' + vm_uuid)), '-i', str(key), 'storecheck@' + ips[0], 'python3 -c ' + shlex.quote(code)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
                    if result.returncode == 0:
                        guest = json.loads(result.stdout)
                        if guest['cloud_init_finished']:
                            break
                except subprocess.TimeoutExpired:
                    pass
            time.sleep(2)
        assert guest and guest['hostname'] == NAME and guest['user'] == 'storecheck' and guest['cloud_init_finished'], 'Guest did not boot and initialize from home storage'
        remote = "import json,os,shutil; p='/home/vm-disks/" + NAME + ".qcow2'; s=os.stat(p); print(json.dumps({'disk':p,'same_device_as_home':s.st_dev==os.stat('/home').st_dev,'different_device_from_root':s.st_dev!=os.stat('/').st_dev,'allocated_bytes':s.st_blocks*512,'file_mode':oct(s.st_mode&0o777)}))"
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@' + HOST, '/usr/bin/python3 -c ' + shlex.quote(remote)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        assert result.returncode == 0
        placement = json.loads(result.stdout)
        assert placement['same_device_as_home'] and placement['different_device_from_root']
        record('guest boots and disk is physically on the independent home device', {'guest': guest, 'disk': placement, 'config': vm['config']})
    finally:
        candidates = [item for item in rpc('vms')['vms'] if item['name'] == NAME]
        if candidates:
            assert len(candidates) == 1 and (vm_uuid is None or candidates[0]['uuid'] == vm_uuid)
            cleanup = rpc('action', {'uuid': candidates[0]['uuid'], 'action': 'delete', 'force': True, 'remove_disks': True})
            assert cleanup['removed_count'] == 2 and cleanup['skipped_count'] == 0
            record('temporary domain system disk and seed ISO removed', cleanup)
    after = rpc('vms')['vms']
    assert {item['uuid']: item['state'] for item in after} == original_states
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', 'root@' + HOST, '/usr/bin/python3 -c ' + shlex.quote("import json,os; print(json.dumps([name for name in os.listdir('/home/vm-disks') if name.startswith('platform-storage-accept-')]))")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert result.returncode == 0 and json.loads(result.stdout) == []
    record('original VM states preserved and no acceptance disk files remain', {'original_vm_count': len(before), 'remaining_temporary_files': []})
    report['passed'] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
