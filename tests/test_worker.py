#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VM Platform 执行端单元测试（只 mock 外部命令，不接触真实 libvirt）。

运行：python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import vmops  # noqa: E402
import worker  # noqa: E402

VM_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
TEN_GIB = 10 * 1024 ** 3
TWENTY_GIB = 20 * 1024 ** 3

DOMINFO_SHUT_OFF = "Name: demo\nUUID: %s\nState: shut off\nCPU(s): 2\nMax memory: 2097152 KiB\nUsed memory: 0 KiB\nManaged save: no\n" % VM_UUID
DOMINFO_RUNNING = "Name: demo\nUUID: %s\nState: running\nCPU(s): 2\nMax memory: 2097152 KiB\nUsed memory: 1048576 KiB\nManaged save: no\n" % VM_UUID
DOMAIN_XML = """<domain type='kvm'>
  <name>demo</name>
  <memory unit='KiB'>2097152</memory>
  <currentMemory unit='KiB'>2097152</currentMemory>
  <vcpu>2</vcpu>
  <devices/>
</domain>
"""


class FakeCommands(object):
    """按 needle 匹配命令并返回预设结果；未匹配到即认为代码发出了意外命令。"""

    def __init__(self):
        self.rules = []
        self.calls = []

    def add(self, needles, stdout="", stderr="", returncode=0, hook=None):
        if isinstance(needles, str):
            needles = [needles]
        self.rules.append(
            {
                "needles": list(needles),
                "results": [{"returncode": returncode, "stdout": stdout, "stderr": stderr}],
                "hook": hook,
            }
        )
        return self

    def add_sequence(self, needles, results):
        if isinstance(needles, str):
            needles = [needles]
        entries = []
        for item in results:
            entry = {"returncode": 0, "stdout": "", "stderr": ""}
            entry.update(item)
            entries.append(entry)
        self.rules.append({"needles": list(needles), "results": entries, "hook": None})
        return self

    def __call__(self, args, timeout=120, cwd=None):
        argv = [str(part) for part in args]
        self.calls.append({"argv": argv, "timeout": timeout, "cwd": cwd})
        joined = " ".join(argv)
        for rule in self.rules:
            if all(needle in joined for needle in rule["needles"]):
                if not rule["results"]:
                    raise AssertionError("命令结果已用尽：%s" % joined)
                if len(rule["results"]) > 1:
                    result = dict(rule["results"].pop(0))
                else:
                    result = dict(rule["results"][0])
                if rule["hook"]:
                    rule["hook"](argv)
                return result
        raise AssertionError("未预期的命令：%s" % joined)

    def joined(self):
        return [" ".join(call["argv"]) for call in self.calls]

    def called(self, needle):
        return [text for text in self.joined() if needle in text]

    def position(self, needle):
        for index, text in enumerate(self.joined()):
            if needle in text:
                return index
        return -1


class HostTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="vm-platform-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.disk_dir = os.path.join(self.tmp, "disks")
        self.image_dir = os.path.join(self.tmp, "templates")
        os.makedirs(self.disk_dir)
        os.makedirs(self.image_dir)

    def make_host(self, commands, **overrides):
        config = {
            "image_dirs": [self.image_dir],
            "disk_dirs": [self.disk_dir],
            "allow_writes": True,
            "libvirt_uri": "qemu:///system",
            "socket_path": os.path.join(self.tmp, "agent.sock"),
        }
        config.update(overrides)
        self.commands = commands
        return vmops.Host(vmops.build_config(config), runner=commands)

    def write_file(self, directory, name, content="data"):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def common_network_rules(self, commands):
        commands.add(["ip", "-o", "link", "show", "type", "bridge"], stdout="")
        commands.add(
            ["net-list", "--all"],
            stdout=" Name      State    Autostart   Persistent\n--------------------------------------------\n default   active   yes         yes\n",
        )
        return commands


class DomainResolutionTests(HostTestCase):
    def test_uuid_mismatch_is_rejected_without_action(self):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=OTHER_UUID + "\n")
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError) as context:
            host.action({"uuid": VM_UUID, "action": "start"})

        self.assertIn("UUID", str(context.exception))
        self.assertEqual(self.commands.called(" start demo"), [])

    def test_name_cannot_be_used_as_uuid(self):
        commands = FakeCommands()
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError):
            host.resolve_domain("demo")
        with self.assertRaises(vmops.OpError):
            host.resolve_domain("")

        self.assertEqual(self.commands.calls, [])

    def test_resolved_uuid_matches_domain(self):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID.upper() + "\n")
        host = self.make_host(commands)

        self.assertEqual(host.resolve_domain(VM_UUID), {"uuid": VM_UUID, "name": "demo"})


class DeleteSafetyTests(HostTestCase):
    def _prepare_delete(self):
        template = self.write_file(self.image_dir, "base.qcow2")
        own = self.write_file(self.disk_dir, "demo.qcow2")
        seed = self.write_file(self.disk_dir, "demo-seed.iso")
        shared = self.write_file(self.disk_dir, "shared.qcow2")
        outside = self.write_file(self.tmp, "outside.qcow2")
        block_list = (
            " Type   Device   Target   Source\n"
            "---------------------------------\n"
            " file   disk     vda      %s\n"
            " file   cdrom    sda      %s\n"
            " file   disk     vdb      %s\n"
            " file   disk     vdc      %s\n"
            " file   disk     vdd      %s\n" % (own, seed, shared, template, outside)
        )
        other_block_list = (
            " Type   Device   Target   Source\n"
            "---------------------------------\n"
            " file   disk     vda      %s\n" % shared
        )
        return {
            "template": template,
            "own": own,
            "seed": seed,
            "shared": shared,
            "outside": outside,
            "block_list": block_list,
            "other_block_list": other_block_list,
        }

    def test_delete_removes_only_owned_disks_after_precheck(self):
        files = self._prepare_delete()
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["dominfo", "demo"], stdout=DOMINFO_SHUT_OFF)
        commands.add(["domblklist", "demo", "--details"], stdout=files["block_list"])
        commands.add(["list", "--all", "--name"], stdout="demo\nother\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["domuuid", "other"], stdout=OTHER_UUID + "\n")
        commands.add(["domblklist", "other", "--details"], stdout=files["other_block_list"])
        commands.add(["undefine", "demo"], stdout="Domain demo destroyed\n")
        host = self.make_host(commands)

        result = host.action({"uuid": VM_UUID, "action": "delete", "remove_disks": True, "force": False})

        self.assertEqual(result["removed_count"], 2)
        self.assertEqual(result["skipped_count"], 3)
        self.assertTrue(any("其它虚拟机" in reason for reason in result["skipped_reasons"]))
        self.assertTrue(any("模板" in reason for reason in result["skipped_reasons"]))
        self.assertFalse(os.path.exists(files["own"]))
        self.assertFalse(os.path.exists(files["seed"]))
        self.assertTrue(os.path.exists(files["template"]))
        self.assertTrue(os.path.exists(files["shared"]))
        self.assertTrue(os.path.exists(files["outside"]))
        self.assertLess(self.commands.position("domblklist demo"), self.commands.position("undefine demo"))
        self.assertEqual(len(self.commands.called("undefine")), 1)

    def test_delete_refuses_when_other_vm_disks_unknown(self):
        files = self._prepare_delete()
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["dominfo", "demo"], stdout=DOMINFO_SHUT_OFF)
        commands.add(["domblklist", "demo", "--details"], stdout=files["block_list"])
        commands.add(["list", "--all", "--name"], stdout="demo\nother\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["domuuid", "other"], stdout=OTHER_UUID + "\n")
        commands.add(["domblklist", "other", "--details"], returncode=1, stderr="error: failed to get domain")
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError) as context:
            host.action({"uuid": VM_UUID, "action": "delete", "remove_disks": True, "force": False})

        self.assertIn("无法确认", str(context.exception))
        self.assertEqual(self.commands.called("undefine"), [])
        self.assertTrue(os.path.exists(files["own"]))
        self.assertTrue(os.path.exists(files["seed"]))

    def test_delete_requires_force_when_running(self):
        commands = FakeCommands()
        commands.add(["dominfo", "demo"], stdout=DOMINFO_RUNNING)
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError):
            host.action({"uuid": VM_UUID, "action": "delete", "remove_disks": True, "force": False})

        self.assertEqual(self.commands.called("destroy"), [])
        self.assertEqual(self.commands.called("undefine"), [])


class CreateCommandTests(HostTestCase):
    def test_iso_install_uses_version_appropriate_os_args(self):
        cases = [
            ("4.1.0\n", "--osinfo", "--os-type"),
            ("1.5.1\n", "--os-type", "--osinfo"),
        ]
        for version, expected, unexpected in cases:
            with self.subTest(version=version):
                commands = FakeCommands()
                commands.add_sequence(
                    ["dominfo", "demo"],
                    [{"returncode": 1, "stderr": "failed to get domain"}, {"returncode": 0, "stdout": DOMINFO_SHUT_OFF}],
                )
                commands.add(["virt-install", "--version"], stdout=version)
                self.common_network_rules(commands)
                commands.add(["virt-install", "--connect"], stdout="Domain creation completed.\n")
                commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
                iso = self.write_file(self.image_dir, "install.iso", "iso")
                host = self.make_host(commands)

                result = host.create(
                    {
                        "kind": "iso",
                        "name": "demo",
                        "source": iso,
                        "memory_gb": 2,
                        "vcpus": 2,
                        "disk_gb": 20,
                        "network_type": "network",
                        "network_source": "default",
                    }
                )

                install = self.commands.called("virt-install --connect")[0]
                self.assertIn(expected, install)
                self.assertNotIn(unexpected, install)
                self.assertIn("--cdrom %s" % iso, install)
                self.assertIn("--graphics vnc,listen=127.0.0.1", install)
                self.assertIn("size=20,format=qcow2,bus=virtio", install)
                self.assertEqual(result["uuid"], VM_UUID)
                self.assertEqual(result["name"], "demo")

    def test_disk_create_with_cloud_init_does_not_leak_password(self):
        password = "S3cretPass!42"
        commands = FakeCommands()
        commands.add_sequence(
            ["dominfo", "demo"],
            [{"returncode": 1, "stderr": "failed to get domain"}, {"returncode": 0, "stdout": DOMINFO_SHUT_OFF}],
        )
        self.common_network_rules(commands)
        disk_path = os.path.join(self.disk_dir, "demo.qcow2")

        def make_disk(_argv):
            with open(disk_path, "wb") as handle:
                handle.write(b"qcow2")

        commands.add(["qemu-img", "convert"], hook=make_disk)
        commands.add(["qemu-img", "info", "demo.qcow2"], stdout=json.dumps({"virtual-size": TEN_GIB}))
        commands.add(["virt-install", "--version"], stdout="4.1.0\n")
        seed_path = os.path.join(self.disk_dir, "demo-seed.iso")

        def make_seed(argv):
            with open(seed_path, "wb") as handle:
                handle.write(b"seed")

        commands.add(["-volid", "cidata"], hook=make_seed)
        commands.add(["virt-install", "--connect"], stdout="Domain creation completed.\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        source = self.write_file(self.image_dir, "base.qcow2", "img")
        tool = self.write_file(self.tmp, "genisoimage", "#!/bin/sh\n")
        os.chmod(tool, 0o755)
        host = self.make_host(commands, genisoimage=tool)

        result = host.create(
            {
                "kind": "disk",
                "name": "demo",
                "source": source,
                "memory_gb": 2,
                "vcpus": 2,
                "disk_gb": 10,
                "network_type": "network",
                "network_source": "default",
                "cloud_init_enabled": True,
                "ci_username": "ubuntu",
                "ci_password": password,
                "ci_ssh_key": "ssh-rsa AAAAB3Nza test",
            }
        )

        for text in self.commands.joined():
            self.assertNotIn(password, text)
        self.assertEqual(sorted(result.keys()), ["kind", "name", "summary", "uuid"])
        self.assertNotIn(password, json.dumps(result))
        self.assertTrue(os.path.isfile(seed_path))
        self.assertEqual(stat.S_IMODE(os.stat(seed_path).st_mode), 0o600)
        seed_call = [call for call in self.commands.calls if "-volid" in call["argv"]][0]
        self.assertTrue(seed_call["cwd"])
        self.assertTrue(os.path.isdir(os.path.dirname(seed_call["cwd"])))

    def test_iso_rejects_disk_template_before_install(self):
        source = self.write_file(self.image_dir, "wrong-source.qcow2", "img")
        commands = FakeCommands()
        commands.add(["dominfo", "demo"], returncode=1, stderr="failed to get domain")
        self.common_network_rules(commands)
        host = self.make_host(commands)
        with self.assertRaisesRegex(vmops.OpError, "请选择 .iso 文件"):
            host.create({"kind": "iso", "name": "demo", "source": source, "memory_gb": 2, "vcpus": 2, "disk_gb": 12, "network_type": "network", "network_source": "default"})
        self.assertEqual(commands.called("virt-install --connect"), [])
        self.assertEqual(os.listdir(self.disk_dir), [])

    def test_custom_cloud_init_user_does_not_require_distribution_groups(self):
        text = vmops.build_cloud_init_user_data("demo", "vmcheck", "", "ssh-ed25519 AAAA test", "Asia/Shanghai")
        self.assertIn('name: "vmcheck"', text)
        self.assertIn('sudo: ALL=(ALL) NOPASSWD:ALL', text)
        self.assertNotIn('groups:', text)

    def test_symlinked_template_is_rejected(self):
        outside = self.write_file(self.tmp, "outside.qcow2", "img")
        link = os.path.join(self.image_dir, "link.qcow2")
        os.symlink(outside, link)
        commands = FakeCommands()
        commands.add(["dominfo", "demo"], returncode=1, stderr="failed to get domain")
        self.common_network_rules(commands)
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError) as context:
            host.create(
                {
                    "kind": "disk",
                    "name": "demo",
                    "source": link,
                    "memory_gb": 2,
                    "vcpus": 2,
                    "disk_gb": 10,
                    "network_type": "network",
                    "network_source": "default",
                }
            )

        self.assertIn("符号链接", str(context.exception))
        self.assertEqual(self.commands.called("qemu-img convert"), [])


class ConfigOperationTests(HostTestCase):
    def _config_host(self, state_output, disk_path, capacity=TEN_GIB):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["dominfo", "demo"], stdout=state_output)
        commands.add(["domblklist", "demo", "--details"], stdout=" file   disk     vda      %s\n" % disk_path)
        commands.add(["qemu-img", "info", disk_path], stdout=json.dumps({"virtual-size": capacity}))
        host = self.make_host(commands)
        return host, commands

    def test_disk_shrink_is_rejected(self):
        disk = self.write_file(self.disk_dir, "demo.qcow2")
        host, commands = self._config_host(DOMINFO_SHUT_OFF, disk, capacity=TWENTY_GIB)

        with self.assertRaises(vmops.OpError) as context:
            host.configure({"uuid": VM_UUID, "vcpus": 2, "memory_gb": 2, "disk_gb": 10})

        self.assertIn("只允许扩容", str(context.exception))
        self.assertEqual(self.commands.called("resize"), [])
        self.assertEqual(self.commands.called("define"), [])

    def test_config_requires_powered_off_vm(self):
        disk = self.write_file(self.disk_dir, "demo.qcow2")
        host, commands = self._config_host(DOMINFO_RUNNING, disk)

        with self.assertRaises(vmops.OpError) as context:
            host.configure({"uuid": VM_UUID, "vcpus": 4, "memory_gb": 4, "disk_gb": 10})

        self.assertIn("关机", str(context.exception))
        self.assertEqual(self.commands.called("define"), [])
        self.assertEqual(self.commands.called("resize"), [])

    def test_disk_outside_allowed_dirs_is_rejected(self):
        outside = self.write_file(self.tmp, "outside.qcow2")
        host, commands = self._config_host(DOMINFO_SHUT_OFF, outside, capacity=1024 ** 3)

        with self.assertRaises(vmops.OpError) as context:
            host.configure({"uuid": VM_UUID, "vcpus": 2, "memory_gb": 2, "disk_gb": 5})

        self.assertIn("必须位于", str(context.exception))
        self.assertEqual(self.commands.called("resize"), [])
        self.assertEqual(self.commands.called("define"), [])

    def test_config_grows_disk_and_updates_xml(self):
        disk = self.write_file(self.disk_dir, "demo.qcow2")
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["dominfo", "demo"], stdout=DOMINFO_SHUT_OFF)
        commands.add(["domblklist", "demo", "--details"], stdout=" file   disk     vda      %s\n" % disk)
        commands.add(["qemu-img", "info", disk], stdout=json.dumps({"virtual-size": TWENTY_GIB}))
        commands.add(["qemu-img", "resize"], stdout="")
        commands.add(["dumpxml", "demo"], stdout=DOMAIN_XML)
        commands.add(["define"], stdout="Domain demo defined from /tmp/vm-platform-xxx.xml\n")
        host = self.make_host(commands)

        result = host.configure({"uuid": VM_UUID, "vcpus": 4, "memory_gb": 4, "disk_gb": 30})

        resize_calls = self.commands.called("qemu-img resize")
        self.assertEqual(len(resize_calls), 1)
        self.assertIn("30G", resize_calls[0])
        self.assertEqual(len(self.commands.called("--connect qemu:///system define")), 1)
        self.assertTrue(any("系统盘" in item for item in result["changed"]))
        self.assertTrue(any("CPU" in item for item in result["changed"]))

    def test_read_only_agent_blocks_writes(self):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        host = self.make_host(commands, allow_writes=False)

        with self.assertRaises(vmops.OpError) as context:
            host.action({"uuid": VM_UUID, "action": "start"})
        self.assertIn("只读", str(context.exception))
        self.assertEqual(self.commands.called(" start demo"), [])

        with self.assertRaises(vmops.OpError):
            host.create({"kind": "disk", "name": "demo"})
        self.assertEqual(self.commands.called("virt-install"), [])

    def test_read_only_uuid_list_blocks_single_vm(self):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        host = self.make_host(commands, read_only_uuids=[VM_UUID.upper()])

        with self.assertRaises(vmops.OpError) as context:
            host.action({"uuid": VM_UUID, "action": "stop"})

        self.assertIn("只读", str(context.exception))
        self.assertEqual(self.commands.called("destroy"), [])


class VncTargetTests(HostTestCase):
    def _host(self, display):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["dominfo", "demo"], stdout=DOMINFO_RUNNING)
        commands.add(["domdisplay", "demo"], stdout=display)
        return self.make_host(commands), commands

    def test_non_loopback_display_is_refused(self):
        host, _commands = self._host("vnc://192.168.0.150:0\n")

        with self.assertRaises(vmops.OpError) as context:
            host.vnc_target({"uuid": VM_UUID})

        self.assertIn("回环", str(context.exception))

    def test_loopback_display_reports_port(self):
        host, _commands = self._host("vnc://127.0.0.1:1\n")

        target = host.vnc_target({"uuid": VM_UUID})

        self.assertEqual(target["host"], "127.0.0.1")
        self.assertEqual(target["port"], 5901)
        self.assertEqual(target["display"], 1)

    def test_vnc_requires_running_vm(self):
        commands = FakeCommands()
        commands.add(["domname", VM_UUID], stdout="demo\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        commands.add(["dominfo", "demo"], stdout=DOMINFO_SHUT_OFF)
        host = self.make_host(commands)

        with self.assertRaises(vmops.OpError) as context:
            host.vnc_target({"uuid": VM_UUID})

        self.assertIn("未运行", str(context.exception))


class StubHost(object):
    def __init__(self, data=None):
        self.data = data or {}
        self.seen = []

    def scrub(self, text):
        return "" if text is None else str(text)

    def _record(self, operation, payload):
        self.seen.append((operation, payload))
        return self.data.get(operation, {})

    def status(self, payload):
        return self._record("status", payload)

    def vms(self, payload):
        return self._record("vms", payload)

    def images(self, payload):
        return self._record("images", payload)

    def networks(self, payload):
        return self._record("networks", payload)

    def create(self, payload):
        return self._record("create", payload)

    def action(self, payload):
        return self._record("action", payload)

    def configure(self, payload):
        return self._record("config", payload)

    def vnc_target(self, payload):
        return self._record("vnc_target", payload)


class WorkerCliTests(unittest.TestCase):
    def set_ssh_command(self, value):
        previous = os.environ.get("SSH_ORIGINAL_COMMAND")

        def restore():
            if previous is None:
                os.environ.pop("SSH_ORIGINAL_COMMAND", None)
            else:
                os.environ["SSH_ORIGINAL_COMMAND"] = previous

        self.addCleanup(restore)
        os.environ["SSH_ORIGINAL_COMMAND"] = value

    def patch(self, target, name, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, original)

    def test_ssh_rejects_arbitrary_command(self):
        self.set_ssh_command("bash -lc 'id'")
        self.assertEqual(worker.run_ssh(StubHost()), 2)

        self.set_ssh_command("")
        self.assertEqual(worker.run_ssh(StubHost()), 2)

        self.set_ssh_command("rpc --extra")
        self.assertEqual(worker.run_ssh(StubHost()), 2)

    def test_ssh_accepts_rpc(self):
        self.set_ssh_command("rpc")
        calls = []
        self.patch(worker, "run_rpc", lambda host: calls.append(host) or 0)
        host = StubHost()

        self.assertEqual(worker.run_ssh(host), 0)
        self.assertEqual(calls, [host])

    def test_ssh_rejects_vnc_without_uuid(self):
        self.set_ssh_command("vnc demo")
        calls = []
        self.patch(worker, "run_vnc", lambda host, value: calls.append(value) or 0)

        self.assertEqual(worker.run_ssh(StubHost()), 2)
        self.assertEqual(calls, [])

    def test_ssh_accepts_vnc_uuid(self):
        self.set_ssh_command("vnc %s" % VM_UUID)
        calls = []
        self.patch(worker, "run_vnc", lambda host, value: calls.append(value) or 0)

        self.assertEqual(worker.run_ssh(StubHost()), 0)
        self.assertEqual(calls, [VM_UUID])

    def test_rpc_writes_single_json_line(self):
        stdin = io.StringIO('{"operation": "status", "payload": {}}\n')
        stdout = io.StringIO()
        host = StubHost({"status": {"hostname": "node150"}})

        code = worker.run_rpc(host, stdin=stdin, stdout=stdout)

        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["hostname"], "node150")

    def test_rpc_unknown_operation_returns_error(self):
        stdin = io.StringIO('{"operation": "nope", "payload": {}}\n')
        stdout = io.StringIO()

        code = worker.run_rpc(StubHost(), stdin=stdin, stdout=stdout)

        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertIn("不支持的操作", payload["error"])

    def test_rpc_invalid_json_returns_error(self):
        stdin = io.StringIO("not-json\n")
        stdout = io.StringIO()

        code = worker.run_rpc(StubHost(), stdin=stdin, stdout=stdout)

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue().splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertIn("JSON", payload["error"])

    def test_dispatch_rejects_non_object_payload(self):
        with self.assertRaises(vmops.OpError):
            worker.dispatch(StubHost(), {"operation": "status", "payload": []})
        with self.assertRaises(vmops.OpError):
            worker.dispatch(StubHost(), ["status"])

    def test_dispatch_routes_known_operations(self):
        host = StubHost()
        for operation in ("status", "vms", "images", "networks", "create", "action", "config", "vnc_target"):
            worker.dispatch(host, {"operation": operation, "payload": {"uuid": VM_UUID}})
        self.assertEqual([item[0] for item in host.seen], list(worker.OPERATIONS))


class ConfigLoadingTests(unittest.TestCase):
    def test_defaults_are_read_only(self):
        config = vmops.build_config()

        self.assertFalse(config["allow_writes"])
        self.assertEqual(config["image_dirs"], [])
        self.assertEqual(config["disk_dirs"], [])

    def test_relative_directories_are_rejected(self):
        with self.assertRaises(vmops.OpError):
            vmops.build_config({"disk_dirs": ["relative/path"]})

    def test_os_install_args_by_version(self):
        self.assertEqual(vmops.os_install_args("virt-install 4.1.0"), ["--osinfo", "detect=on,require=off"])
        self.assertEqual(vmops.os_install_args("1.5.1"), ["--os-type", "linux"])
        self.assertEqual(vmops.os_install_args(""), ["--osinfo", "detect=on,require=off"])

class IpDiscoveryIntegrationTests(HostTestCase):
    """v1.1.2：ip_status / ip_source 与后台探测的集成（不接触真实网络与线程）。"""

    DOMIFLIST_BRIDGE = (
        " Interface   Type      Source    Model    MAC\n"
        "--------------------------------------------------------\n"
        " vnet0       bridge    br0       virtio   52:54:00:3a:56:b5\n"
    )
    DOMIFLIST_NAT = (
        " Interface   Type      Source    Model    MAC\n"
        "--------------------------------------------------------\n"
        " vnet0       network   default   virtio   52:54:00:3a:56:b5\n"
    )
    AGENT_ROW = (
        " Name       MAC address          Protocol     Address\n"
        "-------------------------------------------------------------------------------\n"
        " vnet0      52:54:00:3a:56:b5    ipv4         192.168.0.160/24\n"
    )

    def make_host(self, commands, **overrides):
        host = super(IpDiscoveryIntegrationTests, self).make_host(commands, **overrides)
        # 本组用例的被动来源由夹具控制，不能读取执行测试宿主机的 /proc/net/arp。
        host._neigh_map = lambda: {}
        return host

    def vms_commands(self, running=True, interfaces=None, agent="", networks="", leases=""):
        commands = FakeCommands()
        commands.add("net-list", stdout=networks)
        commands.add("list --all --name", stdout="demo\n")
        commands.add(["ip", "neigh", "show"], stdout="")
        commands.add("dominfo demo", stdout=DOMINFO_RUNNING if running else DOMINFO_SHUT_OFF)
        commands.add("domiflist demo", stdout=interfaces if interfaces else self.DOMIFLIST_BRIDGE)
        commands.add("domuuid demo", stdout=VM_UUID + "\n")
        commands.add("domdisplay demo", stdout="")
        commands.add("domstats demo", stdout="")
        commands.add("domblklist demo", stdout=" Type   Device   Target   Source\n")
        if leases:
            commands.add("net-dhcp-leases", stdout=leases)
        if running:
            commands.add("domifaddr demo", stdout=agent)
        return commands

    class FakeScanner(object):
        def __init__(self, result=None):
            self.calls = []
            self.result = result or {"status": "unavailable", "reason": "", "addresses": {}}

        def __call__(self, bridge, macs):
            self.calls.append((bridge, tuple(macs)))
            return self.result

    def test_shut_off_vm_reports_not_running_without_guest_agent(self):
        commands = self.vms_commands(running=False)
        host = self.make_host(commands)
        vm = host.vms()["vms"][0]
        self.assertEqual(vm["state"], "shut off")
        self.assertEqual(vm["ip_status"], "not_running")
        self.assertEqual(vm["ip_status_reason"], "")
        self.assertEqual(vm["interfaces"][0]["ip_source"], "")
        self.assertEqual(commands.called("domifaddr"), [])

    def test_running_vm_without_any_source_is_unavailable_when_disabled(self):
        commands = self.vms_commands(running=True)
        host = self.make_host(commands)
        scanner = self.FakeScanner()
        host._ip_scanner = scanner
        vm = host.vms()["vms"][0]
        self.assertEqual(vm["ip_addresses"], [])
        self.assertEqual(vm["ip_status"], "unavailable")
        self.assertIn("未启用局域网探测", vm["ip_status_reason"])
        self.assertEqual(scanner.calls, [])          # 未启用配置：绝不开扫描

    def test_guest_agent_address_is_resolved_with_source(self):
        commands = self.vms_commands(running=True, agent=self.AGENT_ROW)
        host = self.make_host(commands)
        vm = host.vms()["vms"][0]
        self.assertEqual(vm["ip_addresses"], ["192.168.0.160"])
        self.assertEqual(vm["ip_status"], "resolved")
        self.assertEqual(vm["interfaces"][0]["ip_source"], "agent")

    def test_enabled_discovery_reports_discovering_without_blocking(self):
        commands = self.vms_commands(running=True)
        host = self.make_host(commands, bridge_ip_discovery=True)
        scanner = self.FakeScanner()
        threads = []
        host._ip_scanner = scanner
        host._ip_thread_factory = lambda target: threads.append(target) or _IdleThread(target)
        vm = host.vms()["vms"][0]
        self.assertEqual(vm["ip_status"], "discovering")
        self.assertEqual(len(scanner.calls), 0)      # 后台线程尚未执行
        self.assertEqual(len(threads), 1)
        self.assertEqual(scanner.calls, [])

    def test_enabled_discovery_resolves_from_arp_when_scan_completes(self):
        commands = self.vms_commands(running=True)
        host = self.make_host(commands, bridge_ip_discovery=True)
        scanner = self.FakeScanner(
            {"status": "ok", "reason": "", "addresses": {"52:54:00:3a:56:b5": "192.168.0.160"}})
        host._ip_scanner = scanner
        host._ip_thread_factory = _InlineThread
        vm = host.vms()["vms"][0]
        self.assertEqual(scanner.calls, [("br0", ("52:54:00:3a:56:b5",))])
        self.assertEqual(vm["ip_addresses"], ["192.168.0.160"])
        self.assertEqual(vm["ip_status"], "resolved")
        self.assertEqual(vm["interfaces"][0]["ip_source"], "arp")

    def test_scan_is_only_requested_for_running_bridge_without_passive_ip(self):
        commands = self.vms_commands(running=True, interfaces=self.DOMIFLIST_NAT)
        host = self.make_host(commands, bridge_ip_discovery=True)
        scanner = self.FakeScanner()
        host._ip_scanner = scanner
        host._ip_thread_factory = _InlineThread
        vm = host.vms()["vms"][0]
        self.assertEqual(scanner.calls, [])          # NAT/network 接口不主动探测
        self.assertEqual(vm["ip_status"], "unavailable")

    def test_passive_dhcp_address_skips_active_scan(self):
        leases = (" Expiry Time          MAC address        Protocol   IP address        Hostname  Client ID or DUID\n"
                  "--------------------------------------------------------------------------------\n"
                  " 2026-01-01 00:00:00  52:54:00:3a:56:b5  ipv4       192.168.0.160/24  -         -\n")
        commands = self.vms_commands(running=True, networks=" default   active   yes   yes\n", leases=leases)
        host = self.make_host(commands, bridge_ip_discovery=True)
        scanner = self.FakeScanner()
        host._ip_scanner = scanner
        host._ip_thread_factory = _InlineThread
        vm = host.vms()["vms"][0]
        self.assertEqual(scanner.calls, [])
        self.assertEqual(vm["ip_addresses"], ["192.168.0.160"])
        self.assertEqual(vm["interfaces"][0]["ip_source"], "dhcp")

    def test_multiple_arp_addresses_are_all_reported(self):
        # 真实场景：同一 guest MAC 同时有 192.168.0.28 与 192.168.0.211
        commands = self.vms_commands(running=True)
        host = self.make_host(commands, bridge_ip_discovery=True)
        scanner = self.FakeScanner(
            {"status": "ok", "reason": "",
             "addresses": {"52:54:00:3a:56:b5": ["192.168.0.28", "192.168.0.211"]}})
        host._ip_scanner = scanner
        host._ip_thread_factory = _InlineThread
        vm = host.vms()["vms"][0]
        self.assertEqual(vm["ip_addresses"], ["192.168.0.28", "192.168.0.211"])
        self.assertEqual(vm["interfaces"][0]["ips"], ["192.168.0.28", "192.168.0.211"])
        self.assertEqual(vm["ip_status"], "resolved")
        self.assertEqual(vm["interfaces"][0]["ip_source"], "arp")

    def test_retain_targets_receives_all_running_bridge_interfaces(self):
        # 被动已命中的接口也必须进快照，否则暂停/换口后旧缓存无法剪枝
        commands = self.vms_commands(running=True, agent=self.AGENT_ROW)
        host = self.make_host(commands, bridge_ip_discovery=True)
        manager = _FakeManagerForHost()
        host._ip_discovery_manager = manager
        vm = host.vms()["vms"][0]
        self.assertEqual(manager.retained, [{"br0": ["52:54:00:3a:56:b5"]}])
        self.assertEqual(manager.requests, [])
        self.assertEqual(vm["interfaces"][0]["ip_source"], "agent")

    def test_discovery_disabled_does_not_create_manager(self):
        commands = self.vms_commands(running=True)
        host = self.make_host(commands)
        scanner = self.FakeScanner()
        host._ip_scanner = scanner
        vm = host.vms()["vms"][0]
        self.assertIsNone(host._ip_discovery_manager)
        self.assertEqual(scanner.calls, [])
        self.assertEqual(vm["ip_status"], "unavailable")


class _FakeManagerForHost(object):
    """Host 层的发现管理器替身：只记录 retain/request，不接触网络。"""

    def __init__(self, results=None):
        self.retained = []
        self.requests = []
        self.results = results or {}

    def retain_targets(self, targets):
        self.retained.append(targets)
        return {"kept": 0, "pruned": 0}

    def request(self, requests):
        self.requests.append(requests)
        return {}

    def lookup(self, bridge, mac):
        return self.results.get((bridge, mac)) or {
            "status": "unavailable", "address": "", "addresses": [], "reason": ""}
class _IdleThread(object):
    """记录 target 但绝不执行的假线程：验证 vms() 不被后台扫描阻塞。"""

    def __init__(self, target):
        self.target = target
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class _InlineThread(_IdleThread):
    """同步执行的假线程：让扫描在 request() 内完成，便于断言缓存结果。"""

    def start(self):
        self.started = True
        self.target()

    def is_alive(self):
        return False


if __name__ == "__main__":
    unittest.main()
