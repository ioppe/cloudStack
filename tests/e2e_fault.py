#!/usr/bin/env python3
"""Interrupt only the new node150 agent, always restore it; no VM writes."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from api_client import Client


def service(action):
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8", "root@192.168.0.150", "systemctl " + action + " vm-platform-agent.service"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=25)
    if proc.returncode:
        raise RuntimeError("Agent service operation failed: " + action)
    return proc.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    client = Client("http://192.168.0.30:8089", str(root / "ADMIN-CREDENTIALS.txt"))
    client.login()
    assert service("is-active") == "active"
    baseline = client.call("GET", "/api/hosts/node30/status")["status"]
    report = {"time": int(time.time()), "before": baseline}
    matches = [v for v in client.call("GET", "/api/hosts/node150/vms")["vms"] if v["name"] == "platform-accept-20261103"]
    assert len(matches) == 1, "Dedicated acceptance VM required"
    vm = matches[0]
    try:
        service("stop")
        failure = client.call("GET", "/api/hosts/node150/status", expected=502)
        assert failure["error"]
        local = client.call("GET", "/api/hosts/node30/status")["status"]
        assert local["hostname"] == baseline["hostname"] and local["vm_count"] == baseline["vm_count"]
        report.update(remote_error=failure, local_while_remote_offline=local)
        print("PASS node150 offline returns 502 and node30 stays independently readable", flush=True)
        task = client.call("POST", "/api/hosts/node150/vms/" + vm["uuid"] + "/action", {"action": "suspend", "name": vm["name"]}, expected=202)["task"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = next(t for t in client.call("GET", "/api/tasks")["tasks"] if t["id"] == task["id"])
            if current["status"] != "running":
                break
            time.sleep(0.5)
        assert current["status"] == "failed" and current["host_id"] == "node150" and current["vm_uuid"] == vm["uuid"] and current["error"]
        report["offline_operation"] = current
        print("PASS offline VM operation failed with target host/UUID and explicit reason", flush=True)
    finally:
        service("start")
        assert service("is-active") == "active"
    deadline = time.monotonic() + 15
    while True:
        try:
            recovered = client.call("GET", "/api/hosts/node150/status")
            break
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    assert recovered["online"]
    restored_vm = next(v for v in client.call("GET", "/api/hosts/node150/vms")["vms"] if v["uuid"] == vm["uuid"])
    assert restored_vm["state"] == vm["state"], "Offline operation must not change VM state"
    report["temporary_vm_state_preserved"] = restored_vm["state"]
    report.update(recovered=recovered, passed=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("PASS node150 agent restored", flush=True)

if __name__ == "__main__":
    main()
