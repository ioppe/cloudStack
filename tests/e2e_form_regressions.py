#!/usr/bin/env python3
"""UI regression fixtures only: no real hosts, passwords or VM operations."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()

    def browser(*parts):
        proc = subprocess.run(['agent-browser', '--session', 'vm-platform-form-regressions', '--json'] + list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        data = json.loads(proc.stdout)
        assert proc.returncode == 0 and data.get('success'), 'Regression browser action failed: ' + parts[0]
        return data['data']

    def evaluate(code):
        return browser('eval', code).get('result')

    def wait(code):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if evaluate(code):
                return
            time.sleep(0.2)
        raise AssertionError('UI fixture state did not become ready')

    def click(testid):
        browser('click', '[data-testid="' + testid + '"]')

    checks = []
    try:
        browser('open', 'http://192.168.0.30:8089/?demo=1')
        wait('!document.querySelector("#app").hidden && !!document.querySelector("#host-select option[value=local]")')
        # Explicit demo fixture: exercise a bridge-default host with two NAT networks.
        evaluate("Object.assign(DEMO_NETWORKS.local,{default_type:'bridge',default_source:'br0',default_bridge:'br0',default_network:'preferred-nat',libvirt_networks:[{name:'first-nat',state:'active'},{name:'preferred-nat',state:'active'}]}); true")
        click('nav-vms')
        browser('select', '#host-select', 'local')
        click('btn-vm-create')
        wait('!!document.querySelector("#new-source") && document.querySelector("#new-source").options.length >= 2')
        selected = '/opt/vm-platform/images/debian-12-genericcloud-amd64.qcow2'
        browser('select', '#new-source', selected)
        browser('uncheck', '[data-testid=vm-create-ci-enabled]')
        assert evaluate('document.querySelector("#new-source").value') == selected
        browser('check', '[data-testid=vm-create-ci-enabled]')
        assert evaluate('document.querySelector("#new-source").value') == selected
        checks.append('cloud-init toggle preserves selected non-first image')
        assert evaluate('document.querySelector("#new-network-source").value') == 'br0'
        browser('select', '[data-testid=vm-create-network-type]', 'network')
        assert evaluate('document.querySelector("#new-network-source").value') == 'preferred-nat'
        checks.append('bridge-default host selects its independent configured NAT source')
        click('vm-create-dialog-close')
        evaluate('window.__logoutCount=0; logout=async function(){window.__logoutCount++;}; true')
        click('btn-logout')
        assert evaluate('window.__logoutCount') == 1
        checks.append('logout button invokes its handler exactly once')
        Path(args.report).write_text(json.dumps({'passed': True, 'fixture_mode': 'isolated demo UI only, no management operations', 'checks': checks}, indent=2) + '\n')
        for check in checks:
            print('PASS ' + check, flush=True)
    finally:
        browser('close')


if __name__ == '__main__':
    main()
