#!/usr/bin/env python3
"""Browser acceptance against the live UI. Creates only the dedicated node150 VM."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from api_client import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--guest-public-key', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = root / 'docs/evidence'
    client = Client('http://192.168.0.30:8089', str(root / 'ADMIN-CREDENTIALS.txt'))
    client.login()
    name = 'platform-accept-20261103'
    assert not client.call('GET', '/api/hosts/node150/vms')['vms'], 'node150 must start empty'
    report = {'time': int(time.time()), 'checks': []}

    def record(check, data):
        report['checks'].append({'check': check, 'evidence': data})
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    def browser(*items):
        proc = subprocess.run(['agent-browser', '--session', 'vm-platform-acceptance', '--json'] + list(items), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=55)
        output = proc.stdout.decode()
        try:
            result = json.loads(output)
        except ValueError:
            raise RuntimeError('Browser returned invalid JSON for ' + items[0])
        if proc.returncode or not result.get('success'):
            error = str(result.get('error') or proc.stderr.decode()).replace(client.password, '[redacted]')
            raise RuntimeError('Browser ' + items[0] + ': ' + error)
        return result.get('data', {})

    def evaluate(code):
        return browser('eval', code).get('result')

    def wait(code, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if evaluate(code):
                return
            time.sleep(0.5)
        raise AssertionError('Browser state timeout: ' + code)

    def click(testid):
        browser('click', '[data-testid="' + testid + '"]')

    def fill(testid, value):
        browser('fill', '[data-testid="' + testid + '"]', str(value))

    def shot(stem):
        browser('screenshot', str(evidence / (stem + '.png')), '--full')
        snapshot = browser('snapshot').get('snapshot', '')
        (evidence / (stem + '.txt')).write_text(str(snapshot), encoding='utf-8')

    browser('open', client.base + '/')
    browser('set', 'viewport', '1440', '1000')
    wait('!document.querySelector("#login-layer").hidden')
    shot('browser-login')
    fill('login-username', 'admin')
    fill('login-password', client.password)
    click('login-submit')
    wait('!document.querySelector("#app").hidden')
    wait('document.body.innerText.includes("192.168.0.30") && document.body.innerText.includes("192.168.0.150")')
    shot('browser-hosts')
    record('browser administrator login and both hosts visible', {'url': client.base, 'hosts': ['node30', 'node150']})

    hosts = client.call('GET', '/api/hosts')['hosts']
    original = next(h for h in hosts if h['id'] == 'node30')['name']
    click('host-edit-node30')
    fill('host-field-name', original + ' · 验收')
    click('host-dialog-submit')
    wait('!document.querySelector("[data-testid=host-dialog]")')
    assert next(h for h in client.call('GET', '/api/hosts')['hosts'] if h['id'] == 'node30')['name'] == original + ' · 验收'
    click('host-edit-node30')
    fill('host-field-name', original)
    click('host-dialog-submit')
    wait('!document.querySelector("[data-testid=host-dialog]")')
    record('local host name editable with original address preserved', next(h for h in client.call('GET', '/api/hosts')['hosts'] if h['id'] == 'node30'))

    click('nav-vms')
    browser('select', '#host-select', 'node30')
    wait('document.querySelectorAll("[data-testid^=vm-row-]").length === 14')
    shot('browser-node30-vms')
    browser('select', '#host-select', 'node150')
    wait('document.querySelectorAll("[data-testid^=vm-row-]").length === 0 && document.body.innerText.includes("创建")')
    click('nav-images')
    wait('document.body.innerText.includes("ubuntu-24.04-server-cloudimg-amd64.img")')
    shot('browser-node150-images')
    record('host switching and node150 image isolation', {'node30_vm_count': 14, 'node150_vm_count': 0, 'image': 'ubuntu-24.04-server-cloudimg-amd64.img'})

    click('nav-vms')
    click('btn-vm-create')
    wait('!!document.querySelector("#new-source option[value=\\"/var/lib/libvirt/images/templates/ubuntu-24.04-server-cloudimg-amd64.img\\"]")')
    assert evaluate('document.querySelector("#new-disk-dir").value') == '/var/lib/libvirt/images/vms'
    assert evaluate('document.querySelector("#new-network-source").value') == 'default'
    assert evaluate('document.querySelector("#new-ci-dhcp").disabled && document.querySelector("#new-ci-dhcp").checked')
    fill('vm-create-name', name)
    fill('vm-create-vcpus', 2)
    fill('vm-create-memory', 2)
    fill('vm-create-disk', 12)
    fill('vm-create-ci-user', 'vmcheck')
    fill('vm-create-ci-key', Path(args.guest_public_key).read_text().strip())
    shot('browser-create-form')
    click('vm-create-submit')
    wait('!document.querySelector("[data-testid=vm-create-dialog]")')
    click('nav-tasks')
    wait('document.body.innerText.includes("' + name + '")')
    shot('browser-task-submitted')
    record('VM creation submitted through target-host form', {'host_id': 'node150', 'name': name, 'cpu': 2, 'memory_gib': 2, 'disk_gib': 12, 'network': 'default', 'user': 'vmcheck'})
    deadline = time.monotonic() + 240
    task = None
    while time.monotonic() < deadline:
        task = next((t for t in client.call('GET', '/api/tasks')['tasks'] if t['host_id'] == 'node150' and t['vm_name'] == name and t['title'] == '创建虚拟机'), None)
        if task and task['status'] != 'running':
            break
        time.sleep(1)
    assert task and task['status'] == 'success', task.get('error') if task else 'No create task'
    click('btn-tasks-refresh')
    wait('document.querySelector("[data-testid=task-status-' + task['id'] + ']").innerText.includes("成功")')
    shot('browser-task-success')
    record('browser task shows success with host and VM', task)
    vm = next(v for v in client.call('GET', '/api/hosts/node150/vms')['vms'] if v['name'] == name)
    click('nav-vms')
    wait('!!document.querySelector("[data-testid=vm-console-' + vm['uuid'] + ']")')
    shot('browser-created-vm')
    click('vm-console-' + vm['uuid'])
    tabs = browser('tab', 'list')
    record('console opened from VM row', {'host_id': 'node150', 'uuid': vm['uuid'], 'tabs': tabs})
    # Navigate the current browser page to the same authenticated console as the popup.
    browser('open', client.base + '/console?host=node150&uuid=' + vm['uuid'])
    wait('document.querySelector("#console-status-text").innerText.includes("已连接")', 120)
    assert evaluate('document.querySelector("#vm-meta").innerText.includes(' + json.dumps(vm['uuid']) + ')')
    assert evaluate('document.querySelector("#vm-meta").innerText.includes("node150")')
    assert evaluate('!!document.querySelector("#screen canvas") && document.querySelector("#screen canvas").width > 0')
    shot('browser-vnc-connected')
    record('authenticated VNC connected to node150 UUID and canvas', {'host_id': 'node150', 'uuid': vm['uuid'], 'name': name, 'status': evaluate('document.querySelector("#console-status-text").innerText')})
    report['passed'] = True
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
