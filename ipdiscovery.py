#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主机网桥上的局域网 ARP 地址发现（v1.1.2）。

设计约束（见 docs 中 v1.1.2 IP 发现一节）：
- 默认关闭：只有执行端配置 ``bridge_ip_discovery: true`` 时才由 vmops 启用。
- 只使用 Python 标准库，Python 3.6.8 与 3.12 均可运行。
- 扫描范围只来自宿主机该 Linux 网桥的**本机 IPv4 网段**：用固定 argv
  ``ip -o -4 addr show dev <bridge>`` 只读读取；不接受控制端 payload / ``ci_ip``
  / 任何请求参数里的地址。网段固定安全上限 256 个地址（仅 /24–/30；
  /0–/23、/31、/32 一律跳过），不从 .1 猜网关，不做全网络扫描。
- 只发送/接收 ARP 帧（AF_PACKET/SOCK_RAW）：不修改 ``ip`` 配置、不清邻居缓存、
  不写宿主机或 guest、不安装 QGA、不打开额外 TCP 监听。
- 后台最多一个守护线程，按网桥 30 秒限流合并刷新；``lookup`` 非阻塞。
- 失败（无权限 / 平台不支持 / OSError）只返回 ``unavailable``，绝不让 vms 失败。
- 时钟、睡眠、套接字、线程工厂均可注入，单元测试不接触真实网络、sysfs 与 sleep。
"""

import ipaddress
import os
import re
import socket
import struct
import threading
import time

__all__ = [
    "BridgeScanner",
    "DiscoveryManager",
    "build_arp_request",
    "normalize_scan_result",
    "parse_arp_reply",
    "parse_ipv4_addresses",
    "resolve_bridge_source",
    "select_network",
    "normalize_mac",
    "normalize_bridge",
]

ARP_ETHERTYPE = 0x0806
ARP_HTYPE_ETHERNET = 1
ARP_PTYPE_IPV4 = 0x0800
ARP_HLEN = 6
ARP_PLEN = 4
ARP_OP_REQUEST = 1
ARP_OP_REPLY = 2
ARP_FRAME_SIZE = 14 + 28
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"
ZERO_MAC = "00:00:00:00:00:00"

# 网段上限：IPv4Network.num_addresses <= 256，即 /24（256）到 /30（4）。
MAX_NETWORK_ADDRESSES = 256
MIN_PREFIXLEN = 24
MAX_PREFIXLEN = 30
# 每个 /24 可用地址 254，去掉宿主机自身 ≤ 253 个请求。
MAX_ARP_TARGETS = 253
REQUEST_GAP_SECONDS = 0.002
SCAN_SECONDS = 3.0
# 单轮收包总预算：至少 256 帧，且按目标数放大（253 请求 + 253 回应 + 背景余量）。
MIN_RECEIVE_BUDGET = 256
RECEIVE_BUDGET_FACTOR = 4

SUCCESS_TTL_SECONDS = 120.0
NEGATIVE_TTL_SECONDS = 30.0
MIN_SCAN_INTERVAL_SECONDS = 30.0

DEFAULT_SYSFS_ROOT = "/sys/class/net"

MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")

# 中文短原因（直接进入 vm.ip_status_reason / interface 说明，保持可读不敏感）。
REASON_NOT_BRIDGE = "不是 Linux 网桥"
REASON_NO_HOST_IPV4 = "网桥没有本机 IPv4 地址"
REASON_NETWORK_TOO_LARGE = "网段超出安全上限（仅 /24–/30）"
REASON_NO_HOST_MAC = "网桥缺少本机 MAC"
REASON_NO_PERMISSION = "无权限发送 ARP（需要 root/CAP_NET_RAW）"
REASON_NO_SOCKET = "平台不支持 AF_PACKET，无法主动探测"
REASON_SCAN_FAILED = "ARP 收发失败"
REASON_NO_ANSWER = "未探测到地址"
REASON_BAD_INPUT = "网桥名或 MAC 无效"


def normalize_mac(value):
    """小写并校验 MAC；非法返回空串。"""
    text = str(value or "").strip().lower()
    return text if MAC_RE.match(text) else ""


def normalize_bridge(value):
    """校验 Linux 网桥接口名（≤15 字符）；非法返回空串。"""
    text = str(value or "").strip()
    return text if IFACE_RE.match(text) else ""


def parse_ipv4_addresses(text):
    """解析 ``ip -o -4 addr show dev <bridge>`` 输出为 [(ip, prefixlen)]。"""
    entries = []
    for line in (text or "").splitlines():
        parts = line.split()
        for index, token in enumerate(parts):
            if token != "inet" or index + 1 >= len(parts):
                continue
            address, _, prefix = parts[index + 1].partition("/")
            try:
                ip = ipaddress.IPv4Address(address)
            except ValueError:
                continue
            try:
                prefixlen = int(prefix)
            except (TypeError, ValueError):
                continue
            if prefixlen < 0 or prefixlen > 32:
                continue
            item = (str(ip), prefixlen)
            if item not in entries:
                entries.append(item)
    return entries


def select_network(entries):
    """选首个满足安全上限的网段。

    返回 ``(network, host_ip, reason)``：
    - 命中时 ``network`` 为 IPv4Network、``host_ip`` 为落在该网段的本机地址；
    - 否则前两项为 None，``reason`` 为中文短原因。
    多 IPv4 时只取第一个合法网段，其余忽略。
    """
    if not entries:
        return None, "", REASON_NO_HOST_IPV4
    for ip, prefixlen in entries:
        if prefixlen < MIN_PREFIXLEN or prefixlen > MAX_PREFIXLEN:
            continue
        try:
            network = ipaddress.IPv4Network(u"%s/%d" % (ip, prefixlen), strict=False)
        except ValueError:
            continue
        if network.num_addresses > MAX_NETWORK_ADDRESSES:
            continue
        if ipaddress.IPv4Address(ip) not in network:
            continue
        return network, ip, ""
    return None, "", REASON_NETWORK_TOO_LARGE


def _read_sysfs(bridge, name, sysfs_root):
    path = os.path.join(sysfs_root, bridge, name)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def resolve_bridge_source(runner, bridge, sysfs_root=DEFAULT_SYSFS_ROOT, timeout=10.0):
    """只读解析宿主机网桥的本机 IPv4 网段与 MAC。

    返回 dict：成功 ``{"ok": True, "bridge", "network", "host_ip", "host_mac"}``；
    失败 ``{"ok": False, "reason": 中文短原因}``。runner 与 vmops.run_command 同签名。
    """
    name = normalize_bridge(bridge)
    if not name:
        return {"ok": False, "reason": REASON_BAD_INPUT}
    if not os.path.isdir(os.path.join(sysfs_root, name, "bridge")):
        return {"ok": False, "reason": REASON_NOT_BRIDGE}
    host_mac = normalize_mac(_read_sysfs(name, "address", sysfs_root))
    if not host_mac or host_mac == ZERO_MAC:
        return {"ok": False, "reason": REASON_NO_HOST_MAC}
    try:
        result = runner(["ip", "-o", "-4", "addr", "show", "dev", name], timeout=timeout)
    except Exception:  # noqa: BLE001 - 命令层任何异常都只降级为 unavailable
        return {"ok": False, "reason": REASON_NO_HOST_IPV4}
    if not isinstance(result, dict) or result.get("returncode") != 0:
        return {"ok": False, "reason": REASON_NO_HOST_IPV4}
    network, host_ip, reason = select_network(parse_ipv4_addresses(result.get("stdout")))
    if network is None:
        return {"ok": False, "reason": reason}
    return {
        "ok": True,
        "bridge": name,
        "network": network,
        "host_ip": host_ip,
        "host_mac": host_mac,
    }


def _mac_bytes(mac):
    return bytes(bytearray(int(part, 16) for part in mac.split(":")))


def _format_mac(raw):
    return ":".join("%02x" % byte for byte in bytearray(raw))


def build_arp_request(host_mac, host_ip, target_ip):
    """构造以太网 + ARP 请求帧（广播询问 target_ip 的 MAC）。"""
    mac = normalize_mac(host_mac)
    if not mac:
        raise ValueError("invalid host mac")
    sender = ipaddress.IPv4Address(str(host_ip))
    target = ipaddress.IPv4Address(str(target_ip))
    header = struct.pack("!6s6sH", _mac_bytes(BROADCAST_MAC), _mac_bytes(mac), ARP_ETHERTYPE)
    body = struct.pack(
        "!HHBBH6s4s6s4s",
        ARP_HTYPE_ETHERNET,
        ARP_PTYPE_IPV4,
        ARP_HLEN,
        ARP_PLEN,
        ARP_OP_REQUEST,
        _mac_bytes(mac),
        sender.packed,
        b"\x00\x00\x00\x00\x00\x00",
        target.packed,
    )
    return header + body


def parse_arp_reply(frame, host_mac, host_ip, network, allowed_macs=None):
    """校验一帧 ARP 应答；合法返回 ``{"mac", "ip"}``，否则 None。

    校验项：长度、ethertype、hardware/protocol 类型与长度、oper==reply、
    以太网源地址 == ARP sender MAC、ARP target IP/MAC == 本机、
    sender IP 在当前网段且非网络/广播/本机地址、sender MAC 在请求集合内。
    """
    if not isinstance(frame, (bytes, bytearray)) or len(frame) < ARP_FRAME_SIZE:
        return None
    data = bytes(frame[:ARP_FRAME_SIZE])
    ethertype = struct.unpack("!H", data[12:14])[0]
    if ethertype != ARP_ETHERTYPE:
        return None
    htype, ptype, hlen, plen, oper = struct.unpack("!HHBBH", data[14:22])
    if htype != ARP_HTYPE_ETHERNET or ptype != ARP_PTYPE_IPV4:
        return None
    if hlen != ARP_HLEN or plen != ARP_PLEN or oper != ARP_OP_REPLY:
        return None
    eth_src = data[6:12]
    sender_mac = data[22:28]
    sender_ip = data[28:32]
    target_mac = data[32:38]
    target_ip = data[38:42]
    if eth_src != sender_mac:
        return None
    mac = normalize_mac(host_mac)
    if not mac:
        return None
    if target_mac != _mac_bytes(mac) or target_ip != ipaddress.IPv4Address(str(host_ip)).packed:
        return None
    try:
        address = ipaddress.IPv4Address(sender_ip)
    except ValueError:
        return None
    try:
        space = network if isinstance(network, ipaddress.IPv4Network) else ipaddress.IPv4Network(str(network))
    except ValueError:
        return None
    if address not in space:
        return None
    if address == space.network_address or address == space.broadcast_address:
        return None
    if str(address) == str(host_ip):
        return None
    sender = _format_mac(sender_mac)
    if allowed_macs is not None and sender not in set(allowed_macs):
        return None
    return {"mac": sender, "ip": str(address)}


def _open_arp_socket(bridge):
    """打开绑定到指定网桥的 AF_PACKET 原始套接字（构建期只读、无持久改动）。"""
    family = getattr(socket, "AF_PACKET", None)
    if family is None:
        raise OSError(REASON_NO_SOCKET)
    handle = socket.socket(family, socket.SOCK_RAW, socket.htons(ARP_ETHERTYPE))
    try:
        handle.bind((bridge, 0))
        handle.settimeout(0.0)
    except OSError:
        handle.close()
        raise
    return handle


class BridgeScanner(object):
    """一次活动 ARP 探测：枚举宿主机网桥网段地址并按 MAC 收集应答。

    可注入：runner（``ip`` 命令）、sysfs_root、clock、sleep、socket_factory。
    单元测试传入假 runner / 临时 sysfs 目录 / 假套接字即可，不接触真实网络。
    """

    def __init__(self, runner=None, sysfs_root=DEFAULT_SYSFS_ROOT, clock=None, sleep=None,
                 socket_factory=None):
        self._runner = runner
        self._sysfs_root = sysfs_root
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._socket_factory = socket_factory or _open_arp_socket

    def __call__(self, bridge, macs):
        return self.scan(bridge, macs)

    def targets(self, bridge):
        """返回 ``(targets, source)``：本轮可探测地址列表与来源信息。"""
        source = resolve_bridge_source(self._runner, bridge, self._sysfs_root)
        if not source.get("ok"):
            return [], source
        targets = []
        for address in source["network"].hosts():
            text = str(address)
            if text == source["host_ip"]:
                continue
            targets.append(text)
            if len(targets) >= MAX_ARP_TARGETS:
                break
        return targets, source

    def scan(self, bridge, macs):
        """返回 ``{"status": ok|unavailable, "reason": str, "addresses": {mac: [ip, ...]}}``。

        真实 guest 同一 MAC 可能同时持有多个地址（例如 192.168.0.28 与 192.168.0.211），
        因此本轮不会在找到第一个地址后提前结束：走完全部受限 targets，按到达顺序去重
        收集每个 MAC 的地址列表。
        """
        allowed = set()
        for item in macs or []:
            mac = normalize_mac(item)
            if mac:
                allowed.add(mac)
        if not allowed:
            return {"status": "unavailable", "reason": "没有可探测的 MAC 地址", "addresses": {}}
        targets, source = self.targets(bridge)
        if not source.get("ok"):
            return {"status": "unavailable", "reason": source.get("reason") or REASON_NOT_BRIDGE,
                    "addresses": {}}
        if not targets:
            return {"status": "unavailable", "reason": "网段内没有可探测地址", "addresses": {}}
        try:
            handle = self._socket_factory(source["bridge"])
        except (OSError, ValueError):
            return {"status": "unavailable", "reason": REASON_NO_PERMISSION, "addresses": {}}
        found = {}
        failure = ""
        deadline = self._clock() + SCAN_SECONDS
        # 总收包预算按“全部帧”计（含本机出站帧与背景 ARP），必须覆盖 253 个请求、
        # 253 个回应和背景流量，避免合法回应被背景帧挤掉后漏掉多地址中的后一个。
        budget = [max(MIN_RECEIVE_BUDGET, RECEIVE_BUDGET_FACTOR * len(targets))]
        try:
            try:
                handle.settimeout(0.0)
            except OSError:
                pass
            for target in targets:
                if self._clock() >= deadline:
                    break
                try:
                    handle.send(build_arp_request(source["host_mac"], source["host_ip"], target))
                except OSError:
                    failure = failure or REASON_SCAN_FAILED
                    continue
                self._drain(handle, found, allowed, source, deadline, budget, 1)
                self._sleep(REQUEST_GAP_SECONDS)
            # 发送结束后再留一段接收窗口：内核回包可能晚于最后一个请求。
            try:
                handle.settimeout(0.05)
            except OSError:
                pass
            self._drain(handle, found, allowed, source, deadline, budget, 4)
        except OSError:
            failure = failure or REASON_SCAN_FAILED
        finally:
            try:
                handle.close()
            except OSError:
                pass
        if found:
            return {"status": "ok", "reason": "", "addresses": found}
        return {"status": "unavailable", "reason": failure or REASON_NO_ANSWER, "addresses": {}}

    def _drain(self, handle, found, allowed, source, deadline, budget, idle_limit):
        idle = 0
        while budget[0] > 0 and idle < idle_limit and self._clock() < deadline:
            try:
                frame = handle.recv(2048)
            except OSError:
                idle += 1
                continue
            if not frame:
                idle += 1
                continue
            # 预算针对每一帧（无论是否合法目标回应）递减，背景流量不能无限消耗时间。
            budget[0] -= 1
            idle = 0
            parsed = parse_arp_reply(frame, source["host_mac"], source["host_ip"],
                                     source["network"], allowed)
            if parsed:
                addresses = found.setdefault(parsed["mac"], [])
                if parsed["ip"] not in addresses:
                    addresses.append(parsed["ip"])


def _daemon_thread(target):
    thread = threading.Thread(target=target)
    thread.daemon = True
    return thread


def normalize_scan_result(result):
    """把扫描器返回值归一为 ``{"status","reason","addresses":{mac:[ip,...]}}``。

    非 dict（线程异常、扫描器返回 None/字符串等）一律降级为 ``unavailable``，
    避免出现永久 ``discovering``；地址按扫描器给出的顺序去重。
    """
    if not isinstance(result, dict):
        return {"status": "unavailable", "reason": REASON_SCAN_FAILED, "addresses": {}}
    raw = result.get("addresses")
    if not isinstance(raw, dict):
        raw = {}
    addresses = {}
    for key, value in raw.items():
        mac = normalize_mac(key)
        if not mac:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        ips = []
        for item in items:
            try:
                ip = str(ipaddress.IPv4Address(str(item)))
            except ValueError:
                continue
            if ip not in ips:
                ips.append(ip)
        if ips:
            addresses[mac] = ips
    status = result.get("status")
    reason = str(result.get("reason") or "")
    if addresses:
        status = "ok"
    else:
        status = "unavailable"
        reason = reason or REASON_NO_ANSWER
    return {"status": status, "reason": reason, "addresses": addresses}


class DiscoveryManager(object):
    """常驻后台发现管理器：非阻塞 lookup + 30 秒限流的单线程扫描。

    - 缓存键为 ``(bridge, mac)``，不同网桥/主机互不串数据；
    - 每个 MAC 保留本轮全部地址列表（同一 guest 可能同时有多个 IPv4）；
    - 成功缓存 120 秒、无响应负缓存 30 秒，过期不再展示旧地址；
    - 每网桥最短 30 秒一次扫描，重复刷新合并进 pending，最多一个 worker 线程；
    - ``retain_targets`` 按当前 running 网桥接口快照剪枝缓存/pending/在途目标，
      并用每目标 generation 丢弃迟到扫描结果（移除后再加入也不会回填旧结果）；
    - 时钟（clock）与线程工厂可注入，单元测试完全确定且不 sleep。
    """

    def __init__(self, scanner, clock=None, thread_factory=None, log=None):
        self._scanner = scanner
        self._clock = clock or time.monotonic
        self._thread_factory = thread_factory or _daemon_thread
        self._log = log or (lambda message: None)
        # 可重入：测试注入的同步线程工厂会在 request() 持锁期间直接执行 worker。
        self._lock = threading.RLock()
        self._cache = {}      # (bridge, mac) -> {"addresses": [ip], "reason": str, "expires": float}
        self._epoch = {}      # (bridge, mac) -> int：目标被移除时递增，用于丢弃迟到结果
        self._bridges = {}    # bridge -> {"last_start","running","pending","running_macs"}
        self._worker = None
        self._active = ""
        self._stopped = False

    # -- 查询 ---------------------------------------------------------------
    def lookup(self, bridge, mac):
        """非阻塞查询单个 (网桥, MAC)。

        返回 ``{"status","address","addresses","reason"}``：``addresses`` 为本轮全部地址
        （顺序稳定），``address`` 为其中第一个，仅用于兼容既有单值调用。
        首次未知返回 discovering；后台扫描完成仍无应答则 unavailable；
        之后 30 秒（负缓存）再尝试。
        """
        name = normalize_bridge(bridge)
        address_mac = normalize_mac(mac)
        if not name or not address_mac:
            return {"status": "unavailable", "address": "", "addresses": [],
                    "reason": REASON_BAD_INPUT}
        now = self._clock()
        with self._lock:
            entry = self._cache.get((name, address_mac))
            if entry and entry["expires"] > now:
                if entry["addresses"]:
                    return {"status": "resolved", "address": entry["addresses"][0],
                            "addresses": list(entry["addresses"]), "reason": ""}
                return {"status": "unavailable", "address": "", "addresses": [],
                        "reason": entry.get("reason") or REASON_NO_ANSWER}
            if self._stopped:
                return {"status": "unavailable", "address": "", "addresses": [],
                        "reason": "探测已停止"}
            state = self._bridges.get(name) or {}
            if state.get("running"):
                return {"status": "discovering", "address": "", "addresses": [],
                        "reason": "正在局域网探测"}
            if address_mac in (state.get("pending") or set()):
                return {"status": "discovering", "address": "", "addresses": [],
                        "reason": "等待局域网探测"}
        return {"status": "unavailable", "address": "", "addresses": [], "reason": "尚未探测"}

    def retain_targets(self, targets):
        """按当前 running 网桥接口快照剪枝；返回 ``{"kept": n, "pruned": n}``。

        ``targets`` 形如 ``{bridge: [macs]}``，应包含本轮所有 running 桥接口（含已有
        被动地址的接口）。不在快照内的缓存、pending 与在途目标会立即移除并递增
        generation，因此暂停/关机/删除/换接口后的迟到扫描结果不会被回填。
        """
        wanted = {}
        for bridge, macs in (targets or {}).items():
            name = normalize_bridge(bridge)
            if not name:
                continue
            bucket = wanted.setdefault(name, set())
            for item in macs or []:
                mac = normalize_mac(item)
                if mac:
                    bucket.add(mac)
        now = self._clock()
        kept = 0
        pruned = 0
        with self._lock:
            for key in list(self._cache.keys()):
                bridge, mac = key
                entry = self._cache[key]
                if entry.get("expires", 0.0) <= now or mac not in wanted.get(bridge, set()):
                    del self._cache[key]
                    self._drop_key_locked(key)
                    pruned += 1
                else:
                    kept += 1
            for bridge in list(self._bridges.keys()):
                state = self._bridges[bridge]
                alive = wanted.get(bridge, set())
                tracked = set(state.get("pending") or ())
                tracked.update(state.get("running_macs") or ())
                for mac in sorted(tracked):
                    if mac in alive:
                        continue
                    (state.get("pending") or set()).discard(mac)
                    (state.get("running_macs") or set()).discard(mac)
                    self._drop_key_locked((bridge, mac))
                    pruned += 1
                idle = not state.get("running") and not state.get("pending")
                last_start = state.get("last_start") or 0.0
                window_passed = not last_start or now - last_start >= MIN_SCAN_INTERVAL_SECONDS
                # 即使全部目标暂时离开，也保留已开始扫描的 30 秒限流窗口。
                if idle and window_passed:
                    del self._bridges[bridge]
        return {"kept": kept, "pruned": pruned}

    def request(self, requests):
        """批量登记待探测的 ``{bridge: [macs]}``；需要时启动后台 worker。

        返回实际排队的 ``{bridge: [macs]}``。窗口内（<30 秒）的刷新只合并进
        pending，不新起线程；已缓存（成功或负缓存未过期）的 MAC 直接跳过。
        """
        now = self._clock()
        queued = {}
        with self._lock:
            if self._stopped:
                return queued
            for bridge, macs in (requests or {}).items():
                name = normalize_bridge(bridge)
                if not name:
                    continue
                keys = []
                for item in macs or []:
                    mac = normalize_mac(item)
                    if not mac or mac in keys:
                        continue
                    entry = self._cache.get((name, mac))
                    if entry and entry["expires"] > now:
                        continue
                    keys.append(mac)
                if not keys:
                    continue
                state = self._bridges.get(name)
                if state is None:
                    state = {"last_start": 0.0, "running": False, "pending": set(),
                             "running_macs": set()}
                    self._bridges[name] = state
                state["pending"].update(keys)
                queued[name] = sorted(state["pending"])
            self._start_worker_locked()
        return queued

    def status(self):
        """后台扫描状态快照（运行中/待探测），供诊断与测试查询。"""
        with self._lock:
            worker = self._worker
            bridges = {}
            for name, state in self._bridges.items():
                bridges[name] = {
                    "running": bool(state.get("running")),
                    "pending": sorted(state.get("pending") or []),
                    "running_macs": sorted(state.get("running_macs") or []),
                    "last_start": state.get("last_start") or 0.0,
                }
            return {
                "stopped": self._stopped,
                "worker_running": bool(worker is not None and getattr(worker, "is_alive", lambda: False)()),
                "active_bridge": self._active,
                "bridges": bridges,
            }

    def shutdown(self, timeout=1.0):
        """停止接受新任务并尽力等待后台线程退出。"""
        with self._lock:
            self._stopped = True
            for state in self._bridges.values():
                (state.get("pending") or set()).clear()
                (state.get("running_macs") or set()).clear()
                state["running"] = False
            self._active = ""
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            join = getattr(worker, "join", None)
            if callable(join):
                try:
                    join(timeout)
                except (RuntimeError, OSError):
                    pass

    # -- 内部：generation 与失败降级 ----------------------------------------
    def _drop_key_locked(self, key):
        """目标被移除/替换：递增 generation，使所有在途旧结果失效。"""
        self._epoch[key] = self._epoch.get(key, 0) + 1

    def _epochs_for_locked(self, bridge, macs):
        return dict((mac, self._epoch.get((bridge, mac), 0)) for mac in macs)

    def _fail_pending_locked(self, reason):
        """线程无法启动等硬失败：把待探测目标直接记为负结果，绝不停留在 discovering。"""
        for bridge in sorted(self._bridges.keys()):
            state = self._bridges[bridge]
            macs = set(state.get("pending") or ())
            if not macs:
                continue
            state["pending"] = set()
            self._apply_result_locked(bridge, macs, self._epochs_for_locked(bridge, macs),
                                      {"status": "unavailable", "reason": reason,
                                       "addresses": {}})

    # -- 后台 worker --------------------------------------------------------
    def _start_worker_locked(self):
        if self._stopped or self._worker is not None:
            return
        if self._claimable_locked() is None:
            return
        try:
            worker = self._thread_factory(self._worker_loop)
        except (RuntimeError, OSError) as exc:  # 线程创建失败只影响本功能
            self._log("ip discovery: 无法启动后台线程：%s" % exc)
            self._fail_pending_locked("无法启动局域网探测线程")
            return
        self._worker = worker
        try:
            worker.start()
        except (RuntimeError, OSError) as exc:
            self._worker = None
            self._log("ip discovery: 后台线程启动失败：%s" % exc)
            self._fail_pending_locked("无法启动局域网探测线程")

    def _claimable_locked(self):
        now = self._clock()
        for name in sorted(self._bridges.keys()):
            state = self._bridges[name]
            if state.get("running") or not state.get("pending"):
                continue
            if state.get("last_start") and now - state["last_start"] < MIN_SCAN_INTERVAL_SECONDS:
                continue
            return name
        return None

    def _claim_locked(self):
        name = self._claimable_locked()
        if name is None:
            return None
        state = self._bridges[name]
        macs = set(state["pending"])
        state["pending"] = set()
        state["running"] = True
        state["running_macs"] = set(macs)
        state["last_start"] = self._clock()
        self._active = name
        return name, macs, self._epochs_for_locked(name, macs)

    def _worker_loop(self):
        while True:
            with self._lock:
                if self._stopped:
                    self._worker = None
                    return
                job = self._claim_locked()
                if job is None:
                    self._worker = None
                    self._active = ""
                    return
            bridge, macs, epochs = job
            try:
                result = self._scanner(bridge, sorted(macs))
            except Exception as exc:  # noqa: BLE001 - 扫描异常不得影响 vms
                self._log("ip discovery: 扫描异常：%s" % exc)
                result = None
            with self._lock:
                if self._stopped:
                    self._worker = None
                    return
                self._apply_result_locked(bridge, macs, epochs, result)

    def _apply_result_locked(self, bridge, macs, epochs, result):
        data = normalize_scan_result(result)
        addresses = data["addresses"]
        reason = data["reason"]
        now = self._clock()
        state = self._bridges.get(bridge)
        if state is not None:
            if state.get("running"):
                self._active = ""
            state["running"] = False
            state["running_macs"] = set()
        epochs = epochs or {}
        for mac in macs:
            address = normalize_mac(mac)
            if not address:
                continue
            if self._epoch.get((bridge, address), 0) != epochs.get(address, 0):
                continue    # 目标已被移除/替换：丢弃迟到结果，不回填缓存
            ips = list(addresses.get(address) or [])
            if ips:
                self._cache[(bridge, address)] = {
                    "addresses": ips, "reason": "", "expires": now + SUCCESS_TTL_SECONDS,
                }
            else:
                self._cache[(bridge, address)] = {
                    "addresses": [], "reason": reason or REASON_NO_ANSWER,
                    "expires": now + NEGATIVE_TTL_SECONDS,
                }
