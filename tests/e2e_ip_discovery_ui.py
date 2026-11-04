#!/usr/bin/env python3
"""Browser fixtures for automatic VM address refresh; no production writes."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    path = Path(args.report)
    report = {'passed': False, 'fixture_mode': 'Explicit demo fixtures only; no production login or management writes', 'checks': []}
    path.write_text(json.dumps(report, indent=2) + '\n')

    def browser(*args):
        result = subprocess.run(['agent-browser', '--session', 'vm-platform-ip-refresh', '--json'] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
        data = json.loads(result.stdout)
        assert result.returncode == 0 and data.get('success'), str(data.get('error', 'browser failed'))
        return data['data']

    def evaluate(expression):
        return browser('eval', expression).get('result')

    def wait(expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if evaluate('Boolean(' + expression + ')') is True:
                return
            time.sleep(0.2)
        raise AssertionError('Browser fixture timed out: ' + expression)

    def click(testid):
        browser('click', '[data-testid=' + testid + ']')

    def record(check):
        report['checks'].append(check)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('PASS ' + check, flush=True)

    def stable_count():
        count = evaluate('window.__ipPollCount')
        time.sleep(6.2)
        assert evaluate('window.__ipPollCount') == count, 'Polling continued while it should be stopped'

    try:
        browser('open', 'http://192.168.0.30:8089/?demo=1')
        wait('!document.getElementById("app").hidden && document.querySelector("#host-select option[value=local]") !== null')
        browser('snapshot', '-i')
        evaluate(r"""window.__ipRows=JSON.parse(JSON.stringify(demoVms));
window.__ipLocal=window.__ipRows.local[0];
Object.assign(window.__ipLocal,{ip_addresses:[],interfaces:[],ip_status:'discovering',ip_status_reason:'正在查询局域网地址'});
window.__ipPollCount=0; window.__holdLocal=false; window.__releaseLocal=null; window.__ipApi=api;
api=async function(method,path,body){
  const m=path.match(/^\/api\/hosts\/([^/]+)\/vms$/);
  if(m && method==='GET'){
    window.__ipPollCount++;
    if(m[1]==='local' && window.__holdLocal){
      window.__holdLocal=false;
      return await new Promise(resolve=>{window.__releaseLocal=()=>resolve({vms:[Object.assign({},window.__ipLocal,{ip_addresses:['198.18.0.252'],ip_status:'resolved'})]});});
    }
    return {vms:JSON.parse(JSON.stringify(window.__ipRows[m[1]] || []))};
  }
  if(method!=='GET' && /\/vms(?:\/|$)/.test(path)) throw new Error('Fixture forbids management writes');
  return window.__ipApi(method,path,body);
};
selectHost('local',{force:true}); true""")
        click('nav-vms')
        wait('document.querySelector("[data-testid=vms-table]") !== null && document.querySelector("[data-testid=vms-table]").textContent.includes("探测中")')
        record('running VM with active discovery shows a bounded discovery status')
        evaluate("Object.assign(window.__ipLocal,{ip_addresses:['192.168.0.218'],ip_status:'resolved'}); true")
        wait('document.querySelector("[data-testid=vms-table]").textContent.includes("192.168.0.218")')
        record('completed discovery appears through automatic polling without manual refresh')
        evaluate("Object.assign(window.__ipLocal,{ip_addresses:[],ip_status:'unavailable',ip_status_reason:'未收到局域网地址响应，可在控制台检查网络'}); true")
        wait('document.querySelector("[data-testid=vms-table]").textContent.includes("未探测到")')
        assert evaluate('document.querySelector("[data-testid=vms-table]").textContent.includes("获取中")') is False
        record('no response becomes not discovered instead of an endless acquiring label')
        evaluate("Object.assign(window.__ipLocal,{state:'shut off',ip_addresses:['192.168.0.218'],ip_status:'not_running'}); true")
        wait('document.querySelector("[data-testid=vm-ip-" + window.__ipLocal.uuid + "]").textContent === "—"')
        record('stopped VM hides previously observed addresses instead of showing stale IPs')
        evaluate("Object.assign(window.__ipLocal,{state:'running',ip_addresses:[],ip_status:'unavailable'}); true")
        evaluate('window.__holdLocal=true; true')
        wait('window.__releaseLocal !== null')
        browser('select', '#host-select', 'vm150')
        wait('state.hostId === "vm150" && state.vms.length > 0 && state.vms.every(v=>!window.__ipRows.local.some(old=>old.uuid===v.uuid))')
        evaluate('window.__releaseLocal(); true')
        time.sleep(0.4)
        assert evaluate('document.querySelector("[data-testid=vms-table]").textContent.includes("198.18.0.252")') is False
        record('a late address response from the previous host cannot overwrite the selected host')
        click('nav-images')
        stable_count()
        record('leaving the VM list stops automatic VM polling')
        click('nav-vms')
        wait('state.view === "vms" && document.querySelector("[data-testid=vms-table]") !== null')
        evaluate("window.__ipHidden=true; Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__ipHidden}); document.dispatchEvent(new Event('visibilitychange')); true")
        stable_count()
        count = evaluate('window.__ipPollCount')
        evaluate("window.__ipHidden=false; document.dispatchEvent(new Event('visibilitychange')); true")
        wait('window.__ipPollCount > ' + str(count))
        record('hidden pages pause polling and visible pages resume it')
        click('btn-logout')
        wait('state.user === null && document.getElementById("app").hidden')
        stable_count()
        record('logout cancels automatic VM polling')
        report['passed'] = True
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    finally:
        browser('close')


if __name__ == '__main__':
    main()
