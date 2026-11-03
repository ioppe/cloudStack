#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VM Platform 执行端本地操作层（libvirt / qemu-img / virt-install）。

约束（见 docs/IMPLEMENTATION.md 执行端一节）：
- 只用 Python 标准库，Python 3.6.8（.30）与 3.12（.150）均可运行。
- 外部命令固定 argv，不经过 shell；控制端不能传入任意命令。
- 读操作（status/vms/images/networks/vnc_target）不修改本机文件。
- 写操作（create/action/config）执行前先做完整预检，失败时不做危险回落。
- 镜像模板目录（image_dirs）只读；可变磁盘只允许在 disk_dirs 内。
- 不输出包含 cloud-init 密码的命令参数或 payload；结果只保留安全摘要。
"""

import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

GIB = 1024 ** 3

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
VM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
LINUX_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}$")
IMAGE_SUFFIXES = (".iso", ".qcow2", ".img", ".raw")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
ACTIONS = ("start", "shutdown", "stop", "reboot", "suspend", "resume", "delete")
OPERATIONS = ("status", "vms", "images", "networks", "create", "action", "config", "vnc_target")

ACTION_LABELS = {
    "start": "启动",
    "shutdown": "关机",
    "stop": "强制停止",
    "reboot": "重启",
    "suspend": "暂停",
    "resume": "恢复",
    "delete": "删除",
}

STATE_LABELS = {
    "running": "运行中",
    "shut off": "已关机",
    "paused": "已暂停",
    "saved": "已保存",
    "in shutdown": "关机中",
    "pmsuspended": "休眠中",
    "crashed": "已崩溃",
    "deleted": "已删除",
}

# 默认配置刻意保守：目录为空、允许写关闭，必须在执行端显式配置后才能创建/改动虚拟机。
DEFAULT_CONFIG = {
    "libvirt_uri": "qemu:///system",
    "allow_writes": False,
    "read_only_uuids": [],
    "image_dirs": [],
    "disk_dirs": [],
    "storage_report_dir": "",
    "default_bridge": "br0",
    "default_network": "default",
    "default_network_type": "network",
    "default_timezone": "Asia/Shanghai",
    "socket_path": "/run/vm-platform/agent.sock",
    "virsh": "virsh",
    "virt_install": "virt-install",
    "qemu_img": "qemu-img",
    "genisoimage": "",
    "max_vcpus": 256,
    "max_memory_gb": 1024,
    "max_disk_gb": 20000,
    "command_timeout": 120,
    "install_timeout": 3600,
}


class OpError(Exception):
    """可安全回传控制端的操作错误（消息中不含密码等敏感内容）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def bool_value(value):
    return value in (True, "true", "True", "1", 1, "on", "yes")


def parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_positive_int(payload, key, minimum, maximum):
    try:
        value = int(str(payload.get(key)).strip())
    except (TypeError, ValueError):
        raise OpError("%s 必须是整数" % key)
    if value < minimum or value > maximum:
        raise OpError("%s 必须在 %d 到 %d 之间" % (key, minimum, maximum))
    return value


def human_size(num):
    try:
        value = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f PB" % value


def bytes_to_gb(num):
    try:
        value = int(num)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return int((value + GIB - 1) // GIB)


def pct(numerator, denominator):
    if not denominator:
        return None
    return round((float(numerator) / float(denominator)) * 100, 1)


def kib_human(kib):
    return human_size(int(kib) * 1024)


def parse_key_values(text):
    values = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def parse_domstats(text):
    stats = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("Domain:"):
            continue
        key, value = stripped.split("=", 1)
        stats[key.strip()] = value.strip()
    return stats


def parse_table_rows(text):
    rows = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        lowered = stripped.lower()
        if lowered.startswith(("interface", "type", "target", "name ")):
            continue
        rows.append(stripped)
    return rows


def parse_block_list(text):
    disks = []
    for row in parse_table_rows(text):
        parts = row.split(None, 3)
        if len(parts) < 4:
            continue
        disks.append(
            {
                "type": parts[0],
                "device": parts[1],
                "target": parts[2],
                "source": parts[3],
            }
        )
    return disks


def parse_dhcp_leases(text):
    mapping = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        mac = None
        address = None
        for token in stripped.split():
            if MAC_RE.match(token):
                mac = token.lower()
                continue
            if "/" in token and address is None:
                try:
                    address = str(ipaddress.ip_interface(token).ip)
                except ValueError:
                    continue
        if mac and address:
            mapping.setdefault(mac, [])
            if address not in mapping[mac]:
                mapping[mac].append(address)
    return mapping


def normalized_state(info):
    state = str(info.get("State", "")).strip()
    managed_save = str(info.get("Managed save", "")).strip().lower() == "yes"
    if managed_save and state.lower() == "shut off":
        return "saved"
    return state.lower()


def state_label(state):
    return STATE_LABELS.get(state, state or "未知")


def parse_vnc_display(display):
    text = (display or "").strip()
    if not text.startswith("vnc://"):
        return None
    target = text[len("vnc://"):]
    if "/" in target:
        target = target.split("/", 1)[0]
    if ":" not in target:
        return None
    host, value = target.rsplit(":", 1)
    host = host or "127.0.0.1"
    if host in ("[::1]",):
        host = "::1"
    try:
        display_num = int(value)
    except ValueError:
        return None
    if display_num < 0:
        return None
    return {"host": host, "port": 5900 + display_num, "display": display_num}


def generate_mac():
    value = uuid.uuid4().int
    octets = [(value >> shift) & 0xFF for shift in (16, 8, 0)]
    return "52:54:00:%02x:%02x:%02x" % tuple(octets)


def sanitize_hostname(name):
    hostname = re.sub(r"[^A-Za-z0-9-]", "-", str(name)).strip("-").lower()
    return hostname[:63] or "vm"


def os_install_args(version_text):
    """按 virt-install 版本返回 OS 识别相关参数。

    virt-install 1.x（.30，EL7）没有 --osinfo，使用 --os-type linux；
    virt-install 4.x（.150 的 4.1）不再接受旧式 os-variant 组合，使用
    --osinfo detect=on,require=off，检测不到时也不中断安装。
    版本无法识别时按新版处理：宁可让不支持的参数明确失败，也不猜测旧参数。
    """
    major = None
    match = re.search(r"(\d+)\.(\d+)", version_text or "")
    if match:
        major = int(match.group(1))
    if major is not None and major < 4:
        return ["--os-type", "linux"]
    return ["--osinfo", "detect=on,require=off"]


def guess_image_metadata(path, image_kind):
    name = os.path.basename(str(path)).lower()
    meta = {
        "alias": os.path.basename(str(path)),
        "os_family": "",
        "os_version": "",
        "image_type": "install_iso" if image_kind == "iso" else "cloud_image",
        "default_username": "",
        "cloud_init_supported": 1 if image_kind == "disk" else 0,
        "notes": "",
    }
    if "ubuntu" in name or "noble" in name or "jammy" in name or "focal" in name:
        meta["os_family"] = "Ubuntu"
        meta["default_username"] = "ubuntu"
        if "noble" in name or "24.04" in name or "2404" in name:
            meta["os_version"] = "24.04"
        elif "jammy" in name or "22.04" in name or "2204" in name:
            meta["os_version"] = "22.04"
        elif "focal" in name or "20.04" in name or "2004" in name:
            meta["os_version"] = "20.04"
    elif "centos" in name:
        meta["os_family"] = "CentOS"
        meta["default_username"] = "centos"
        meta["os_version"] = "7" if "7" in name else ""
    elif "debian" in name:
        meta["os_family"] = "Debian"
        meta["default_username"] = "debian"
    elif "windows" in name or "win" in name:
        meta["os_family"] = "Windows"
        meta["default_username"] = "Administrator"
        meta["cloud_init_supported"] = 0
        meta["image_type"] = "install_iso" if image_kind == "iso" else "disk_image"
    if meta["os_family"] and meta["os_version"]:
        meta["alias"] = "%s %s - %s" % (meta["os_family"], meta["os_version"], os.path.basename(str(path)))
    elif meta["os_family"]:
        meta["alias"] = "%s - %s" % (meta["os_family"], os.path.basename(str(path)))
    return meta


def _normalize_dirs(value, label):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise OpError("%s 必须是路径数组" % label)
    dirs = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if "\x00" in text or not os.path.isabs(text):
            raise OpError("%s 只允许绝对路径" % label)
        resolved = os.path.realpath(text)
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def build_config(overrides=None):
    """合并默认配置并做归一化（目录解析为真实绝对路径）。"""
    config = dict(DEFAULT_CONFIG)
    for key, value in (overrides or {}).items():
        config[key] = value
    config["libvirt_uri"] = str(config.get("libvirt_uri") or "qemu:///system")
    config["image_dirs"] = _normalize_dirs(config.get("image_dirs"), "image_dirs")
    config["disk_dirs"] = _normalize_dirs(config.get("disk_dirs"), "disk_dirs")
    config["read_only_uuids"] = [
        str(item).strip().lower() for item in (config.get("read_only_uuids") or []) if str(item).strip()
    ]
    config["allow_writes"] = bool_value(config.get("allow_writes", False))
    config["socket_path"] = os.path.abspath(str(config.get("socket_path") or DEFAULT_CONFIG["socket_path"]))
    storage_dir = str(config.get("storage_report_dir") or "").strip()
    if storage_dir and not os.path.isabs(storage_dir):
        raise OpError("storage_report_dir 只允许绝对路径")
    config["storage_report_dir"] = os.path.realpath(storage_dir) if storage_dir else ""
    for key in ("max_vcpus", "max_memory_gb", "max_disk_gb", "command_timeout", "install_timeout"):
        try:
            config[key] = max(1, int(str(config.get(key) or DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            config[key] = DEFAULT_CONFIG[key]
    for key in ("default_bridge", "default_network", "default_network_type", "default_timezone",
                "virsh", "virt_install", "qemu_img", "genisoimage"):
        config[key] = str(config.get(key) or DEFAULT_CONFIG[key])
    return config


def load_config(path=None):
    data = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except FileNotFoundError:
            loaded = {}
        except (OSError, ValueError) as exc:
            raise OpError("执行端配置无法读取：%s" % (getattr(exc, "strerror", None) or "格式无效"))
        if loaded in (None, ""):
            loaded = {}
        if not isinstance(loaded, dict):
            raise OpError("执行端配置必须是 JSON 对象")
        data = loaded
    return build_config(data)


def run_command(args, timeout=120, cwd=None):
    """固定 argv 执行外部命令，返回 {returncode,stdout,stderr}。"""
    env = os.environ.copy()
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    if not env.get("PATH"):
        env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    argv = [str(item) for item in args]
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            close_fds=True,
        )
    except subprocess.TimeoutExpired:
        raise OpError("命令执行超时")
    except OSError as exc:
        raise OpError("无法执行 %s：%s" % (os.path.basename(argv[0]), getattr(exc, "strerror", None) or "未知错误"))
    return {"returncode": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}


def build_cloud_init_user_data(vm_name, username, password, ssh_key, timezone):
    hostname = sanitize_hostname(vm_name)
    lines = [
        "#cloud-config",
        "preserve_hostname: false",
        "hostname: %s" % json.dumps(hostname),
        "manage_etc_hosts: true",
        "timezone: %s" % json.dumps(timezone),
        "disable_root: false",
        "ssh_pwauth: %s" % ("true" if password else "false"),
        "users:",
        "  - default",
        "  - name: %s" % json.dumps(username),
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: false",
    ]
    if ssh_key:
        lines.extend(["    ssh_authorized_keys:", "      - %s" % json.dumps(ssh_key)])
    if password:
        lines.extend(
            [
                "chpasswd:",
                "  expire: false",
                "  users:",
                "    - name: %s" % json.dumps(username),
                "      password: %s" % json.dumps(password),
                "      type: text",
            ]
        )
    return "\n".join(lines) + "\n"


def build_cloud_init_network_config(mac, iface, static=None):
    """生成 network-config v2，匹配指定 MAC 的网卡。

    static 为 None 时保持原有 DHCP 输出（兼容旧 seed 协议）；否则为已校验的
    静态 IPv4 设置：{"address": "192.168.0.201/24", "gateway": "" 或 IPv4,
    "dns": ["1.1.1.1", ...]}。
    """
    lines = [
        "network:",
        "  version: 2",
        "  ethernets:",
        "    nic0:",
        "      match:",
        "        macaddress: %s" % json.dumps(str(mac).lower()),
        "      set-name: %s" % json.dumps(iface),
    ]
    if static is None:
        lines.extend(["      dhcp4: true", "      dhcp6: false"])
        return "\n".join(lines) + "\n"
    lines.extend([
        "      dhcp4: false",
        "      dhcp6: false",
        "      addresses:",
        "        - %s" % json.dumps(str(static["address"])),
    ])
    gateway = str(static.get("gateway") or "").strip()
    if gateway:
        lines.extend([
            "      routes:",
            "        - to: default",
            "          via: %s" % json.dumps(gateway),
        ])
    dns = list(static.get("dns") or [])
    if dns:
        lines.append("      nameservers:")
        lines.append("        addresses:")
        for item in dns:
            lines.append("          - %s" % json.dumps(str(item)))
    return "\n".join(lines) + "\n"


def build_cloud_init_meta_data(vm_name):
    return "instance-id: %s\nlocal-hostname: %s\n" % (
        json.dumps("iid-%s-%s" % (vm_name, uuid.uuid4().hex[:8])),
        json.dumps(sanitize_hostname(vm_name)),
    )


# ---------------------------------------------------------------------------
# 本地主机
# ---------------------------------------------------------------------------


class Host(object):
    """一台执行端的本机操作集合。--serve 模式下实例常驻，CPU 采样得以保留。"""

    def __init__(self, config, runner=None, log=None, clock=None):
        self.config = config if isinstance(config, dict) else build_config(config)
        self._runner = runner or run_command
        self._log = log or (lambda message: None)
        self._clock = clock or time.time
        self._cpu_samples = {}
        self._cpu_lock = threading.Lock()
        self._secrets = set()
        self.mutation_lock = threading.Lock()

    # -- 命令 ---------------------------------------------------------------
    def _binary(self, key):
        return self.config.get(key) or DEFAULT_CONFIG[key]

    def run(self, args, timeout=None, cwd=None):
        if timeout is None:
            timeout = self.config.get("command_timeout", 120)
        argv = [str(item) for item in args]
        return self._runner(argv, timeout=timeout, cwd=cwd)

    def virsh(self, *args, **kwargs):
        timeout = kwargs.pop("timeout", None)
        if kwargs:
            raise TypeError("unexpected keyword arguments")
        argv = [self._binary("virsh"), "--connect", self.config["libvirt_uri"]]
        argv.extend(str(item) for item in args)
        return self.run(argv, timeout=timeout)

    def scrub(self, text):
        value = "" if text is None else str(text)
        for secret in self._secrets:
            if secret:
                value = value.replace(secret, "***")
        return value

    def _command_error(self, result):
        text = (result.get("stderr") or "").strip() or (result.get("stdout") or "").strip() or "命令执行失败"
        return OpError(self.scrub(text))

    def require_ok(self, result):
        if result.get("returncode") != 0:
            raise self._command_error(result)
        return result

    # -- 路径与安全 ---------------------------------------------------------
    def _inside_any(self, path, dirs):
        real = os.path.realpath(path)
        for base in dirs or []:
            base_real = os.path.realpath(base)
            if real == base_real or real.startswith(base_real.rstrip(os.sep) + os.sep):
                return True
        return False

    @staticmethod
    def _reject_symlink_components(path_norm, base_norm):
        rel = os.path.relpath(path_norm, base_norm)
        if rel in (".", "", os.curdir):
            return
        current = base_norm
        for part in rel.split(os.sep):
            if part in ("", os.curdir):
                continue
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise OpError("路径中不允许包含符号链接")

    def _resolve_inside(self, value, dirs, label):
        if not isinstance(value, str) or not value.strip():
            raise OpError("请填写%s" % label)
        text = value.strip()
        if "\x00" in text or not os.path.isabs(text):
            raise OpError("%s必须是绝对路径" % label)
        norm = os.path.normpath(text)
        base = None
        for candidate in dirs or []:
            base_norm = os.path.normpath(candidate)
            if norm == base_norm or norm.startswith(base_norm.rstrip(os.sep) + os.sep):
                base = base_norm
                break
        if base is None:
            raise OpError("%s必须位于执行端配置的目录内" % label)
        self._reject_symlink_components(norm, base)
        return norm

    def _template_image(self, value, required_suffix=None, label="镜像文件"):
        dirs = self.config.get("image_dirs") or []
        if not dirs:
            raise OpError("执行端未配置镜像模板目录，禁止读取或创建")
        path = self._resolve_inside(value, dirs, label)
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in IMAGE_SUFFIXES:
            raise OpError("只允许 ISO、qcow2、img 或 raw 镜像")
        if required_suffix and suffix != required_suffix:
            raise OpError("请选择 %s 文件" % required_suffix)
        if not os.path.isfile(path):
            raise OpError("镜像文件不存在")
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise OpError("镜像文件类型无效")
        return path

    def _disk_dir_base(self, value):
        dirs = self.config.get("disk_dirs") or []
        if not dirs:
            raise OpError("执行端未配置磁盘目录，禁止新建或删除磁盘")
        if value in (None, ""):
            base = dirs[0]
        else:
            text = str(value).strip()
            if "\x00" in text or not os.path.isabs(text):
                raise OpError("磁盘目录必须是绝对路径")
            base = self._resolve_inside(text, dirs, "磁盘目录")
        if os.path.islink(base):
            raise OpError("磁盘目录不允许是符号链接")
        if not os.path.isdir(base):
            try:
                os.makedirs(base, mode=0o755, exist_ok=True)
            except OSError as exc:
                raise OpError("磁盘目录不可用：%s" % (getattr(exc, "strerror", None) or "无法创建"))
        if os.path.islink(base) or not os.path.isdir(base):
            raise OpError("磁盘目录不可用")
        if not os.access(base, os.W_OK):
            raise OpError("磁盘目录不可写")
        return base

    def _mutable_disk_path(self, value):
        dirs = self.config.get("disk_dirs") or []
        if not dirs:
            raise OpError("执行端未配置磁盘目录，禁止修改磁盘文件")
        path = self._resolve_inside(value, dirs, "磁盘路径")
        if not os.path.isfile(path):
            raise OpError("磁盘文件不存在")
        return path

    # -- 域名解析（UUID 优先） ----------------------------------------------
    def _domain_names(self):
        result = self.require_ok(self.virsh("list", "--all", "--name"))
        return [line.strip() for line in result["stdout"].splitlines() if line.strip()]

    def _running_names(self):
        result = self.require_ok(self.virsh("list", "--name"))
        return [line.strip() for line in result["stdout"].splitlines() if line.strip()]

    def _domain_exists(self, name):
        return self.virsh("dominfo", name)["returncode"] == 0

    def _domain_uuid(self, name):
        result = self.virsh("domuuid", name)
        if result["returncode"] != 0:
            return ""
        value = result["stdout"].strip().lower()
        return value if UUID_RE.match(value) else ""

    def _domain_info(self, name):
        result = self.virsh("dominfo", name)
        if result["returncode"] != 0:
            raise OpError("虚拟机不存在或无法读取信息")
        return parse_key_values(result["stdout"])

    def _domain_state(self, name):
        result = self.virsh("domstate", name)
        if result["returncode"] != 0:
            raise OpError("虚拟机不存在或无法读取状态")
        return result["stdout"].strip().lower()

    def resolve_domain(self, uuid_text):
        """校验 UUID 并用 domname 解析名称，再核对 domuuid，拒绝名称冒充 UUID。"""
        text = str(uuid_text or "").strip()
        if not UUID_RE.match(text):
            raise OpError("虚拟机 UUID 格式无效")
        uuid_lower = text.lower()
        result = self.virsh("domname", text)
        if result["returncode"] != 0:
            raise OpError("虚拟机不存在或 UUID 未登记")
        name = result["stdout"].strip()
        if not name or len(name.splitlines()) > 1 or not VM_NAME_RE.match(name):
            raise OpError("虚拟机名称异常，拒绝操作")
        check = self.virsh("domuuid", name)
        if check["returncode"] != 0 or check["stdout"].strip().lower() != uuid_lower:
            raise OpError("虚拟机 UUID 与名称不一致，拒绝操作")
        return {"uuid": uuid_lower, "name": name}

    def _require_writes_allowed(self, uuid_text=None):
        if not self.config.get("allow_writes"):
            raise OpError("执行端配置为只读，禁止写操作")
        if uuid_text:
            blocked = set(self.config.get("read_only_uuids") or [])
            if str(uuid_text).lower() in blocked:
                raise OpError("该虚拟机在执行端被标记为只读，禁止写操作")

    # -- 只读操作 -----------------------------------------------------------
    def status(self, payload=None):
        cpu_count, memory_total, memory_available = self._host_resources()
        disk_total, disk_free = self._storage_usage()
        all_names = self._domain_names()
        running = self._running_names()
        return {
            "hostname": socket.gethostname(),
            "os": self._os_pretty_name(),
            "cpu_count": cpu_count,
            "memory_total": memory_total,
            "memory_available": memory_available,
            "disk_total": disk_total,
            "disk_free": disk_free,
            "vm_count": len(all_names),
            "running_count": len(running),
            "kvm": os.path.exists("/dev/kvm"),
        }

    def _host_resources(self):
        cpu_count = 0
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("processor"):
                        cpu_count += 1
        except OSError:
            cpu_count = 0
        if not cpu_count:
            cpu_count = os.cpu_count() or 0
        memory = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts:
                        memory[key.strip()] = parse_int(parts[0]) * 1024
        except OSError:
            memory = {}
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable")
        if available is None:
            available = memory.get("MemFree", 0) + memory.get("Buffers", 0) + memory.get("Cached", 0)
        return cpu_count, total, available or 0

    def _storage_dir(self):
        if self.config.get("storage_report_dir"):
            return self.config["storage_report_dir"]
        for candidate in (self.config.get("disk_dirs") or []) + (self.config.get("image_dirs") or []):
            if os.path.isdir(candidate):
                return candidate
        return ""

    def _storage_usage(self):
        path = self._storage_dir()
        if not path:
            return 0, 0
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return 0, 0
        return usage.total, usage.free

    @staticmethod
    def _os_pretty_name():
        try:
            with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return "Linux"

    def _domain_interfaces(self, name):
        result = self.virsh("domiflist", name)
        if result["returncode"] != 0:
            return []
        interfaces = []
        for row in parse_table_rows(result["stdout"]):
            parts = row.split()
            if len(parts) < 5:
                continue
            interfaces.append(
                {
                    "interface": parts[0],
                    "type": parts[1],
                    "source": parts[2],
                    "model": parts[3],
                    "mac": parts[4].lower(),
                }
            )
        return interfaces

    def _domain_disks(self, name):
        result = self.virsh("domblklist", name, "--details")
        if result["returncode"] != 0:
            return []
        return parse_block_list(result["stdout"])

    def _domain_stats(self, name):
        result = self.virsh("domstats", name, "--cpu-total", "--balloon", "--block", timeout=20)
        if result["returncode"] != 0:
            return {}
        return parse_domstats(result["stdout"])

    def _agent_ips(self, name):
        result = self.virsh("domifaddr", name, "--source", "agent", timeout=8)
        if result["returncode"] != 0:
            return {}
        mapping = {}
        for row in parse_table_rows(result["stdout"]):
            parts = row.split()
            if len(parts) < 4:
                continue
            mac = parts[1].lower()
            if not MAC_RE.match(mac):
                continue
            address = parts[-1].split("/", 1)[0]
            try:
                ip = str(ipaddress.ip_address(address))
            except ValueError:
                continue
            mapping.setdefault(mac, [])
            if ip not in mapping[mac]:
                mapping[mac].append(ip)
        return mapping

    def _dhcp_leases(self):
        """NAT 地址：virsh net-dhcp-leases（配合 qemu-guest-agent 结果）。"""
        mapping = {}
        result = self.virsh("net-list", "--all", "--name")
        if result["returncode"] != 0:
            return mapping
        for network in [line.strip() for line in result["stdout"].splitlines() if line.strip()]:
            leases = self.virsh("net-dhcp-leases", network)
            if leases["returncode"] != 0:
                continue
            for mac, ips in parse_dhcp_leases(leases["stdout"]).items():
                mapping.setdefault(mac, [])
                for ip in ips:
                    if ip not in mapping[mac]:
                        mapping[mac].append(ip)
        return mapping

    def _neigh_map(self):
        mapping = {}
        try:
            result = self.run(["ip", "neigh", "show"], timeout=10)
        except OpError:
            result = {"returncode": 1, "stdout": "", "stderr": ""}
        if result.get("returncode") == 0:
            for line in (result.get("stdout") or "").splitlines():
                parts = line.split()
                if "lladdr" not in parts:
                    continue
                try:
                    ip = parts[0]
                    mac = parts[parts.index("lladdr") + 1].lower()
                except (IndexError, ValueError):
                    continue
                if not MAC_RE.match(mac):
                    continue
                mapping.setdefault(mac, [])
                if ip not in mapping[mac]:
                    mapping[mac].append(ip)
        arp_path = "/proc/net/arp"
        if os.path.isfile(arp_path):
            try:
                with open(arp_path, "r", encoding="utf-8", errors="ignore") as handle:
                    lines = handle.read().splitlines()[1:]
            except OSError:
                lines = []
            for line in lines:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, mac = parts[0], parts[3].lower()
                if not MAC_RE.match(mac) or mac == "00:00:00:00:00:00":
                    continue
                mapping.setdefault(mac, [])
                if ip not in mapping[mac]:
                    mapping[mac].append(ip)
        return mapping

    def _cpu_metrics(self, uuid_text, stats, vcpus):
        cpu_time = parse_int(stats.get("cpu.time"), None)
        if cpu_time is None:
            return {"percent": None, "text": "未知"}
        now = self._clock()
        with self._cpu_lock:
            previous = self._cpu_samples.get(uuid_text)
            self._cpu_samples[uuid_text] = (now, cpu_time)
        percent = None
        if previous:
            elapsed = max(now - previous[0], 0.001)
            delta = max(cpu_time - previous[1], 0)
            percent = round(((delta / 1000000000.0) / elapsed / max(vcpus, 1)) * 100, 1)
            percent = max(0, min(percent, 999.0))
        return {"percent": percent, "text": "%0.1f%%" % percent if percent is not None else "采样中"}

    @staticmethod
    def _memory_metrics(stats, info):
        available = parse_int(stats.get("balloon.available"))
        unused = parse_int(stats.get("balloon.unused"))
        current = parse_int(stats.get("balloon.current"))
        if not current:
            used_memory = str(info.get("Used memory", "")).split()
            current = parse_int(used_memory[0]) if used_memory else 0
        if available and unused >= 0:
            used = max(available - unused, 0)
            return {
                "percent": pct(used, available),
                "used_human": kib_human(used),
                "total_human": kib_human(available),
                "text": "%s / %s" % (kib_human(used), kib_human(available)),
            }
        if current:
            return {"percent": None, "used_human": "", "total_human": kib_human(current),
                    "text": "已分配 %s" % kib_human(current)}
        return {"percent": None, "used_human": "", "total_human": "", "text": "未知"}

    def _disk_capacity(self, path):
        if not path or not os.path.isfile(path):
            return 0
        try:
            result = self.run([self._binary("qemu_img"), "info", "--output=json", path], timeout=60)
        except OpError:
            return 0
        if result.get("returncode") == 0:
            try:
                parsed = json.loads(result.get("stdout") or "{}")
            except ValueError:
                parsed = {}
            size = parse_int(parsed.get("virtual-size"))
            if size:
                return size
        try:
            plain = self.run([self._binary("qemu_img"), "info", path], timeout=60)
        except OpError:
            return 0
        for line in (plain.get("stdout") or "").splitlines():
            if line.startswith("virtual size:"):
                match = re.search(r"\((\d+) bytes\)", line)
                if match:
                    return parse_int(match.group(1))
        return 0

    def _disk_metrics(self, disks, stats):
        by_path = {}
        block_count = parse_int(stats.get("block.count"))
        for index in range(block_count):
            path = stats.get("block.%d.path" % index)
            if path:
                by_path[path] = {
                    "allocation": parse_int(stats.get("block.%d.allocation" % index)),
                    "capacity": parse_int(stats.get("block.%d.capacity" % index)),
                    "physical": parse_int(stats.get("block.%d.physical" % index)),
                }
        total_alloc = 0
        total_capacity = 0
        enriched = []
        for disk in disks:
            item = dict(disk)
            source = disk.get("source", "")
            block = by_path.get(source, {})
            capacity = block.get("capacity", 0)
            if disk.get("device") == "disk" and not capacity and source and source != "-":
                capacity = self._disk_capacity(source)
            allocation = block.get("physical") or block.get("allocation", 0)
            if disk.get("device") == "disk":
                total_alloc += allocation
                total_capacity += capacity
            item.update(
                {
                    "allocation": allocation,
                    "capacity": capacity,
                    "allocation_human": human_size(allocation) if allocation else "",
                    "capacity_human": human_size(capacity) if capacity else "",
                    "allocation_percent": pct(allocation, capacity),
                }
            )
            enriched.append(item)
        return {
            "disks": enriched,
            "summary": {
                "percent": pct(total_alloc, total_capacity),
                "allocated_human": human_size(total_alloc) if total_alloc else "",
                "capacity_human": human_size(total_capacity) if total_capacity else "",
                "text": ("%s / %s" % (human_size(total_alloc), human_size(total_capacity))) if total_capacity else "未知",
            },
        }

    def _vnc_display(self, name):
        result = self.virsh("domdisplay", name)
        if result["returncode"] != 0:
            return ""
        return result["stdout"].strip()

    def _vm_summary(self, name, neigh_ips, lease_ips):
        info = self._domain_info(name)
        uuid_text = self._domain_uuid(name)
        state = normalized_state(info)
        display = self._vnc_display(name)
        target = parse_vnc_display(display)
        agent_ips = self._agent_ips(name)
        interfaces = self._domain_interfaces(name)
        ip_addresses = []
        for interface in interfaces:
            mac = (interface.get("mac") or "").lower()
            ips = agent_ips.get(mac) or lease_ips.get(mac) or neigh_ips.get(mac) or []
            interface["ips"] = list(ips)
            for ip in ips:
                if ip not in ip_addresses:
                    ip_addresses.append(ip)
        stats = self._domain_stats(name)
        vcpus = parse_int(info.get("CPU(s)"), 1) or 1
        disk_data = self._disk_metrics(self._domain_disks(name), stats)
        primary = None
        for disk in disk_data["disks"]:
            if disk.get("device") == "disk" and disk.get("source") and disk.get("source") != "-":
                primary = disk
                break
        primary = primary or {}
        memory_unit = str(info.get("Max memory", "")).split()
        max_memory_kib = parse_int(memory_unit[0]) if memory_unit else 0
        return {
            "uuid": uuid_text,
            "name": name,
            "state": state,
            "managed_save": str(info.get("Managed save", "")).strip().lower() == "yes",
            "id": info.get("Id", "-"),
            "cpu": info.get("CPU(s)", ""),
            "max_memory": info.get("Max memory", ""),
            "used_memory": info.get("Used memory", ""),
            "interfaces": interfaces,
            "ip_addresses": ip_addresses,
            "disks": disk_data["disks"],
            "metrics": {
                "cpu": self._cpu_metrics(uuid_text, stats, vcpus),
                "memory": self._memory_metrics(stats, info),
                "storage": disk_data["summary"],
            },
            "config": {
                "vcpus": vcpus,
                "memory_gb": bytes_to_gb(max_memory_kib * 1024),
                "primary_disk_target": primary.get("target", ""),
                "primary_disk_path": primary.get("source", ""),
                "primary_disk_gb": bytes_to_gb(primary.get("capacity", 0)),
                "primary_disk_human": primary.get("capacity_human", ""),
            },
            "display": display,
            "has_console": bool(target) and target["host"] in LOOPBACK_HOSTS and state == "running",
        }

    def vms(self, payload=None):
        neigh_ips = self._neigh_map()
        lease_ips = self._dhcp_leases()
        vms = []
        for name in self._domain_names():
            vms.append(self._vm_summary(name, neigh_ips, lease_ips))
        return {"vms": vms}

    def _disk_dirs_info(self):
        dirs = []
        for disk_dir in self.config.get("disk_dirs") or []:
            item = {"path": disk_dir, "name": disk_dir, "total": 0, "used": 0, "free": 0,
                    "free_human": "", "exists": os.path.isdir(disk_dir)}
            if item["exists"]:
                try:
                    usage = shutil.disk_usage(disk_dir)
                except OSError:
                    usage = None
                if usage is not None:
                    item.update(
                        {
                            "total": usage.total,
                            "used": usage.used,
                            "free": usage.free,
                            "free_human": human_size(usage.free),
                        }
                    )
            dirs.append(item)
        return dirs

    def images(self, payload=None):
        images = []
        for image_dir in self.config.get("image_dirs") or []:
            if not os.path.isdir(image_dir):
                continue
            try:
                entries = sorted(os.listdir(image_dir))
            except OSError:
                continue
            for entry in entries:
                path = os.path.join(image_dir, entry)
                suffix = os.path.splitext(entry)[1].lower()
                if suffix not in IMAGE_SUFFIXES:
                    continue
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    stat_result = os.stat(path)
                except OSError:
                    continue
                kind = "iso" if suffix == ".iso" else "disk"
                item = {
                    "name": entry,
                    "path": path,
                    "type": kind,
                    "size": stat_result.st_size,
                    "size_human": human_size(stat_result.st_size),
                    "mtime": int(stat_result.st_mtime),
                    "virtual_size": "",
                    "format": "",
                    "metadata": guess_image_metadata(path, kind),
                }
                if kind == "disk":
                    try:
                        qinfo = self.run([self._binary("qemu_img"), "info", "--output=json", path], timeout=30)
                    except OpError:
                        qinfo = {"returncode": 1}
                    if qinfo.get("returncode") == 0:
                        try:
                            parsed = json.loads(qinfo.get("stdout") or "{}")
                        except ValueError:
                            parsed = {}
                        item["virtual_size"] = human_size(parsed.get("virtual-size", 0))
                        item["format"] = parsed.get("format", "")
                images.append(item)
        return {
            "images": images,
            "dirs": list(self.config.get("image_dirs") or []),
            "disk_dirs": self._disk_dirs_info(),
        }

    def _networks_data(self):
        bridges = []
        try:
            result = self.run(["ip", "-o", "link", "show", "type", "bridge"], timeout=20)
        except OpError:
            result = {"returncode": 1}
        if result.get("returncode") == 0:
            for line in (result.get("stdout") or "").splitlines():
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    name = parts[1].strip()
                    if name and name not in bridges:
                        bridges.append(name)
        networks = []
        result = self.virsh("net-list", "--all")
        if result["returncode"] == 0:
            for row in parse_table_rows(result["stdout"]):
                parts = row.split()
                if parts:
                    networks.append({"name": parts[0], "state": parts[1] if len(parts) > 1 else ""})
        default_type = str(self.config.get("default_network_type") or "network").lower()
        if default_type not in ("network", "bridge"):
            default_type = "network"
        default_source = (
            self.config.get("default_bridge") if default_type == "bridge" else self.config.get("default_network")
        ) or ""
        return {
            "bridges": bridges,
            "libvirt_networks": networks,
            "default_bridge": self.config.get("default_bridge") or "",
            "default_network": self.config.get("default_network") or "",
            "default_type": default_type,
            "default_source": default_source,
        }

    def networks(self, payload=None):
        return self._networks_data()

    def _loopback_vnc_target(self, name):
        info = self._domain_info(name)
        state = normalized_state(info)
        if state != "running":
            raise OpError("虚拟机未运行，无法连接控制台")
        target = parse_vnc_display(self._vnc_display(name))
        if not target:
            raise OpError("该虚拟机没有可用的 VNC 显示")
        if target["host"] not in LOOPBACK_HOSTS:
            raise OpError("VNC 未监听回环地址，远程控制台已拒绝；请把执行端 display 改为 listen=127.0.0.1")
        if target["host"] == "localhost":
            target["host"] = "127.0.0.1"
        return target

    def vnc_target(self, payload):
        domain = self.resolve_domain(payload.get("uuid"))
        target = self._loopback_vnc_target(domain["name"])
        return {
            "uuid": domain["uuid"],
            "name": domain["name"],
            "host": target["host"],
            "port": target["port"],
            "display": target["display"],
        }

    def vnc_connection(self, uuid_text):
        """给 --vnc 用：返回 (domain, target, socket)，只连接回环地址。"""
        domain = self.resolve_domain(uuid_text)
        target = self._loopback_vnc_target(domain["name"])
        sock = socket.create_connection((target["host"], target["port"]), timeout=10)
        sock.setblocking(True)
        return domain, target, sock

    # -- 写操作 -------------------------------------------------------------
    def create(self, payload):
        """创建虚拟机；与 action/config 共用 mutation 锁，避免交错写同一磁盘目录。"""
        with self.mutation_lock:
            return self._create_locked(payload)

    def _create_locked(self, payload):
        self._require_writes_allowed(None)
        kind = str(payload.get("kind") or "disk").strip().lower()
        if kind not in ("disk", "iso"):
            raise OpError("创建方式无效，只支持 disk 或 iso")
        name = self._validate_new_name(payload.get("name"))
        memory_mb = self._parse_memory_mb(payload)
        vcpus = parse_positive_int(payload, "vcpus", 1, self.config.get("max_vcpus", 256))
        max_disk_gb = self.config.get("max_disk_gb", 20000)
        disk_gb = 0
        if payload.get("disk_gb") not in (None, ""):
            disk_gb = parse_positive_int(payload, "disk_gb", 1, max_disk_gb)
        elif kind == "iso":
            raise OpError("安装 ISO 创建必须指定磁盘容量")
        cloud_init = bool_value(payload.get("cloud_init_enabled"))
        if cloud_init and kind != "disk":
            raise OpError("安装 ISO 创建不支持 cloud-init")
        password = str(payload.get("ci_password") or "")
        if len(password) > 256:
            raise OpError("cloud-init 密码过长")
        ssh_key = str(payload.get("ci_ssh_key") or "").strip()
        if len(ssh_key) > 4096:
            raise OpError("cloud-init SSH 公钥过长")
        username = str(payload.get("ci_username") or "ubuntu").strip() or "ubuntu"
        if cloud_init and not LINUX_USER_RE.match(username):
            raise OpError("cloud-init 用户名只能包含小写字母、数字、下划线和中划线，且不能以数字开头")
        mac = generate_mac() if cloud_init else None
        network_type = str(payload.get("network_type") or self.config.get("default_network_type") or "network").lower()
        network_source = payload.get("network_source") or self._default_network_source(network_type)
        network = self._validate_network(network_type, network_source, mac)
        static_network = self._validate_static_network(payload, kind, cloud_init, network_type)
        disk_base = self._disk_dir_base(payload.get("disk_dir"))
        disk_path = os.path.join(disk_base, "%s.qcow2" % name)
        if os.path.lexists(disk_path):
            raise OpError("目标磁盘文件已存在")
        disk_path = self._resolve_inside(disk_path, self.config.get("disk_dirs") or [], "磁盘路径")
        created = []
        if password:
            self._secrets.add(password)
        try:
            if kind == "disk":
                source = self._template_image(payload.get("source"), label="镜像文件")
                if os.path.splitext(source)[1].lower() == ".iso":
                    raise OpError("disk 方式不能选择 ISO 镜像")
                self.require_ok(
                    self.run(
                        [self._binary("qemu_img"), "convert", "-O", "qcow2", source, disk_path],
                        timeout=self.config.get("install_timeout", 3600),
                    )
                )
                created.append(disk_path)
                if disk_gb:
                    current_size = self._disk_capacity(disk_path)
                    if current_size and disk_gb * GIB < current_size:
                        raise OpError("磁盘容量小于镜像虚拟容量（约 %d GB）" % bytes_to_gb(current_size))
                    if not current_size or disk_gb * GIB > current_size:
                        self.require_ok(
                            self.run([self._binary("qemu_img"), "resize", disk_path, "%dG" % disk_gb], timeout=900)
                        )
            else:
                source = self._template_image(payload.get("source"), required_suffix=".iso", label="ISO 镜像")
                # virt-install 会创建该磁盘；失败清理时按“域未登记 + 仍在 disk_dirs”再判断是否删除。
                created.append(disk_path)
            seed_iso = None
            if cloud_init:
                seed_iso = self._create_seed_iso(name, disk_base, payload, mac, static_network)
                created.append(seed_iso)
            argv = self._install_argv(
                kind=kind,
                name=name,
                memory_mb=memory_mb,
                vcpus=vcpus,
                disk_path=disk_path,
                disk_gb=disk_gb,
                source=source,
                seed_iso=seed_iso,
                network=network,
            )
            self.require_ok(self.run(argv, timeout=self.config.get("install_timeout", 3600)))
            if not self._domain_exists(name):
                raise OpError("virt-install 未登记虚拟机")
            uuid_text = self._domain_uuid(name)
            if not uuid_text:
                raise OpError("无法读取新建虚拟机的 UUID")
        except Exception:
            self._cleanup_created(name, created)
            raise
        finally:
            if password:
                self._secrets.discard(password)
        return {
            "uuid": uuid_text,
            "name": name,
            "kind": kind,
            "summary": "已创建虚拟机 %s" % name,
        }

    def _validate_new_name(self, value):
        name = str(value or "").strip()
        if not VM_NAME_RE.match(name) or name.startswith(".") or name in (".", ".."):
            raise OpError("虚拟机名称只能包含字母、数字、点、下划线和中划线，最长 64 字符，且不能以点开头")
        if self._domain_exists(name):
            raise OpError("同名虚拟机已存在")
        return name

    def _parse_memory_mb(self, payload):
        max_memory_gb = self.config.get("max_memory_gb", 1024)
        if payload.get("memory_gb") not in (None, ""):
            return parse_positive_int(payload, "memory_gb", 1, max_memory_gb) * 1024
        if payload.get("memory_mb") not in (None, ""):
            return parse_positive_int(payload, "memory_mb", 256, max_memory_gb * 1024)
        raise OpError("请填写内存容量")

    def _default_network_source(self, network_type):
        if network_type == "bridge":
            return self.config.get("default_bridge")
        return self.config.get("default_network")

    def _validate_network(self, network_type, source, mac=None):
        data = self._networks_data()
        source = str(source or "").strip()
        mac_part = ",mac=%s" % mac if mac else ""
        if network_type == "bridge":
            if not source or source not in data["bridges"]:
                raise OpError("桥接网络不存在")
            return "bridge=%s,model=virtio%s" % (source, mac_part)
        if network_type == "network":
            names = set(item["name"] for item in data["libvirt_networks"])
            if not source or source not in names:
                raise OpError("libvirt 网络不存在")
            return "network=%s,model=virtio%s" % (source, mac_part)
        raise OpError("网络类型无效")

    def _static_ipv4(self, value):
        """严格解析静态 IPv4/前缀，返回 IPv4Interface。"""
        text = str(value or "").strip()
        if not text or "/" not in text:
            raise OpError("静态 IP 必须为 IPv4/前缀 形式，例如 192.168.0.201/24")
        try:
            iface = ipaddress.IPv4Interface(text)
        except ValueError:
            raise OpError("静态 IP 无效，必须为 IPv4/前缀 形式，例如 192.168.0.201/24")
        network = iface.network
        if not 1 <= network.prefixlen <= 30:
            raise OpError("静态 IP 前缀必须在 1 到 30 之间")
        address = iface.ip
        if address.is_multicast or address.is_unspecified or address.is_loopback:
            raise OpError("静态 IP 必须为可用的单播 IPv4 地址")
        if address == network.network_address:
            raise OpError("静态 IP 不能是子网网络地址")
        if address == network.broadcast_address:
            raise OpError("静态 IP 不能是子网广播地址")
        return iface

    def _static_gateway(self, value, iface):
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            gateway = ipaddress.IPv4Address(text)
        except ValueError:
            raise OpError("网关必须是合法的 IPv4 地址")
        if gateway.is_multicast or gateway.is_unspecified or gateway.is_loopback:
            raise OpError("网关必须为可用的单播 IPv4 地址")
        network = iface.network
        if gateway not in network:
            raise OpError("网关必须与静态 IP 在同一子网")
        if gateway == iface.ip:
            raise OpError("网关不能等于虚拟机静态 IP")
        if gateway == network.network_address:
            raise OpError("网关不能是子网网络地址")
        if gateway == network.broadcast_address:
            raise OpError("网关不能是子网广播地址")
        return str(gateway)

    def _static_dns(self, value):
        if value in (None, ""):
            return []
        if isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            parts = re.split(r"[\s,]+", str(value).strip())
        servers = []
        for part in parts:
            text = str(part or "").strip()
            if not text:
                continue
            try:
                address = ipaddress.IPv4Address(text)
            except ValueError:
                raise OpError("DNS 必须是合法的 IPv4 地址，多个地址可用逗号或空格分隔")
            if address.is_multicast or address.is_unspecified:
                raise OpError("DNS 必须为可用的单播 IPv4 地址")
            text = str(address)
            if text not in servers:
                servers.append(text)
        return servers

    def _validate_static_network(self, payload, kind, cloud_init, network_type):
        """ci_dhcp=false 时校验静态 IPv4；缺省保持 DHCP，旧请求完全兼容。"""
        raw_dhcp = payload.get("ci_dhcp", True)
        if raw_dhcp is None or bool_value(raw_dhcp):
            return None
        if kind != "disk" or not cloud_init:
            raise OpError("静态 IP 仅支持启用 cloud-init 的磁盘镜像创建")
        if network_type != "bridge":
            raise OpError("静态 IP 仅支持桥接网络；NAT/libvirt 网络请使用 DHCP")
        iface = self._static_ipv4(payload.get("ci_ip"))
        gateway = self._static_gateway(payload.get("ci_gateway"), iface)
        dns = self._static_dns(payload.get("ci_dns"))
        return {"address": str(iface), "gateway": gateway, "dns": dns}

    def _validate_cloud_init_iface(self, value):
        iface = str(value or "ens3").strip()
        if not LINUX_IFACE_RE.match(iface):
            raise OpError("cloud-init 网卡名称只能包含字母、数字、点、下划线和中划线，最长 15 字符")
        return iface

    def _create_seed_iso(self, name, disk_base, payload, mac, static_network=None):
        tool = str(self.config.get("genisoimage") or "").strip()
        if not tool:
            tool = shutil.which("genisoimage") or shutil.which("mkisofs") or ""
        if not tool:
            raise OpError("缺少 genisoimage/mkisofs，无法生成 cloud-init seed ISO")
        if not os.path.isabs(tool):
            resolved = shutil.which(tool)
            if not resolved:
                raise OpError("配置的 genisoimage 不存在")
            tool = resolved
        seed_path = os.path.join(disk_base, "%s-seed.iso" % name)
        if os.path.lexists(seed_path):
            raise OpError("cloud-init seed ISO 已存在")
        work_root = os.path.join(disk_base, ".vm-platform-cloudinit")
        work_dir = os.path.join(work_root, "%s-%s" % (name, uuid.uuid4().hex[:8]))
        try:
            os.makedirs(work_root, mode=0o700, exist_ok=True)
            os.chmod(work_root, 0o700)
            os.makedirs(work_dir, mode=0o700, exist_ok=True)
            os.chmod(work_dir, 0o700)
        except OSError as exc:
            raise OpError("cloud-init 临时目录不可用：%s" % (getattr(exc, "strerror", None) or "无法创建"))
        username = str(payload.get("ci_username") or "ubuntu").strip() or "ubuntu"
        password = str(payload.get("ci_password") or "")
        ssh_key = str(payload.get("ci_ssh_key") or "").strip()
        timezone = str(payload.get("ci_timezone") or self.config.get("default_timezone") or "Asia/Shanghai").strip()
        iface = self._validate_cloud_init_iface(payload.get("ci_iface"))
        try:
            files = {
                "user-data": build_cloud_init_user_data(name, username, password, ssh_key, timezone),
                "meta-data": build_cloud_init_meta_data(name),
                "network-config": build_cloud_init_network_config(mac, iface, static_network),
            }
            for filename, content in files.items():
                file_path = os.path.join(work_dir, filename)
                fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
            self.require_ok(
                self.run(
                    [tool, "-quiet", "-output", seed_path, "-volid", "cidata", "-joliet", "-rock",
                     "user-data", "meta-data", "network-config"],
                    timeout=300,
                    cwd=work_dir,
                )
            )
        except Exception:
            # The tool can create a partial ISO before failing or timing out.
            # This path was checked absent above and belongs to this attempt.
            try:
                os.unlink(seed_path)
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not os.path.isfile(seed_path):
            raise OpError("cloud-init seed ISO 生成失败")
        try:
            os.chmod(seed_path, 0o600)
        except OSError:
            pass
        return seed_path

    def _install_argv(self, kind, name, memory_mb, vcpus, disk_path, disk_gb, source, seed_iso, network):
        argv = [
            self._binary("virt_install"),
            "--connect",
            self.config["libvirt_uri"],
            "--name",
            name,
            "--memory",
            str(memory_mb),
            "--vcpus",
            str(vcpus),
        ]
        if kind == "disk":
            argv.extend(["--import", "--disk", "path=%s,format=qcow2,bus=virtio" % disk_path])
        else:
            argv.extend(
                [
                    "--disk",
                    "path=%s,size=%d,format=qcow2,bus=virtio" % (disk_path, disk_gb),
                    "--cdrom",
                    source,
                ]
            )
        if seed_iso:
            argv.extend(["--disk", "path=%s,device=cdrom,bus=ide,readonly=on" % seed_iso])
        argv.extend(
            [
                "--network",
                network,
                "--channel",
                "unix,target_type=virtio,name=org.qemu.guest_agent.0",
                "--graphics",
                "vnc,listen=127.0.0.1",
                "--noautoconsole",
                "--virt-type",
                "kvm",
            ]
        )
        argv.extend(self._os_install_args())
        return argv

    def _os_install_args(self):
        result = self.run([self._binary("virt_install"), "--version"], timeout=60)
        text = ""
        if result.get("returncode") == 0:
            text = "%s %s" % (result.get("stdout") or "", result.get("stderr") or "")
        return os_install_args(text)

    def _cleanup_created(self, name, paths):
        try:
            if self._domain_exists(name):
                return
        except OpError:
            return
        for item in paths:
            if not item:
                continue
            try:
                real = self._mutable_disk_path(item)
            except OpError:
                continue
            try:
                os.unlink(real)
            except OSError:
                continue

    def action(self, payload):
        action = str(payload.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise OpError("不支持的操作")
        domain = self.resolve_domain(payload.get("uuid"))
        self._require_writes_allowed(domain["uuid"])
        name = domain["name"]
        with self.mutation_lock:
            if action == "delete":
                return self._delete(domain, bool_value(payload.get("remove_disks")), bool_value(payload.get("force")))
            if action == "start":
                self.require_ok(self.virsh("start", name, timeout=180))
            elif action == "shutdown":
                self.require_ok(self.virsh("shutdown", name, timeout=180))
            elif action == "stop":
                self.require_ok(self.virsh("destroy", name, timeout=180))
            elif action == "reboot":
                self.require_ok(self.virsh("reboot", name, timeout=180))
            elif action == "suspend":
                self.require_ok(self.virsh("suspend", name, timeout=180))
            elif action == "resume":
                self.require_ok(self.virsh("resume", name, timeout=180))
            state = state_label(normalized_state(self._domain_info(name)))
        return {
            "uuid": domain["uuid"],
            "name": name,
            "action": action,
            "state": state,
            "summary": "已执行%s，当前状态 %s" % (ACTION_LABELS.get(action, action), state),
        }

    def _template_file_paths(self):
        paths = set()
        for image_dir in self.config.get("image_dirs") or []:
            if not os.path.isdir(image_dir):
                continue
            try:
                entries = os.listdir(image_dir)
            except OSError:
                continue
            for entry in entries:
                path = os.path.join(image_dir, entry)
                if os.path.isfile(path):
                    paths.add(os.path.realpath(path))
        return paths

    def _other_vm_disk_paths(self, target_uuid):
        """其它虚拟机引用的磁盘路径；任一台读取失败都拒绝删除（无危险回落）。"""
        paths = set()
        for name in self._domain_names():
            if self._domain_uuid(name) == target_uuid:
                continue
            result = self.virsh("domblklist", name, "--details")
            if result["returncode"] != 0:
                raise OpError("无法确认其它虚拟机的磁盘占用，拒绝删除磁盘文件")
            for disk in parse_block_list(result["stdout"]):
                source = (disk.get("source") or "").strip()
                if source and source != "-":
                    paths.add(os.path.realpath(source))
        return paths

    def _deletion_plan(self, disks, target_uuid, target_name):
        other_refs = self._other_vm_disk_paths(target_uuid)
        template_files = self._template_file_paths()
        seed_name = "%s-seed.iso" % target_name
        candidates = []
        skipped = []
        for disk in disks:
            source = (disk.get("source") or "").strip()
            if not source or source == "-":
                skipped.append("未使用的磁盘设备")
                continue
            if "\x00" in source or not os.path.isabs(source):
                skipped.append("磁盘路径无效")
                continue
            real = os.path.realpath(source)
            if real in other_refs:
                skipped.append("其它虚拟机仍在使用，未删除")
                continue
            if real in template_files:
                base_name = os.path.basename(real)
                if base_name.lower().endswith(".iso") and base_name != seed_name:
                    skipped.append("ISO 模板只读，未删除")
                    continue
                if not base_name.lower().endswith(".iso") and not self._inside_any(
                    real, self.config.get("disk_dirs")
                ):
                    skipped.append("模板文件只读，未删除")
                    continue
            try:
                verified = self._mutable_disk_path(source)
            except OpError as exc:
                skipped.append(str(exc))
                continue
            if verified in candidates:
                continue
            candidates.append(verified)
        return candidates, skipped

    def _delete(self, domain, remove_disks, force):
        name = domain["name"]
        info = self._domain_info(name)
        state = normalized_state(info)
        if state in ("running", "paused", "in shutdown", "pmsuspended"):
            if not force:
                raise OpError("虚拟机处于运行状态，请先关机或使用强制删除")
            self.require_ok(self.virsh("destroy", name, timeout=300))
        disks = self._domain_disks(name) if remove_disks else []
        candidates = []
        skipped = []
        if remove_disks:
            # 预检必须在 undefine 之前完成，任何不确定都直接报错而不是先删后判。
            candidates, skipped = self._deletion_plan(disks, domain["uuid"], name)
        if str(info.get("Managed save", "")).strip().lower() == "yes":
            self.require_ok(self.virsh("managedsave-remove", name, timeout=300))
        self.require_ok(self.virsh("undefine", name, timeout=300))
        removed = 0
        for path in candidates:
            try:
                verified = self._mutable_disk_path(path)
            except OpError as exc:
                skipped.append(str(exc))
                continue
            try:
                os.unlink(verified)
                removed += 1
            except OSError:
                skipped.append("磁盘文件无法删除")
        summary = "已删除虚拟机 %s" % name
        if remove_disks:
            summary += "，移除 %d 个磁盘文件，跳过 %d 个" % (removed, len(skipped))
        reasons = []
        for reason in skipped:
            if reason not in reasons:
                reasons.append(reason)
        return {
            "uuid": domain["uuid"],
            "name": name,
            "action": "delete",
            "state": "deleted",
            "removed_count": removed,
            "skipped_count": len(skipped),
            "skipped_reasons": reasons,
            "summary": summary,
        }

    def configure(self, payload):
        domain = self.resolve_domain(payload.get("uuid"))
        self._require_writes_allowed(domain["uuid"])
        name = domain["name"]
        vcpus = parse_positive_int(payload, "vcpus", 1, self.config.get("max_vcpus", 256))
        memory_gb = parse_positive_int(payload, "memory_gb", 1, self.config.get("max_memory_gb", 1024))
        disk_gb = 0
        if payload.get("disk_gb") not in (None, ""):
            disk_gb = parse_positive_int(payload, "disk_gb", 1, self.config.get("max_disk_gb", 20000))
        if bool_value(payload.get("resize_guest")):
            raise OpError("guest 分区扩容需要虚拟机运行，停机配置不支持")
        with self.mutation_lock:
            info = self._domain_info(name)
            state = normalized_state(info)
            if state != "shut off":
                raise OpError("修改 CPU/内存/磁盘配置需要先关机（当前状态：%s）" % state_label(state))
            current_vcpus = parse_int(info.get("CPU(s)"), 0) or 1
            memory_text = str(info.get("Max memory", "")).split()
            current_memory_gb = bytes_to_gb(parse_int(memory_text[0]) * 1024 if memory_text else 0)
            primary = None
            for disk in self._domain_disks(name):
                if disk.get("device") == "disk" and disk.get("source") and disk.get("source") != "-":
                    primary = disk
                    break
            disk_path = primary.get("source", "") if primary else ""
            current_disk_gb = bytes_to_gb(self._disk_capacity(disk_path)) if disk_path else 0
            if disk_gb and current_disk_gb and disk_gb < current_disk_gb:
                raise OpError("磁盘只允许扩容，当前容量约 %d GB" % current_disk_gb)
            changed = []
            if disk_gb and disk_gb > current_disk_gb:
                if not disk_path:
                    raise OpError("没有找到可扩容的系统盘")
                verified = self._mutable_disk_path(disk_path)
                self.require_ok(
                    self.run([self._binary("qemu_img"), "resize", verified, "%dG" % disk_gb], timeout=900)
                )
                changed.append(
                    "系统盘 %s -> %d GB" % (("%d GB" % current_disk_gb) if current_disk_gb else "未知", disk_gb)
                )
            if vcpus != current_vcpus or memory_gb != current_memory_gb:
                self._update_domain_resources(name, vcpus, memory_gb * 1024)
                if vcpus != current_vcpus:
                    changed.append("CPU %d -> %d" % (current_vcpus, vcpus))
                if memory_gb != current_memory_gb:
                    changed.append("内存 %d -> %d GB" % (current_memory_gb, memory_gb))
            state_after = state_label(normalized_state(self._domain_info(name)))
        return {
            "uuid": domain["uuid"],
            "name": name,
            "vcpus": vcpus,
            "memory_gb": memory_gb,
            "disk_gb": disk_gb or current_disk_gb,
            "changed": changed,
            "state": state_after,
            "summary": "，".join(changed) if changed else "配置未变化",
        }

    def _domain_xml(self, name):
        result = self.require_ok(self.virsh("dumpxml", name, timeout=60))
        try:
            return ET.fromstring(result["stdout"])
        except ET.ParseError:
            raise OpError("无法解析虚拟机 XML 配置")

    def _save_domain_xml(self, name, root):
        fd, xml_path = tempfile.mkstemp(prefix="vm-platform-", suffix=".xml")
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(ET.tostring(root, encoding="unicode"))
                handle.flush()
                os.fsync(handle.fileno())
            self.require_ok(self.virsh("define", xml_path, timeout=120))
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass

    def _update_domain_resources(self, name, vcpus, memory_mb):
        root = self._domain_xml(name)
        vcpu_element = root.find("vcpu")
        if vcpu_element is None:
            raise OpError("虚拟机 XML 缺少 vcpu 节点，已拒绝修改")
        vcpu_element.text = str(vcpus)
        if "current" in vcpu_element.attrib:
            vcpu_element.set("current", str(vcpus))
        memory_kib = str(int(memory_mb) * 1024)
        for tag in ("memory", "currentMemory"):
            element = root.find(tag)
            if element is None:
                raise OpError("虚拟机 XML 缺少 %s 节点，已拒绝修改" % tag)
            element.set("unit", "KiB")
            element.text = memory_kib
        self._save_domain_xml(name, root)


def default_config():
    return build_config()
