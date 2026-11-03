#!/usr/bin/env python3
"""Final UI snapshots using the acceptance browser's existing authenticated session."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = root / 'docs/evidence'

    def browser(*parts):
        proc = subprocess.run(['agent-browser', '--session', 'vm-platform-acceptance', '--json'] + list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        data = json.loads(proc.stdout)
        assert proc.returncode == 0 and data.get('success'), 'Browser status action failed: ' + parts[0]
        return data['data']

    def wait(code):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if browser('eval', code).get('result'):
                return
            time.sleep(0.5)
        raise AssertionError('Final browser status timeout')

    def shot(stem):
        browser('screenshot', str(evidence / (stem + '.png')), '--full')
        (evidence / (stem + '.txt')).write_text(browser('snapshot')['snapshot'], encoding='utf-8')

    browser('open', 'http://192.168.0.30:8089/')
    wait('!!document.querySelector("[data-testid=host-check-node30]")')
    for hid, count in [('node30', 14), ('node150', 0)]:
        browser('click', '[data-testid=host-check-' + hid + ']')
        wait('document.querySelector("[data-testid=host-status-' + hid + ']").innerText.includes("在线") && document.querySelector("[data-testid=host-row-' + hid + ']").innerText.includes("VM ' + str(count) + '")')
    shot('browser-hosts-final')
    browser('click', '[data-testid=nav-tasks]')
    wait('document.body.innerText.includes("连接中断") && document.body.innerText.includes("成功")')
    shot('browser-tasks-final')
    report = {'passed': True, 'hosts': {'node30': {'online': True, 'vm_count': 14}, 'node150': {'online': True, 'vm_count': 0}}, 'task_feedback': 'success and explicit failed-operation reason visible', 'screenshots': ['browser-hosts-final.png', 'browser-tasks-final.png']}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('PASS both host rows show real online resources/counts after cleanup; task page shows success/failure reasons')


if __name__ == '__main__':
    main()
