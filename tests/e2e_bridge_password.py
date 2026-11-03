#!/usr/bin/env python3
"""Live v1.1 browser acceptance: static bridge guest, administrator password UI."""
import argparse
import json
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from api_client import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['bridge', 'password'])
    parser.add_argument('--address', help='Temporary free LAN IPv4/CIDR')
    parser.add_argument('--guest-key', help='Acceptance-only SSH private key retained in scratch')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = root / 'docs/evidence'
    credentials = str(root / 'ADMIN-CREDENTIALS.txt')
    client = Client('http://192.168.0.30:8089', credentials)
    client.login()
    original_password = client.password
    temporary_password = secrets.token_urlsafe(28)
    private_values = [original_password, temporary_password]
    report = {'stage': args.stage, 'time': int(time.time()), 'checks': []}

    def record(check, data):
        report['checks'].append({'check': check, 'evidence': data})
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    def browser(*parts):
        try:
            proc = subprocess.run(['agent-browser', '--session', 'vm-platform-v11', '--json'] + list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Browser operation timed out: ' + parts[0]) from None
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError('Invalid browser response for ' + parts[0]) from None
        if proc.returncode or not result.get('success'):
            message = str(result.get('error') or 'Browser operation failed')
            for value in private_values:
                message = message.replace(value, '[redacted]')
            raise RuntimeError(parts[0] + ': ' + message)
        return result.get('data', {})

    def evaluate(code):
        return browser('eval', code).get('result')

    def wait(code, timeout=80):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if evaluate(code):
                return
            time.sleep(0.5)
        raise AssertionError('Browser state did not become ready')

    def click(testid):
        browser('click', '[data-testid="' + testid + '"]')

    def fill(testid, value):
        browser('fill', '[data-testid="' + testid + '"]', str(value))

    def shot(stem, snapshot=True):
        browser('screenshot', str(evidence / (stem + '.png')), '--full')
        if snapshot:
            text = str(browser('snapshot').get('snapshot', ''))
            for value in private_values:
                text = text.replace(value, '[redacted]')
            (evidence / (stem + '.txt')).write_text(text, encoding='utf-8')

    def login_ui(password):
        wait('!document.querySelector("#login-layer").hidden')
        fill('login-username', 'admin')
        fill('login-password', password)
        click('login-submit')
        wait('!document.querySelector("#app").hidden')

    browser('open', client.base + '/')
    browser('set', 'viewport', '1440', '1060')
    wait('!document.querySelector("#login-layer").hidden || !document.querySelector("#app").hidden')
    if evaluate('!document.querySelector("#login-layer").hidden'):
        login_ui(original_password)

    if args.stage == 'password':
        observer = Client(client.base, credentials)
        observer.login()
        changed = False
        try:
            click('btn-password')
            wait('!!document.querySelector("[data-testid=password-dialog]")')
            shot('v1.1-password-dialog')
            fill('password-current', original_password)
            fill('password-new', temporary_password)
            fill('password-confirm', temporary_password + '-mismatch')
            click('password-submit')
            wait('!!document.querySelector("[data-testid=password-error]").innerText.trim()')
            assert not evaluate('document.querySelector("[data-testid=password-error]").hidden')
            record('password confirmation mismatch rejected in dialog', {'dialog_still_open': True})
            fill('password-current', 'invalid-current-password')
            fill('password-confirm', temporary_password)
            click('password-submit')
            wait('document.querySelector("[data-testid=password-error]").innerText.includes("当前密码")')
            record('wrong current password rejected without logging out', {'http': 403, 'dialog_still_open': True})
            fill('password-current', original_password)
            click('password-submit')
            wait('!document.querySelector("#login-layer").hidden')
            changed = True
            shot('v1.1-password-changed')
            observer.call('GET', '/api/hosts', expected=401)
            client.call('GET', '/api/hosts', expected=401)
            client.call('POST', '/api/login', {'username': 'admin', 'password': original_password}, expected=401)
            record('password UI succeeds and revokes other sessions and old password', {'old_sessions': 401, 'old_password': 401})
            login_ui(temporary_password)
            click('btn-password')
            fill('password-current', temporary_password)
            fill('password-new', original_password)
            fill('password-confirm', original_password)
            click('password-submit')
            wait('!document.querySelector("#login-layer").hidden')
            changed = False
            login_ui(original_password)
            client.login()
            record('new password login and UI restoration of delivery password succeeded', {'delivery_credential_unchanged': True, 'final_login': True})
            shot('v1.1-password-final')
        finally:
            if changed:
                recovery = Client(client.base, credentials)
                recovery.password = temporary_password
                recovery.login()
                recovery.call('POST', '/api/password', {'current_password': temporary_password, 'password': original_password})
                print('RESTORED original delivery password after incomplete UI acceptance', flush=True)
    else:
        assert args.address and args.guest_key, 'Temporary address and scratch key required'
        import ipaddress
        interface = ipaddress.IPv4Interface(args.address)
        assert interface.network == ipaddress.IPv4Network('192.168.0.0/24')
        assert str(interface.ip) not in ('192.168.0.1', '192.168.0.30', '192.168.0.150')
        ip = str(interface.ip)
        key = Path(args.guest_key)
        assert key.is_file() and key.with_suffix('.pub').is_file()
        name = 'platform-bridge-accept-20261103'
        assert client.call('GET', '/api/hosts/node150/vms')['vms'] == [], 'node150 must start empty'
        networks = client.call('GET', '/api/hosts/node150/networks')
        assert 'br0' in networks['bridges'] and any(item['name'] == 'default' for item in networks['libvirt_networks'])
        click('nav-vms')
        browser('select', '#host-select', 'node150')
        click('btn-vm-create')
        wait('!!document.querySelector("[data-testid=vm-create-network-type]") && document.querySelector("#new-source").value.includes("ubuntu-24.04")')
        browser('select', '[data-testid=vm-create-network-type]', 'bridge')
        wait('document.querySelector("[data-testid=vm-create-network-source]").value === "br0"')
        browser('uncheck', '[data-testid=vm-create-ci-dhcp]')
        fill('vm-create-ci-ip', args.address)
        fill('vm-create-ci-gateway', '192.168.0.1')
        fill('vm-create-ci-dns', '192.168.0.1, 8.8.8.8')
        fill('vm-create-name', name)
        fill('vm-create-vcpus', 2)
        fill('vm-create-memory', 2)
        fill('vm-create-disk', 12)
        fill('vm-create-ci-user', 'vmcheck')
        fill('vm-create-ci-key', key.with_suffix('.pub').read_text().strip())
        shot('v1.1-bridge-static-form')
        vm_uuid = None
        submitted = False
        try:
            click('vm-create-submit')
            wait('!document.querySelector("[data-testid=vm-create-dialog]")')
            submitted = True
            record('bridge static-address creation submitted through UI', {'host': 'node150', 'name': name, 'network_type': 'bridge', 'source': 'br0', 'address': args.address, 'gateway': '192.168.0.1', 'dns': ['192.168.0.1', '8.8.8.8']})
            deadline = time.monotonic() + 220
            task = None
            while time.monotonic() < deadline:
                task = next((t for t in client.call('GET', '/api/tasks')['tasks'] if t['host_id'] == 'node150' and t['vm_name'] == name and t['title'] == '创建虚拟机'), None)
                if task and task['status'] != 'running':
                    break
                time.sleep(1)
            assert task and task['status'] == 'success', (task or {}).get('error', 'Create did not complete')
            vm_uuid = task['vm_uuid']
            vm = next(v for v in client.call('GET', '/api/hosts/node150/vms')['vms'] if v['uuid'] == vm_uuid)
            assert vm['name'] == name and vm['state'] == 'running'
            assert any(item.get('source') == 'br0' and item.get('type') == 'bridge' for item in vm['interfaces'])
            record('bridge VM task and actual libvirt interface match', {'uuid': vm_uuid, 'host_id': task['host_id'], 'interfaces': vm['interfaces']})
            code = "import json,os,socket,subprocess; print(json.dumps({'hostname':socket.gethostname(),'user':__import__('getpass').getuser(),'addresses':json.loads(subprocess.check_output(['ip','-j','address','show','dev','ens3'])),'routes':json.loads(subprocess.check_output(['ip','-j','-4','route','show','default'])),'dns':subprocess.check_output(['resolvectl','dns','ens3']).decode(),'cloud_init_finished':os.path.isfile('/var/lib/cloud/instance/boot-finished')}))"
            deadline = time.monotonic() + 180
            guest = None
            while time.monotonic() < deadline:
                try:
                    proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes', '-o', 'UserKnownHostsFile=' + str(key.parent / ('bridge-guest-known-hosts-' + vm_uuid)), '-i', str(key), 'vmcheck@' + ip, 'python3 -c ' + shlex.quote(code)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
                except subprocess.TimeoutExpired:
                    time.sleep(2)
                    continue
                if proc.returncode == 0:
                    guest = json.loads(proc.stdout)
                    if guest['cloud_init_finished']:
                        break
                time.sleep(2)
            assert guest and guest['cloud_init_finished'], 'Static guest SSH did not become ready'
            record('guest network identity read before comparison', guest)
            assert guest['hostname'] == name and guest['user'] == 'vmcheck'
            assert any(addr['local'] == ip and addr['prefixlen'] == interface.network.prefixlen for link in guest['addresses'] for addr in link['addr_info'])
            assert any(route.get('gateway') == '192.168.0.1' and route.get('dev') == 'ens3' for route in guest['routes'])
            assert '192.168.0.1' in guest['dns'] and '8.8.8.8' in guest['dns']
            assert any(link.get('address') == item.get('mac') for link in guest['addresses'] for item in vm['interfaces'])
            record('guest actually uses configured static IP gateway DNS and matching MAC', guest)
            click('nav-tasks')
            wait('document.body.innerText.includes("' + name + '")')
            shot('v1.1-bridge-task-success')
        finally:
            if submitted:
                matches = [v for v in client.call('GET', '/api/hosts/node150/vms')['vms'] if v['name'] == name]
                if matches:
                    assert len(matches) == 1 and (vm_uuid is None or matches[0]['uuid'] == vm_uuid)
                    cleanup = client.call('POST', '/api/hosts/node150/vms/' + matches[0]['uuid'] + '/action', {'action': 'delete', 'name': name, 'force': True, 'remove_disks': True}, expected=202)['task']
                    deadline = time.monotonic() + 80
                    while time.monotonic() < deadline:
                        cleanup = next(t for t in client.call('GET', '/api/tasks')['tasks'] if t['id'] == cleanup['id'])
                        if cleanup['status'] != 'running':
                            break
                        time.sleep(1)
                    assert cleanup['status'] == 'success' and cleanup['result'].get('removed_count') == 2
                    assert client.call('GET', '/api/hosts/node150/vms')['vms'] == []
                    record('dedicated bridge guest and two disk files cleaned', cleanup['result'])
    report['passed'] = True
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
