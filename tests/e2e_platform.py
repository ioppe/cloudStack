#!/usr/bin/env python3
"""Real-host acceptance. All VM writes restricted to the named temporary VM on node150."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from api_client import Client
from guest_probe import probe as guest_probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["readiness", "lifecycle", "failure", "cleanup"])
    parser.add_argument("--base", default="http://192.168.0.30:8089")
    parser.add_argument("--credentials", default=str(Path(__file__).resolve().parents[1] / "ADMIN-CREDENTIALS.txt"))
    parser.add_argument("--name", default="platform-accept-20261103")
    parser.add_argument("--report", required=True)
    parser.add_argument("--guest-key", help="Acceptance-only SSH key in session scratch")
    args = parser.parse_args()
    if not args.name.startswith("platform-accept-"):
        raise SystemExit("Only acceptance VM names are allowed")
    client = Client(args.base, args.credentials)
    report = {"stage": args.stage, "time": int(time.time()), "checks": []}
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)

    def record(name, evidence):
        report["checks"].append({"check": name, "evidence": evidence})
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("PASS " + name, flush=True)

    def vms(hid="node150"):
        return client.call("GET", "/api/hosts/" + hid + "/vms")["vms"]

    def vm():
        matches = [item for item in vms() if item["name"] == args.name]
        if len(matches) != 1:
            raise AssertionError("Acceptance VM must exist exactly once")
        return matches[0]

    def job(endpoint, data, success=True):
        task = client.call("POST", endpoint, data, expected=202)["task"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = next(x for x in client.call("GET", "/api/tasks")["tasks"] if x["id"] == task["id"])
            if current["status"] != "running":
                assert current["host_id"] == "node150"
                assert current["status"] == ("success" if success else "failed"), current.get("error")
                return current
            time.sleep(1)
        raise AssertionError("Task did not finish")

    def wait_state(target, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = vm()
            if item["state"] == target:
                return item
            time.sleep(2)
        raise AssertionError("VM did not reach state " + target)

    if args.stage == "readiness":
        record("health", client.call("GET", "/health"))
        client.call("GET", "/api/hosts", expected=401)
        client.call("POST", "/api/hosts/node150/vms", {"kind": "disk", "name": args.name}, expected=401)
        client.call("POST", "/api/login", {"username": "admin", "password": "invalid-acceptance-password"}, expected=401)
        record("unauthenticated writes and wrong password rejected", {"unauthenticated": 401, "wrong_password": 401})
        client.login()
        hosts = client.call("GET", "/api/hosts")["hosts"]
        assert {h["id"] for h in hosts} == {"node30", "node150"}
        for host in hosts:
            status = client.call("GET", "/api/hosts/" + host["id"] + "/status")
            assert status["online"] and status["status"]["kvm"]
            record("online " + host["id"], status["status"])
        original = {x["uuid"]: x["state"] for x in vms("node30")}
        assert len(original) == 14
        client.call("GET", "/api/hosts/missing/vms", expected=404)
        data = dict(id="accept-registration", name="验收登记", address="192.168.0.150", transport="ssh", ssh_user="root", ssh_port=22)
        client.call("POST", "/api/hosts", data, expected=201)
        data["name"] = "验收登记已编辑"
        client.call("PUT", "/api/hosts/accept-registration", data)
        assert client.call("GET", "/api/hosts/accept-registration/status")["online"]
        client.call("DELETE", "/api/hosts/accept-registration")
        assert original == {x["uuid"]: x["state"] for x in vms("node30")}
        record("host CRUD and unknown-host isolation", {"registered_and_removed": data["id"], "old_vms_unchanged": original})
        client.call("POST", "/api/logout")
        client.call("GET", "/api/hosts", expected=401)
        record("logout invalidates cookie", 401)
    else:
        client.login()
        if args.stage == "failure":
            failure = job("/api/hosts/node150/vms", {"kind": "disk", "name": args.name + "-denied", "source": "/etc/passwd", "disk_dir": "/var/lib/libvirt/images/vms", "vcpus": 1, "memory_gb": 1, "network_type": "network", "network_source": "default"}, success=False)
            assert failure["error"]
            assert not [x for x in vms() if x["name"] == args.name + "-denied"]
            record("out-of-directory image rejected with failed task", {k: failure[k] for k in ("id", "host_id", "status", "error")})
        elif args.stage == "lifecycle":
            item = vm()
            vm_uuid = item["uuid"]
            endpoint = "/api/hosts/node150/vms/" + vm_uuid
            assert item["state"] == "running"
            record("created via browser", {"uuid": vm_uuid, "name": item["name"], "host_id": item["host_id"], "state": item["state"]})
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                item = vm()
                if any(ip.startswith("192.168.122.") for ip in item["ip_addresses"]):
                    break
                time.sleep(3)
            assert any(ip.startswith("192.168.122.") for ip in item["ip_addresses"]), item["ip_addresses"]
            record("NAT address obtained", item["ip_addresses"])
            guest_before = guest_probe(client, args.guest_key) if args.guest_key else None
            if guest_before:
                record("cloud-init custom user SSH and guest identity", guest_before)
            for action, state in [("suspend", "paused"), ("resume", "running"), ("reboot", "running"), ("shutdown", "shut off")]:
                task = job(endpoint + "/action", {"action": action, "name": args.name})
                wait_state(state, 120)
                if action == "reboot" and guest_before:
                    guest_after = guest_probe(client, args.guest_key, previous_boot=guest_before["boot_id"])
                    record("guest reboot boot ID changed", {"before": guest_before["boot_id"], "after": guest_after["boot_id"], "uuid": vm_uuid})
                record("lifecycle " + action, {"task": task["id"], "state": state})
            before = vm()["config"]
            size = int(before["primary_disk_gb"]) + 4
            task = job(endpoint + "/config", {"vcpus": 3, "memory_gb": 4, "disk_gb": size, "name": args.name, "resize_guest": False})
            changed = vm()["config"]
            assert int(changed["vcpus"]) == 3 and int(changed["memory_gb"]) == 4 and float(changed["primary_disk_gb"]) >= size, changed
            record("offline CPU memory disk growth", {"task": task["id"], "before": before, "after": changed})
            for action, state in [("start", "running"), ("stop", "shut off"), ("start", "running")]:
                task = job(endpoint + "/action", {"action": action, "name": args.name})
                wait_state(state)
                record("lifecycle " + action, {"task": task["id"], "state": state})
            if args.guest_key:
                final_guest = guest_probe(client, args.guest_key)
                assert final_guest["cpu_count"] == 3
                record("configured guest booted with three vCPUs", final_guest)
        elif args.stage == "cleanup":
            item = vm()
            endpoint = "/api/hosts/node150/vms/" + item["uuid"] + "/action"
            task = job(endpoint, {"action": "delete", "force": True, "remove_disks": True, "name": args.name})
            assert not [x for x in vms() if x["name"] == args.name]
            record("temporary VM removed", {"uuid": item["uuid"], "task": task["id"], "result": task["result"]})
    report["passed"] = True
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
