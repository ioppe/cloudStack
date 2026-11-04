#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ipdiscovery 单元测试（不接触真实网络、sysfs、sleep 与线程）。

覆盖：网段安全上限与非网桥拒绝、ARP 帧构建/解析（合法与各类非法帧）、
扫描器降级路径、后台管理器缓存隔离/过期/负缓存/限流/异步/关闭。
运行：python3 -m unittest discover -s tests -v
"""

import os
import shutil
import socket
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ipdiscovery  # noqa: E402

HOST_MAC = "52:54:00:9c:98:ea"
HOST_IP = "192.168.0.30"
GUEST_MAC = "52:54:00:3a:56:b5"
GUEST_IP = "192.168.0.160"
NETWORK = "192.168.0.0/24"
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"


def mac_bytes(mac):
    return bytes(bytearray(int(part, 16) for part in mac.split(":")))


def arp_frame(sender_mac=GUEST_MAC, sender_ip=GUEST_IP, target_mac=HOST_MAC, target_ip=HOST_IP,
              oper=ipdiscovery.ARP_OP_REPLY, ethertype=ipdiscovery.ARP_ETHERTYPE, eth_src=None,
              htype=1, ptype=0x0800, hlen=6, plen=4, eth_dst=HOST_MAC, truncate=None):
    head = mac_bytes(eth_dst) + mac_bytes(eth_src or sender_mac) + struct.pack("!H", ethertype)
    body = struct.pack("!HHBBH", htype, ptype, hlen, plen, oper)
    body += mac_bytes(sender_mac) + socket.inet_aton(sender_ip)
    body += mac_bytes(target_mac) + socket.inet_aton(target_ip)
    frame = head + body
    return frame[:truncate] if truncate else frame


class FakeRunner(object):
    """固定 argv 的只读命令替身：只记录调用并返回预设输出。"""

    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls = []

    def __call__(self, args, timeout=120, cwd=None):
        self.calls.append({"argv": [str(item) for item in args], "timeout": timeout})
        if self.error:
            raise self.error
        return {"returncode": self.returncode, "stdout": self.stdout, "stderr": ""}


class FakeSocket(object):
    """假 AF_PACKET 套接字：发送只记录帧，接收按队列吐帧后超时。"""

    def __init__(self, frames=None, socket_error=None, send_error=None, repeat=None):
        self.frames = list(frames or [])
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.recv_calls = 0
        self.socket_error = socket_error
        self.send_error = send_error
        # repeat：始终返回该帧（模拟无界背景流量），用于验证收包总预算有界。
        self.repeat = repeat

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, data):
        if self.send_error:
            raise self.send_error
        self.sent.append(bytes(data))
        return len(data)

    def recv(self, size):
        self.recv_calls += 1
        if self.frames:
            return self.frames.pop(0)
        if self.repeat is not None:
            return self.repeat
        raise socket.timeout("no data")

    def close(self):
        self.closed = True


class FakeClock(object):
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BridgeFixture(unittest.TestCase):
    """临时 sysfs 目录：<root>/<bridge>/bridge 为目录，address 文件为宿主机 MAC。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ipdiscovery-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_bridge(self, name="br0", host_mac=HOST_MAC):
        base = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(base, "bridge"))
        with open(os.path.join(base, "address"), "w", encoding="utf-8") as handle:
            handle.write(host_mac + "\n")
        return base

    def ip_output(self, *entries):
        lines = []
        for index, value in enumerate(entries):
            lines.append("%d: br0    inet %s brd 192.168.0.255 scope global br0\\       "
                         "valid_lft forever preferred_lft forever" % (index + 2, value))
        return "\n".join(lines) + ("\n" if lines else "")


class NetworkBoundTests(BridgeFixture):
    def test_slash24_and_slash30_are_accepted(self):
        network, host_ip, reason = ipdiscovery.select_network([("192.168.0.30", 24)])
        self.assertEqual(str(network), NETWORK)
        self.assertEqual(host_ip, HOST_IP)
        self.assertEqual(reason, "")
        network, host_ip, _ = ipdiscovery.select_network([("10.0.0.1", 30)])
        self.assertEqual(network.num_addresses, 4)
        self.assertEqual(host_ip, "10.0.0.1")

    def test_large_and_tiny_prefixes_are_skipped(self):
        for prefix in (0, 8, 22, 23, 31, 32):
            network, _, reason = ipdiscovery.select_network([(HOST_IP, prefix)])
            self.assertIsNone(network, prefix)
            self.assertTrue(reason, prefix)
        self.assertLessEqual(ipdiscovery.MAX_NETWORK_ADDRESSES, 256)

    def test_first_eligible_network_wins(self):
        entries = [("10.10.0.1", 16), ("192.168.0.30", 24), ("172.16.0.1", 24)]
        network, host_ip, _ = ipdiscovery.select_network(entries)
        self.assertEqual(str(network), NETWORK)
        self.assertEqual(host_ip, HOST_IP)

    def test_ip_address_parsing_ignores_ipv6_and_garbage(self):
        text = ("2: br0    inet 192.168.0.30/24 brd 192.168.0.255 scope global br0\n"
                "2: br0    inet6 fe80::1/64 scope link\n"
                "3: br1    inet nope scope global br1\n")
        self.assertEqual(ipdiscovery.parse_ipv4_addresses(text), [(HOST_IP, 24)])

    def test_resolve_bridge_source_rejects_non_bridge(self):
        os.makedirs(os.path.join(self.tmp, "eth0"))
        result = ipdiscovery.resolve_bridge_source(FakeRunner(), "eth0", sysfs_root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], ipdiscovery.REASON_NOT_BRIDGE)

    def test_resolve_bridge_source_rejects_bad_interface_name(self):
        result = ipdiscovery.resolve_bridge_source(FakeRunner(), "br0;rm -rf /", sysfs_root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], ipdiscovery.REASON_BAD_INPUT)

    def test_resolve_bridge_source_requires_host_mac(self):
        base = os.path.join(self.tmp, "br0")
        os.makedirs(os.path.join(base, "bridge"))
        result = ipdiscovery.resolve_bridge_source(FakeRunner(), "br0", sysfs_root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], ipdiscovery.REASON_NO_HOST_MAC)

    def test_resolve_bridge_source_without_host_ipv4(self):
        self.make_bridge()
        runner = FakeRunner(stdout="")
        result = ipdiscovery.resolve_bridge_source(runner, "br0", sysfs_root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], ipdiscovery.REASON_NO_HOST_IPV4)
        self.assertEqual(runner.calls[0]["argv"], ["ip", "-o", "-4", "addr", "show", "dev", "br0"])

    def test_resolve_bridge_source_rejects_large_network(self):
        self.make_bridge()
        runner = FakeRunner(stdout=self.ip_output("192.168.0.30/16"))
        result = ipdiscovery.resolve_bridge_source(runner, "br0", sysfs_root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], ipdiscovery.REASON_NETWORK_TOO_LARGE)

    def test_scanner_targets_exclude_host_and_stay_in_subnet(self):
        self.make_bridge()
        runner = FakeRunner(stdout=self.ip_output(HOST_IP + "/24"))
        scanner = ipdiscovery.BridgeScanner(runner=runner, sysfs_root=self.tmp)
        targets, source = scanner.targets("br0")
        self.assertTrue(source["ok"])
        self.assertEqual(len(targets), 253)
        self.assertNotIn(HOST_IP, targets)
        for address in targets:
            self.assertTrue(address.startswith("192.168.0."))
        self.assertEqual(scanner.targets("nobridge")[1]["reason"], ipdiscovery.REASON_NOT_BRIDGE)


class ArpFrameTests(unittest.TestCase):
    def test_request_frame_layout(self):
        frame = ipdiscovery.build_arp_request(HOST_MAC, HOST_IP, GUEST_IP)
        self.assertEqual(len(frame), ipdiscovery.ARP_FRAME_SIZE)
        self.assertEqual(frame[0:6], mac_bytes(BROADCAST_MAC))
        self.assertEqual(frame[6:12], mac_bytes(HOST_MAC))
        self.assertEqual(struct.unpack("!H", frame[12:14])[0], ipdiscovery.ARP_ETHERTYPE)
        htype, ptype, hlen, plen, oper = struct.unpack("!HHBBH", frame[14:22])
        self.assertEqual((htype, ptype, hlen, plen, oper), (1, 0x0800, 6, 4, 1))
        self.assertEqual(frame[22:28], mac_bytes(HOST_MAC))
        self.assertEqual(socket.inet_ntoa(frame[28:32]), HOST_IP)
        self.assertEqual(frame[32:38], b"\x00\x00\x00\x00\x00\x00")
        self.assertEqual(socket.inet_ntoa(frame[38:42]), GUEST_IP)

    def parse(self, frame, allowed=(GUEST_MAC,), network=NETWORK):
        return ipdiscovery.parse_arp_reply(frame, HOST_MAC, HOST_IP, network, set(allowed))

    def test_valid_reply_is_accepted(self):
        parsed = self.parse(arp_frame())
        self.assertEqual(parsed, {"mac": GUEST_MAC, "ip": GUEST_IP})

    def test_reply_with_wrong_target_ip_is_rejected(self):
        self.assertIsNone(self.parse(arp_frame(target_ip="192.168.0.99")))

    def test_reply_with_wrong_target_mac_is_rejected(self):
        self.assertIsNone(self.parse(arp_frame(target_mac="52:54:00:00:00:01")))

    def test_reply_from_other_subnet_is_rejected(self):
        self.assertIsNone(self.parse(arp_frame(sender_ip="10.0.0.9")))

    def test_reply_for_unrequested_mac_is_rejected(self):
        self.assertIsNone(self.parse(arp_frame(sender_mac="52:54:00:aa:bb:cc")))

    def test_reply_with_network_or_host_address_is_rejected(self):
        self.assertIsNone(self.parse(arp_frame(sender_ip="192.168.0.0")))
        self.assertIsNone(self.parse(arp_frame(sender_ip="192.168.0.255")))
        self.assertIsNone(self.parse(arp_frame(sender_ip=HOST_IP)))

    def test_non_reply_and_malformed_frames_are_rejected(self):
        self.assertIsNone(self.parse(arp_frame(oper=ipdiscovery.ARP_OP_REQUEST)))
        self.assertIsNone(self.parse(arp_frame(ethertype=0x0800)))
        self.assertIsNone(self.parse(arp_frame(htype=6)))
        self.assertIsNone(self.parse(arp_frame(ptype=0x86DD)))
        self.assertIsNone(self.parse(arp_frame(hlen=8)))
        self.assertIsNone(self.parse(arp_frame(plen=16)))
        self.assertIsNone(self.parse(arp_frame(truncate=20)))
        self.assertIsNone(self.parse(b""))
        self.assertIsNone(self.parse(None))

    def test_ethernet_source_must_match_arp_sender(self):
        frame = arp_frame(eth_src="52:54:00:11:22:33")
        self.assertIsNone(self.parse(frame))

    def test_reply_to_other_host_is_rejected(self):
        frame = arp_frame()
        self.assertIsNone(ipdiscovery.parse_arp_reply(frame, "52:54:00:00:00:09", HOST_IP,
                                                      NETWORK, {GUEST_MAC}))


class SteppingClock(object):
    """每次读取推进固定步长的假时钟，用于验证 3 秒总时限。"""

    def __init__(self, start=1000.0, step=0.02):
        self.now = start
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class BridgeScannerTests(BridgeFixture):
    def make_scanner(self, frames, socket_error=None, send_error=None, repeat=None, clock=None):
        self.make_bridge()
        self.runner = FakeRunner(stdout=self.ip_output(HOST_IP + "/24"))
        self.handle = FakeSocket(frames, socket_error=socket_error, send_error=send_error,
                                 repeat=repeat)
        self.slept = []

        def factory(bridge):
            if socket_error:
                raise socket_error
            return self.handle

        return ipdiscovery.BridgeScanner(
            runner=self.runner, sysfs_root=self.tmp, clock=clock or FakeClock(),
            sleep=self.slept.append, socket_factory=factory)

    def test_scan_keeps_all_addresses_and_walks_every_target(self):
        # 同一 MAC 的真实场景：192.168.0.28 与 192.168.0.211；末帧重复用于验证去重。
        frames = [
            arp_frame(sender_ip="192.168.0.28"),
            arp_frame(sender_ip="192.168.0.211"),
            arp_frame(sender_ip="192.168.0.28"),
        ]
        scanner = self.make_scanner(frames)
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["addresses"], {GUEST_MAC: ["192.168.0.28", "192.168.0.211"]})
        self.assertTrue(self.handle.closed)
        # 找到第一个地址后不再提前停止：253 个受限目标全部发送，间隔 >=2ms。
        self.assertEqual(len(self.handle.sent), ipdiscovery.MAX_ARP_TARGETS)
        self.assertEqual(len(self.slept), ipdiscovery.MAX_ARP_TARGETS)
        self.assertTrue(all(gap >= 0.002 for gap in self.slept))
        self.assertEqual(ipdiscovery.SCAN_SECONDS, 3.0)

    def test_scan_without_permission_is_unavailable(self):
        scanner = self.make_scanner([], socket_error=OSError("operation not permitted"))
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], ipdiscovery.REASON_NO_PERMISSION)
        self.assertEqual(result["addresses"], {})

    def test_scan_ignores_frames_for_other_macs(self):
        scanner = self.make_scanner([arp_frame(sender_mac="52:54:00:aa:bb:cc", sender_ip="192.168.0.9")])
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["addresses"], {})
        self.assertTrue(self.handle.closed)

    def test_scan_send_error_degrades_without_raising(self):
        scanner = self.make_scanner([], send_error=OSError("network down"))
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(self.handle.closed)

    def test_background_frames_do_not_hide_late_target_replies(self):
        # 400 个非目标/背景帧超过旧的 len(targets)+16 预算，目标回应在后段仍必须收齐。
        junk = [arp_frame(sender_mac="52:54:00:aa:bb:cc", sender_ip="192.168.0.9") for _ in range(400)]
        frames = junk + [
            arp_frame(sender_ip="192.168.0.28"),
            arp_frame(sender_ip="192.168.0.211"),
        ]
        scanner = self.make_scanner(frames)
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["addresses"], {GUEST_MAC: ["192.168.0.28", "192.168.0.211"]})
        self.assertTrue(self.handle.closed)

    def test_receive_budget_is_bounded_with_endless_background(self):
        junk = arp_frame(sender_mac="52:54:00:aa:bb:cc", sender_ip="192.168.0.9")
        scanner = self.make_scanner([], repeat=junk)
        result = scanner.scan("br0", [GUEST_MAC])
        self.assertEqual(result["status"], "unavailable")
        budget = max(ipdiscovery.MIN_RECEIVE_BUDGET,
                     ipdiscovery.RECEIVE_BUDGET_FACTOR * ipdiscovery.MAX_ARP_TARGETS)
        self.assertGreater(self.handle.recv_calls, 0)
        self.assertLessEqual(self.handle.recv_calls, budget)
        self.assertTrue(self.handle.closed)

    def test_scan_respects_three_second_deadline(self):
        scanner = self.make_scanner([], clock=SteppingClock())
        scanner.scan("br0", [GUEST_MAC])
        self.assertGreater(len(self.handle.sent), 0)
        self.assertLess(len(self.handle.sent), ipdiscovery.MAX_ARP_TARGETS)
        self.assertTrue(self.handle.closed)

    def test_scan_rejects_non_bridge_target(self):
        scanner = self.make_scanner([])
        result = scanner.scan("eth0", [GUEST_MAC])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], ipdiscovery.REASON_NOT_BRIDGE)
        self.assertEqual(self.handle.sent, [])


class DeferredThread(object):
    """记录 target 但不执行的假线程，用于验证 lookup 不阻塞。"""

    def __init__(self, target):
        self.target = target
        self.started = False
        self.alive = False

    def start(self):
        self.started = True
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        return None

    def run(self):
        self.target()
        self.alive = False


class ThreadRegistry(object):
    def __init__(self):
        self.threads = []

    def __call__(self, target):
        thread = DeferredThread(target)
        self.threads.append(thread)
        return thread

    def run_all(self):
        for thread in list(self.threads):
            if thread.started and thread.alive:
                thread.run()


class FakeScanner(object):
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {
            "status": "unavailable", "reason": "未探测到地址", "addresses": {}}
        self.error = error
        self.calls = []
        self.on_call = None

    def __call__(self, bridge, macs):
        self.calls.append((bridge, tuple(sorted(macs))))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return self.result


class DiscoveryManagerTests(unittest.TestCase):
    def make_manager(self, scanner, clock=None):
        self.clock = clock or FakeClock()
        self.threads = ThreadRegistry()
        self.manager = ipdiscovery.DiscoveryManager(
            scanner, clock=self.clock, thread_factory=self.threads)
        self.addCleanup(self.manager.shutdown)
        return self.manager

    def test_lookup_is_non_blocking_and_reports_discovering(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.assertEqual(len(self.threads.threads), 1)
        self.assertTrue(self.threads.threads[0].started)   # 只登记线程，不阻塞 lookup
        self.assertEqual(scanner.calls, [])                # worker 尚未执行
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "discovering")
        self.assertEqual(result["addresses"], [])

    def test_scan_result_becomes_resolved_and_cached(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(manager.lookup("br0", GUEST_MAC),
                         {"status": "resolved", "address": GUEST_IP,
                          "addresses": [GUEST_IP], "reason": ""})
        # 缓存内再次请求不触发新扫描
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 1)
        self.assertEqual(manager.status()["bridges"]["br0"]["pending"], [])

    def test_multiple_addresses_are_cached_in_order_and_deduped(self):
        scanner = FakeScanner({"status": "ok", "reason": "",
                               "addresses": {GUEST_MAC: ["192.168.0.28", "192.168.0.211",
                                                         "192.168.0.28"]}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["addresses"], ["192.168.0.28", "192.168.0.211"])
        self.assertEqual(result["address"], "192.168.0.28")   # 兼容单值调用

    def test_no_response_becomes_unavailable_not_stuck(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "未探测到地址")
        self.assertEqual(result["addresses"], [])

    def test_cache_is_isolated_by_bridge_and_mac(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "resolved")
        self.assertEqual(manager.lookup("br1", GUEST_MAC)["status"], "unavailable")
        self.assertEqual(manager.lookup("br0", "52:54:00:aa:bb:cc")["status"], "unavailable")

    def test_success_cache_expires_after_ttl(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.clock.advance(ipdiscovery.SUCCESS_TTL_SECONDS + 1)
        self.assertNotEqual(manager.lookup("br0", GUEST_MAC)["address"], GUEST_IP)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 2)

    def test_negative_cache_blocks_retry_for_window_then_retries(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.clock.advance(ipdiscovery.NEGATIVE_TTL_SECONDS - 1)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 1)
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "unavailable")
        self.clock.advance(2)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 2)

    def test_single_worker_and_merged_requests_per_bridge(self):
        other_mac = "52:54:00:aa:bb:cc"
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        manager.request({"br0": [GUEST_MAC, other_mac]})
        self.assertEqual(len(self.threads.threads), 1)     # 不积压线程
        self.threads.run_all()
        self.assertEqual(scanner.calls, [("br0", (GUEST_MAC, other_mac))])

    def test_bridge_rate_limit_defers_new_mac_within_window(self):
        other_mac = "52:54:00:aa:bb:cc"
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        manager.request({"br0": [other_mac]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 1)
        self.assertEqual(manager.lookup("br0", other_mac)["status"], "discovering")
        self.clock.advance(ipdiscovery.MIN_SCAN_INTERVAL_SECONDS + 1)
        manager.request({"br0": [other_mac]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 2)

    def test_scanner_exception_does_not_break_lookup(self):
        scanner = FakeScanner(error=RuntimeError("boom"))
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], ipdiscovery.REASON_SCAN_FAILED)

    def test_non_dict_scan_result_is_unavailable(self):
        manager = self.make_manager(lambda bridge, macs: "garbage")
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], ipdiscovery.REASON_SCAN_FAILED)
        self.assertEqual(manager.status()["bridges"]["br0"]["pending"], [])

    def test_thread_creation_failure_is_negative_not_pending(self):
        def factory(target):
            raise RuntimeError("can't start new thread")

        manager = ipdiscovery.DiscoveryManager(
            FakeScanner(), clock=FakeClock(), thread_factory=factory)
        self.addCleanup(manager.shutdown)
        manager.request({"br0": [GUEST_MAC]})
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("探测线程", result["reason"])
        self.assertEqual(manager.status()["bridges"]["br0"]["pending"], [])

    def test_status_reports_background_activity(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        self.assertFalse(manager.status()["worker_running"])
        manager.request({"br0": [GUEST_MAC]})
        snapshot = manager.status()
        self.assertTrue(snapshot["worker_running"])
        self.assertEqual(snapshot["bridges"]["br0"]["pending"], [GUEST_MAC])
        self.threads.run_all()
        self.assertFalse(manager.status()["worker_running"])
        self.assertEqual(manager.status()["bridges"]["br0"]["pending"], [])

    def test_retain_targets_keeps_valid_targets(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        stats = manager.retain_targets({"br0": [GUEST_MAC]})
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["pruned"], 0)
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["addresses"], [GUEST_IP])

    def test_retain_targets_prunes_stopped_or_deleted_vm(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        stats = manager.retain_targets({})          # VM 关机/删除或接口换桥
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "unavailable")

    def test_retain_targets_expires_cache(self):
        scanner = FakeScanner({"status": "ok", "reason": "", "addresses": {GUEST_MAC: GUEST_IP}})
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.clock.advance(ipdiscovery.SUCCESS_TTL_SECONDS + 1)
        stats = manager.retain_targets({"br0": [GUEST_MAC]})
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "unavailable")
        self.assertEqual(manager.status()["bridges"], {})   # 窗口过后无 pending 的桥状态被清理

    def test_retain_targets_clears_pending_and_bridge_state(self):
        manager = self.make_manager(FakeScanner())
        manager.request({"br0": [GUEST_MAC]})
        manager.retain_targets({})
        self.assertEqual(manager.status()["bridges"], {})
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "unavailable")

    def test_in_flight_result_is_discarded_when_target_removed_mid_scan(self):
        scanner = FakeScanner({"status": "ok", "reason": "",
                               "addresses": {GUEST_MAC: ["192.168.0.28"]}})
        manager = self.make_manager(scanner)
        scanner.on_call = lambda: manager.retain_targets({})   # 扫描期间 VM 停止/删除
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["addresses"], [])              # 迟到结果不回填

    def test_removed_target_is_not_backfilled_after_readd(self):
        scanner = FakeScanner({"status": "ok", "reason": "",
                               "addresses": {GUEST_MAC: ["192.168.0.28"]}})
        manager = self.make_manager(scanner)
        scanner.on_call = lambda: manager.retain_targets({})
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        scanner.on_call = None
        scanner.result = {"status": "ok", "reason": "",
                          "addresses": {GUEST_MAC: ["192.168.0.211"]}}
        self.clock.advance(ipdiscovery.MIN_SCAN_INTERVAL_SECONDS + 1)
        manager.retain_targets({"br0": [GUEST_MAC]})   # 重新加入同一 key
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        result = manager.lookup("br0", GUEST_MAC)
        self.assertEqual(result["addresses"], ["192.168.0.211"])
        self.assertNotIn("192.168.0.28", result["addresses"])

    def test_removing_all_targets_does_not_reset_bridge_rate_limit(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        manager.retain_targets({})
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "unavailable")
        manager.retain_targets({"br0": [GUEST_MAC]})
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 1)
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["status"], "discovering")
        self.clock.advance(ipdiscovery.MIN_SCAN_INTERVAL_SECONDS + 1)
        manager.request({"br0": [GUEST_MAC]})
        self.threads.run_all()
        self.assertEqual(len(scanner.calls), 2)

    def test_shutdown_stops_further_scans(self):
        scanner = FakeScanner()
        manager = self.make_manager(scanner)
        manager.shutdown()
        manager.request({"br0": [GUEST_MAC]})
        self.assertEqual(self.threads.threads, [])
        self.assertEqual(manager.lookup("br0", GUEST_MAC)["reason"], "探测已停止")

    def test_invalid_mac_or_bridge_is_unavailable(self):
        manager = self.make_manager(FakeScanner())
        self.assertEqual(manager.lookup("br0", "nope")["status"], "unavailable")
        self.assertEqual(manager.lookup("", GUEST_MAC)["status"], "unavailable")
        self.assertEqual(manager.request({"bad bridge name": [GUEST_MAC]}), {})


if __name__ == "__main__":
    unittest.main()
