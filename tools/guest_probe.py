"""Read-only SSH probe for the one task-created NAT guest; keys never leave scratch."""
import json
from pathlib import Path
import shlex
import subprocess
import time


def probe(client, private_key, previous_boot=None, timeout=150):
    key = Path(private_key)
    assert key.is_file(), 'Acceptance key required'
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        items = [v for v in client.call('GET', '/api/hosts/node150/vms')['vms'] if v['name'] == 'platform-accept-20261103']
        assert len(items) == 1, 'Only the dedicated node150 guest may be probed'
        vm = items[0]
        ips = [ip for ip in vm.get('ip_addresses', []) if ip.startswith('192.168.122.')]
        if not ips:
            time.sleep(2)
            continue
        code = "import getpass,json,os,socket; print(json.dumps({'username':getpass.getuser(),'hostname':socket.gethostname(),'boot_id':open('/proc/sys/kernel/random/boot_id').read().strip(),'cpu_count':os.cpu_count(),'cloud_init_finished':os.path.isfile('/var/lib/cloud/instance/boot-finished')}))"
        args = ['ssh', '-J', 'root@192.168.0.150', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(key.parent / 'vm-platform-accept-guest-known_hosts'), '-o', 'IdentitiesOnly=yes', '-i', str(key), 'vmcheck@' + ips[0], 'python3 -c ' + shlex.quote(code)]
        try:
            proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        except subprocess.TimeoutExpired:
            last = 'SSH timeout'
            time.sleep(2)
            continue
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            assert data['username'] == 'vmcheck' and data['hostname'] == 'platform-accept-20261103', 'Wrong guest identity'
            if data['cloud_init_finished'] and (previous_boot is None or data['boot_id'] != previous_boot):
                data.update(host_id='node150', uuid=vm['uuid'], ip=ips[0])
                return data
        last = proc.stderr.decode()[-500:]
        time.sleep(2)
    raise AssertionError('Acceptance guest SSH/boot did not become ready: ' + last)
