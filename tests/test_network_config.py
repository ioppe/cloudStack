#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态 IPv4 network-config 与创建前校验（只 mock 外部命令，不接触 libvirt）。"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import vmops  # noqa: E402
from test_worker import DOMINFO_SHUT_OFF, TEN_GIB, VM_UUID, FakeCommands, HostTestCase  # noqa: E402

LEGACY_DHCP = (
    "network:\n"
    "  version: 2\n"
    "  ethernets:\n"
    "    nic0:\n"
    "      match:\n"
    '        macaddress: "aa:bb:cc:dd:ee:01"\n'
    '      set-name: "ens3"\n'
    "      dhcp4: true\n"
    "      dhcp6: false\n"
)

BRIDGE_LIST = (
    "7: br0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 master br0 state UP mode DEFAULT\n"
    "8: br1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 master br1 state UP\n"
)
NETWORK_LIST = (
    " Name      State    Autostart   Persistent\n"
    "--------------------------------------------\n"
    " default   active   yes         yes\n"
)


class NetworkConfigTests(unittest.TestCase):
    def test_dhcp_output_is_unchanged_for_legacy_clients(self):
        text = vmops.build_cloud_init_network_config("AA:BB:CC:DD:EE:01", "ens3")
        self.assertEqual(LEGACY_DHCP, text)
        self.assertNotIn("addresses:", text)
        self.assertNotIn("routes:", text)
        self.assertNotIn("nameservers:", text)

    def test_static_output_matches_mac_address_route_and_dns(self):
        static = {"address": "192.168.0.201/24", "gateway": "192.168.0.1", "dns": ["1.1.1.1", "8.8.8.8"]}
        text = vmops.build_cloud_init_network_config("AA:BB:CC:DD:EE:01", "ens3", static)
        self.assertIn('macaddress: "aa:bb:cc:dd:ee:01"', text)
        self.assertIn('set-name: "ens3"', text)
        self.assertIn("      dhcp4: false", text)
        self.assertNotIn("dhcp4: true", text)
        self.assertIn('        - "192.168.0.201/24"', text)
        self.assertIn("      routes:\n        - to: default\n          via: \"192.168.0.1\"", text)
        self.assertIn(
            "      nameservers:\n        addresses:\n          - \"1.1.1.1\"\n          - \"8.8.8.8\"",
            text,
        )

    def test_static_without_gateway_or_dns_omits_optional_sections(self):
        text = vmops.build_cloud_init_network_config(
            "aa:bb:cc:dd:ee:02", "ens4", {"address": "10.0.0.9/24", "gateway": "", "dns": []}
        )
        self.assertIn('        - "10.0.0.9/24"', text)
        self.assertNotIn("routes:", text)
        self.assertNotIn("nameservers:", text)


class StaticCreateTests(HostTestCase):
    def bridge_commands(self):
        commands = FakeCommands()
        commands.add(["ip", "-o", "link", "show", "type", "bridge"], stdout=BRIDGE_LIST)
        commands.add(["net-list", "--all"], stdout=NETWORK_LIST)
        return commands

    def validation_host(self, **overrides):
        commands = self.bridge_commands()
        commands.add(["dominfo", "demo"], returncode=1, stderr="failed to get domain")
        return self.make_host(commands, **overrides)

    def payload(self, **extra):
        data = {
            "kind": "disk",
            "name": "demo",
            "source": os.path.join(self.image_dir, "base.qcow2"),
            "memory_gb": 1,
            "vcpus": 1,
            "disk_gb": 10,
            "network_type": "bridge",
            "network_source": "br0",
            "cloud_init_enabled": True,
            "ci_username": "ubuntu",
            "ci_password": "S3cretPass!42",
            "ci_dhcp": False,
            "ci_ip": "192.168.0.201/24",
            "ci_gateway": "192.168.0.1",
            "ci_dns": ["1.1.1.1", "8.8.8.8"],
        }
        data.update(extra)
        return data

    def assert_no_writes(self):
        self.assertEqual([], self.commands.called("qemu-img"))
        self.assertEqual([], self.commands.called("-volid"))
        self.assertEqual([], self.commands.called("virt-install --connect"))
        self.assertEqual([], os.listdir(self.disk_dir))

    def prepare_full_create(self, commands, capture):
        commands.add_sequence(
            ["dominfo", "demo"],
            [{"returncode": 1, "stderr": "failed to get domain"}, {"returncode": 0, "stdout": DOMINFO_SHUT_OFF}],
        )
        disk_path = os.path.join(self.disk_dir, "demo.qcow2")

        def make_disk(_argv):
            with open(disk_path, "wb") as handle:
                handle.write(b"qcow2")

        commands.add(["qemu-img", "convert"], hook=make_disk)
        commands.add(["qemu-img", "info", "demo.qcow2"], stdout=json.dumps({"virtual-size": TEN_GIB}))
        commands.add(["virt-install", "--version"], stdout="4.1.0\n")
        seed_path = os.path.join(self.disk_dir, "demo-seed.iso")

        def make_seed(_argv):
            cwd = commands.calls[-1]["cwd"]
            with open(os.path.join(cwd, "network-config"), "r", encoding="utf-8") as handle:
                capture.append(handle.read())
            with open(seed_path, "wb") as handle:
                handle.write(b"seed")

        commands.add(["-volid", "cidata"], hook=make_seed)
        commands.add(["virt-install", "--connect"], stdout="Domain creation completed.\n")
        commands.add(["domuuid", "demo"], stdout=VM_UUID + "\n")
        return seed_path

    def make_seed_host(self, overrides=None, capture=None):
        commands = self.bridge_commands()
        self.prepare_full_create(commands, capture)
        tool = self.write_file(self.tmp, "genisoimage", "#!/bin/sh\n")
        os.chmod(tool, 0o755)
        config = {"genisoimage": tool}
        config.update(overrides or {})
        return self.make_host(commands, **config)

    def test_invalid_cidr_is_rejected_before_any_disk_or_seed_write(self):
        cases = {
            "192.168.0.201": "IPv4/前缀",
            "192.168.0.201/31": "前缀",
            "192.168.0.201/33": "静态 IP 无效",
            "192.168.0.0/24": "网络地址",
            "192.168.0.255/24": "广播地址",
            "300.1.1.1/24": "静态 IP 无效",
        }
        for value, expected in cases.items():
            with self.subTest(ip=value):
                host = self.validation_host()
                with self.assertRaisesRegex(vmops.OpError, expected):
                    host.create(self.payload(ci_ip=value))
                self.assert_no_writes()

    def test_gateway_must_be_unicast_in_same_subnet_and_not_guest(self):
        cases = {
            "192.168.1.1": "同一子网",
            "192.168.0.201": "不能等于",
            "192.168.0.0": "网络地址",
            "192.168.0.255": "广播地址",
            "0.0.0.0": "单播",
            "not-an-ip": "合法",
        }
        for value, expected in cases.items():
            with self.subTest(gateway=value):
                host = self.validation_host()
                with self.assertRaisesRegex(vmops.OpError, expected):
                    host.create(self.payload(ci_gateway=value))
                self.assert_no_writes()

    def test_invalid_dns_values_are_rejected(self):
        values = ("1.1.1.1, nope", ["8.8.8.8", "300.1.1.1"], "1.1.1.1 999.1.1.1", ["::1"])
        for value in values:
            with self.subTest(dns=value):
                host = self.validation_host()
                with self.assertRaisesRegex(vmops.OpError, "DNS"):
                    host.create(self.payload(ci_dns=value))
                self.assert_no_writes()

    def test_static_rejected_for_nat_network(self):
        host = self.validation_host()
        with self.assertRaisesRegex(vmops.OpError, "仅支持桥接"):
            host.create(self.payload(network_type="network", network_source="default"))
        self.assert_no_writes()

    def test_static_rejected_when_cloud_init_disabled(self):
        host = self.validation_host()
        with self.assertRaisesRegex(vmops.OpError, "cloud-init"):
            host.create(self.payload(cloud_init_enabled=False))
        self.assert_no_writes()

    def test_static_rejected_for_iso_install(self):
        host = self.validation_host()
        payload = self.payload(kind="iso", cloud_init_enabled=False)
        with self.assertRaisesRegex(vmops.OpError, "cloud-init"):
            host.create(payload)
        self.assert_no_writes()

    def test_dhcp_mode_ignores_static_fields(self):
        host = self.validation_host()
        self.assertIsNone(host._validate_static_network({"ci_dhcp": True, "ci_ip": "bad"}, "disk", True, "bridge"))
        self.assertIsNone(host._validate_static_network({"ci_ip": "bad"}, "iso", False, "network"))

    def test_static_create_writes_validated_settings_into_seed(self):
        capture = []
        host = self.make_seed_host(capture=capture)
        source = self.write_file(self.image_dir, "base.qcow2", "img")

        result = host.create(self.payload(source=source))

        self.assertEqual(VM_UUID, result["uuid"])
        self.assertEqual(1, len(capture))
        config = capture[0]
        install = self.commands.called("virt-install --connect")[0]
        match = re.search(r"mac=([0-9a-f]{2}(?::[0-9a-f]{2}){5})", install)
        self.assertIsNotNone(match)
        self.assertIn("bridge=br0", install)
        self.assertIn('macaddress: "%s"' % match.group(1), config)
        self.assertIn("      dhcp4: false", config)
        self.assertIn('        - "192.168.0.201/24"', config)
        self.assertIn("        - to: default", config)
        self.assertIn('          via: "192.168.0.1"', config)
        self.assertIn("      nameservers:", config)
        self.assertIn('          - "1.1.1.1"', config)
        self.assertIn('          - "8.8.8.8"', config)

    def test_host_default_bridge_accepts_static_without_explicit_network_fields(self):
        capture = []
        host = self.make_seed_host(
            overrides={"default_network_type": "bridge", "default_bridge": "br0"}, capture=capture
        )
        source = self.write_file(self.image_dir, "base.qcow2", "img")
        payload = self.payload(source=source)
        payload.pop("network_type")
        payload.pop("network_source")

        host.create(payload)

        self.assertIn("      dhcp4: false", capture[0])
        self.assertIn("bridge=br0", self.commands.called("virt-install --connect")[0])

    def test_legacy_payload_without_ci_dhcp_keeps_dhcp_seed(self):
        capture = []
        host = self.make_seed_host(capture=capture)
        source = self.write_file(self.image_dir, "base.qcow2", "img")
        payload = self.payload(source=source)
        for key in ("ci_dhcp", "ci_ip", "ci_gateway", "ci_dns"):
            payload.pop(key)

        result = host.create(payload)

        self.assertEqual(VM_UUID, result["uuid"])
        self.assertIn("      dhcp4: true", capture[0])
        self.assertNotIn("addresses:", capture[0])
        self.assertNotIn("routes:", capture[0])

    def test_networks_report_independent_defaults_for_both_modes(self):
        host = self.validation_host(default_network_type="bridge", default_bridge="br0", default_network="preferred-nat")
        data = host.networks()
        self.assertEqual(data["default_source"], "br0")
        self.assertEqual(data["default_bridge"], "br0")
        self.assertEqual(data["default_network"], "preferred-nat")

    def test_dns_string_is_split_and_deduplicated(self):
        host = self.validation_host()
        static = host._validate_static_network(self.payload(ci_dns="1.1.1.1, 8.8.8.8 1.1.1.1"), "disk", True, "bridge")
        self.assertEqual(static["dns"], ["1.1.1.1", "8.8.8.8"])

    def test_failed_partial_seed_is_removed_and_same_name_can_retry(self):
        for mode in ("nonzero", "timeout"):
            with self.subTest(mode=mode):
                name = "seed-retry-" + mode
                seed = os.path.join(self.disk_dir, name + "-seed.iso")
                tool = self.write_file(self.tmp, "genisoimage-" + mode, "#!/bin/sh\n")
                os.chmod(tool, 0o755)
                commands = FakeCommands()
                attempts = [0]

                def partial_seed(_argv):
                    attempts[0] += 1
                    with open(seed, "wb") as stream:
                        stream.write(b"partial")
                    if mode == "timeout" and attempts[0] == 1:
                        raise vmops.OpError("命令执行超时")

                commands.add_sequence(["-volid", "cidata"], [{"returncode": 1, "stderr": "seed generation failed"}, {"returncode": 0}])
                commands.rules[-1]["hook"] = partial_seed
                host = self.make_host(commands, genisoimage=tool)
                with self.assertRaises(vmops.OpError):
                    host._create_seed_iso(name, self.disk_dir, self.payload(), "aa:bb:cc:dd:ee:01")
                self.assertFalse(os.path.lexists(seed))
                result = host._create_seed_iso(name, self.disk_dir, self.payload(), "aa:bb:cc:dd:ee:01")
                self.assertEqual(result, seed)
                self.assertTrue(os.path.isfile(seed))
                self.assertEqual(os.listdir(os.path.join(self.disk_dir, ".vm-platform-cloudinit")), [])

    # -- static_network_defaults：静态 IP 默认联网（有界修复） -----------------

    PROFILES = {
        "br0": {"cidr": "192.168.0.0/24", "gateway": "192.168.0.1", "dns": ["192.168.0.1", "8.8.8.8"]},
        "br1": {"cidr": "10.20.30.0/24", "gateway": "10.20.30.1", "dns": ["10.20.30.1"]},
    }

    def profile_host(self, profiles=None, **overrides):
        data = self.PROFILES if profiles is None else profiles
        return self.validation_host(static_network_defaults=data, **overrides)

    def test_profile_fills_route_and_dns_when_only_ip_is_sent(self):
        capture = []
        host = self.make_seed_host(overrides={"static_network_defaults": self.PROFILES}, capture=capture)
        source = self.write_file(self.image_dir, "base.qcow2", "img")

        result = host.create(self.payload(source=source, ci_gateway="", ci_dns=""))

        self.assertEqual(VM_UUID, result["uuid"])
        install = self.commands.called("virt-install --connect")[0]
        self.assertIn("bridge=br0", install)
        mac = re.search(r"mac=([0-9a-f]{2}(?::[0-9a-f]{2}){5})", install).group(1)
        config = capture[0]
        self.assertIn('macaddress: "%s"' % mac, config)
        self.assertIn("      dhcp4: false", config)
        self.assertIn('        - "192.168.0.201/24"', config)
        self.assertIn("      routes:\n        - to: default\n          via: \"192.168.0.1\"", config)
        self.assertIn(
            "      nameservers:\n        addresses:\n          - \"192.168.0.1\"\n          - \"8.8.8.8\"",
            config,
        )

    def test_four_argument_call_derives_bridge_from_payload_and_config(self):
        host = self.profile_host()
        static = host._validate_static_network(self.payload(ci_gateway="", ci_dns=""), "disk", True, "bridge")
        self.assertEqual("192.168.0.1", static["gateway"])
        self.assertEqual(["192.168.0.1", "8.8.8.8"], static["dns"])

    def test_manual_gateway_is_kept_while_missing_dns_is_filled(self):
        host = self.profile_host()
        static = host._validate_static_network(
            self.payload(ci_gateway="192.168.0.254", ci_dns=""), "disk", True, "bridge", "br0"
        )
        self.assertEqual("192.168.0.254", static["gateway"])
        self.assertEqual(["192.168.0.1", "8.8.8.8"], static["dns"])

        static = host._validate_static_network(
            self.payload(ci_gateway="", ci_dns=["9.9.9.9"]), "disk", True, "bridge", "br0"
        )
        self.assertEqual("192.168.0.1", static["gateway"])
        self.assertEqual(["9.9.9.9"], static["dns"])

    def test_complete_manual_values_are_not_overwritten_by_profile(self):
        host = self.profile_host()
        static = host._validate_static_network(
            self.payload(ci_gateway="192.168.0.2", ci_dns=["1.1.1.1"]), "disk", True, "bridge", "br0"
        )
        self.assertEqual(
            {"address": "192.168.0.201/24", "gateway": "192.168.0.2", "dns": ["1.1.1.1"]}, static
        )

    def test_missing_gateway_or_dns_without_profile_is_rejected_before_writes(self):
        cases = (
            ({"ci_gateway": "", "ci_dns": ""}, "网关"),
            ({"ci_gateway": "192.168.0.1", "ci_dns": ""}, "DNS"),
            ({"ci_gateway": "", "ci_dns": ["1.1.1.1"]}, "网关"),
        )
        for extra, expected in cases:
            with self.subTest(**extra):
                host = self.validation_host()
                with self.assertRaisesRegex(vmops.OpError, expected):
                    host.create(self.payload(**extra))
                self.assert_no_writes()

    def test_profile_from_another_bridge_or_subnet_is_not_applied(self):
        cases = (
            # 只有 br1 的 profile，请求落在 br0
            ({"static_network_defaults": {"br1": self.PROFILES["br1"]}}, {}),
            # br0 的 profile 网段与 guest IP 不同
            ({"static_network_defaults": {"br0": {
                "cidr": "10.20.30.0/24", "gateway": "10.20.30.1", "dns": ["10.20.30.1"]}}}, {}),
            # profile 缺 DNS 视为无效
            ({"static_network_defaults": {"br0": {
                "cidr": "192.168.0.0/24", "gateway": "192.168.0.1"}}}, {}),
            # guest 在 br1 上，profile 只在 br0
            ({"static_network_defaults": {"br0": self.PROFILES["br0"]}},
             {"network_source": "br1", "ci_ip": "10.20.30.201/24"}),
        )
        for config, extra in cases:
            with self.subTest(config=config, extra=extra):
                host = self.validation_host(**config)
                payload = self.payload(ci_gateway="", ci_dns="")
                payload.update(extra)
                with self.assertRaisesRegex(vmops.OpError, "网关"):
                    host.create(payload)
                self.assert_no_writes()

    def test_default_gateway_outside_guest_subnet_is_not_used(self):
        host = self.profile_host()
        with self.assertRaisesRegex(vmops.OpError, "网关"):
            host.create(self.payload(ci_ip="192.168.0.201/28", ci_gateway="", ci_dns=""))
        self.assert_no_writes()

    def test_profile_gateway_is_revalidated_against_guest_before_writes(self):
        cases = (
            ("192.168.0.1/24", "192.168.0.1", "等于虚拟机"),
            ("192.168.0.30/27", "192.168.0.31", "广播地址"),
            ("192.168.0.17/28", "192.168.0.16", "网络地址"),
        )
        for address, gateway, error in cases:
            with self.subTest(address=address, gateway=gateway):
                host = self.profile_host(profiles={"br0": {
                    "cidr": "192.168.0.0/24", "gateway": gateway, "dns": ["8.8.8.8"]
                }})
                with self.assertRaisesRegex(vmops.OpError, error):
                    host.create(self.payload(ci_ip=address, ci_gateway="", ci_dns=""))
                self.assert_no_writes()

    def test_isolated_validation_output_has_empty_gateway_and_dns(self):
        host = self.profile_host()
        static = host._validate_static_network(
            self.payload(ci_isolated=True, ci_gateway="", ci_dns=""), "disk", True, "bridge", "br0"
        )
        self.assertEqual({"address": "192.168.0.201/24", "gateway": "", "dns": []}, static)

    def test_isolated_create_writes_no_route_or_dns(self):
        capture = []
        host = self.make_seed_host(capture=capture)
        source = self.write_file(self.image_dir, "base.qcow2", "img")

        host.create(self.payload(source=source, ci_isolated=True, ci_gateway="", ci_dns=""))

        config = capture[0]
        self.assertIn("      dhcp4: false", config)
        self.assertIn('        - "192.168.0.201/24"', config)
        self.assertNotIn("routes:", config)
        self.assertNotIn("nameservers:", config)

    def test_isolated_with_gateway_or_dns_is_rejected(self):
        cases = (
            {"ci_gateway": "192.168.0.1", "ci_dns": ""},
            {"ci_gateway": "", "ci_dns": ["1.1.1.1"]},
            {},
        )
        for extra in cases:
            with self.subTest(**extra):
                host = self.profile_host()
                with self.assertRaisesRegex(vmops.OpError, "内网"):
                    host.create(self.payload(ci_isolated=True, **extra))
                self.assert_no_writes()

    def test_profiles_do_not_change_dhcp_mode(self):
        host = self.profile_host()
        self.assertIsNone(
            host._validate_static_network({"ci_dhcp": True, "ci_ip": "bad"}, "disk", True, "bridge", "br0")
        )
        self.assertIsNone(
            host._validate_static_network({"ci_isolated": True}, "disk", True, "bridge", "br0")
        )
        text = vmops.build_cloud_init_network_config("aa:bb:cc:dd:ee:05", "ens3")
        self.assertIn("      dhcp4: true", text)
        self.assertNotIn("routes:", text)
        self.assertNotIn("nameservers:", text)

    def test_networks_returns_only_valid_profiles_for_real_bridges(self):
        host = self.validation_host(static_network_defaults={
            "br0": {"cidr": "192.168.0.0/24", "gateway": "192.168.0.1", "dns": ["192.168.0.1", "8.8.8.8"]},
            "br1": {"cidr": "192.168.0.0/24", "gateway": "10.9.9.1", "dns": ["1.1.1.1"]},
            "br2": {"cidr": "172.16.0.0/24", "gateway": "172.16.0.1", "dns": ["172.16.0.1"]},
            "br3": "not-a-dict",
        })
        data = host.networks()
        self.assertEqual(["br0"], list(data["static_network_defaults"]))
        self.assertEqual(
            {"cidr": "192.168.0.0/24", "gateway": "192.168.0.1", "dns": ["192.168.0.1", "8.8.8.8"]},
            data["static_network_defaults"]["br0"],
        )

    def test_networks_defaults_are_empty_without_config(self):
        host = self.validation_host()
        self.assertEqual({}, host.networks()["static_network_defaults"])


if __name__ == "__main__":
    unittest.main()
