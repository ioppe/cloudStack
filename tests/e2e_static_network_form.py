#!/usr/bin/env python3
"""Static network form fixtures in an explicit demo session, no real VM writes."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    report_path = Path(args.report)
    report = {'passed': False, 'fixture_mode': 'Explicit isolated demo UI; no real management operations or login changes', 'checks': []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def record(check):
        report['checks'].append(check)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    def browser(*parts):
        proc = subprocess.run(['agent-browser', '--session', 'vm-platform-static-network-form', '--json'] + list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        data = json.loads(proc.stdout)
        assert proc.returncode == 0 and data.get('success'), 'Browser fixture action failed: ' + parts[0] + ' ' + str(data.get('error', ''))
        return data['data']

    def evaluate(code):
        return browser('eval', code).get('result')

    def wait(code):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if evaluate('Boolean(' + code + ')') is True:
                return
            time.sleep(0.2)
        raise AssertionError('UI fixture did not reach expected state: ' + code)

    def selector(testid):
        return '[data-testid=' + testid + ']'

    def click(testid):
        browser('click', selector(testid))

    def fill(testid, value):
        browser('fill', selector(testid), value)

    def fields():
        return evaluate("({gateway:document.getElementById('new-ci-gateway').value,dns:document.getElementById('new-ci-dns').value,gatewayDisabled:document.getElementById('new-ci-gateway').disabled,dnsDisabled:document.getElementById('new-ci-dns').disabled})")

    try:
        browser('open', 'http://192.168.0.30:8089/?demo=1')
        wait('!document.getElementById("app").hidden && document.querySelector("#host-select option[value=vm150]") !== null')
        click('nav-vms')
        browser('select', '#host-select', 'vm150')
        click('btn-vm-create')
        wait('document.getElementById("new-source") !== null && document.getElementById("new-source").options.length > 0 && document.getElementById("new-disk-dir").value !== ""')
        browser('select', selector('vm-create-network-type'), 'bridge')
        browser('select', '#new-network-source', 'br0')
        # agent-browser 0.20.7 uncheck does not activate this nested label input.
        # Native click emits the real control's input/change events; assert state.
        evaluate("document.getElementById('new-ci-dhcp').click(); true")
        wait('!document.getElementById("new-ci-dhcp").checked && !document.getElementById("new-ci-ip").disabled')
        fill('vm-create-name', 'static-ui-fixture-only')
        fill('vm-create-ci-key', 'ssh-ed25519 AAAAstatic-network-demo-fixture')
        fill('vm-create-ci-ip', '192.168.0.201/24')
        assert fields()['gateway'] == '192.168.0.1'
        assert fields()['dns'] == '192.168.0.1, 8.8.8.8'
        record('matching IP and br0 automatically fill configured gateway and DNS')
        fill('vm-create-ci-dns', '9.9.9.9')
        fill('vm-create-ci-ip', '192.168.0.202/24')
        assert fields()['dns'] == '9.9.9.9'
        fill('vm-create-ci-ip', '192.168.10.202/24')
        assert fields()['gateway'] == '' and fields()['dns'] == '9.9.9.9'
        record('changing subnets clears stale automatic values and preserves manual DNS')
        browser('select', '#new-network-source', 'br1')
        assert fields()['gateway'] == '192.168.10.1' and fields()['dns'] == '9.9.9.9'
        browser('select', '#new-network-source', 'br0')
        assert fields()['gateway'] == '' and fields()['dns'] == '9.9.9.9'
        record('bridge changes only apply the matching bridge profile')
        fill('vm-create-ci-ip', '192.168.0.203/24')
        fill('vm-create-ci-dns', '')
        fill('vm-create-ci-ip', '192.168.0.204/24')
        assert fields()['dns'] == '192.168.0.1, 8.8.8.8'
        # Capture only demo requests. A valid submit deliberately raises a fixture
        # error to keep the dialog alive; no API request can leave this wrapper.
        evaluate("window.__staticCreates=[]; window.__staticApi=api; api=async function(method,path,payload){if(method==='POST' && /^\\/api\\/hosts\\/[^/]+\\/vms$/.test(path)){window.__staticCreates.push({path:path,payload:payload}); throw new Error('STATIC_UI_FIXTURE_CAPTURE');} return window.__staticApi(method,path,payload);}; true")
        fill('vm-create-ci-gateway', '')
        click('vm-create-submit')
        assert evaluate('window.__staticCreates.length') == 0
        assert evaluate("document.getElementById('new-ci-gateway').getAttribute('aria-invalid')") == 'true'
        assert fields()['gateway'] == ''
        fill('vm-create-ci-gateway', '192.168.0.1')
        fill('vm-create-ci-dns', '')
        click('vm-create-submit')
        assert evaluate('window.__staticCreates.length') == 0
        assert evaluate("document.getElementById('new-ci-dns').getAttribute('aria-invalid')") == 'true'
        assert fields()['dns'] == ''
        record('cleared gateway or DNS blocks submission without secretly refilling')
        fill('vm-create-ci-gateway', '192.168.0.254')
        fill('vm-create-ci-dns', '9.9.9.9')
        click('vm-create-submit')
        wait('window.__staticCreates.length === 1 && !document.querySelector("[data-testid=vm-create-submit]").disabled')
        payload = evaluate('window.__staticCreates[0].payload')
        assert payload['ci_dhcp'] is False and payload['ci_isolated'] is False
        assert payload['ci_gateway'] == '192.168.0.254' and payload['ci_dns'] == '9.9.9.9'
        record('connected mode submits manually entered route and DNS unchanged')
        evaluate("document.getElementById('new-ci-isolated').click(); true")
        wait('document.getElementById("new-ci-isolated").checked')
        assert fields()['gatewayDisabled'] and fields()['dnsDisabled']
        fill('vm-create-ci-ip', '198.18.0.204/24')
        click('vm-create-submit')
        wait('window.__staticCreates.length === 2 && !document.querySelector("[data-testid=vm-create-submit]").disabled')
        captured = evaluate('window.__staticCreates[1]')
        payload = captured['payload']
        assert captured['path'] == '/api/hosts/vm150/vms'
        assert payload['ci_isolated'] is True and payload['ci_dhcp'] is False
        assert payload['ci_ip'] == '198.18.0.204/24' and payload['ci_gateway'] == '' and payload['ci_dns'] == ''
        record('explicit isolated mode submits IP only with blank route DNS to the selected host')
        browser('select', selector('vm-create-network-type'), 'network')
        assert evaluate("document.getElementById('new-ci-isolated').checked") is False
        assert evaluate("document.getElementById('new-ci-dhcp').checked") is True
        click('vm-create-submit')
        wait('window.__staticCreates.length === 3 && !document.querySelector("[data-testid=vm-create-submit]").disabled')
        payload = evaluate('window.__staticCreates[2].payload')
        assert payload['ci_dhcp'] is True and payload['ci_isolated'] is False
        assert payload['ci_ip'] == '' and payload['ci_gateway'] == '' and payload['ci_dns'] == ''
        record('switching back to NAT retains DHCP and sends no static flags')
        report['passed'] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        browser('close')


if __name__ == '__main__':
    main()
