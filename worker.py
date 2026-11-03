#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VM Platform 执行端入口。

运行模式（互斥）：
- --serve      在配置的 Unix socket（默认 /run/vm-platform/agent.sock）提供仅 root 可用的服务，
               systemd 常驻；进程内保留 CPU 采样，多次请求之间能算出利用率。
- --rpc        从 stdin 读一行 JSON，向 stdout 写一行 JSON；标准输出没有其它内容。
- --ssh        SSH forced command：只接受 SSH_ORIGINAL_COMMAND 为 "rpc" 或 "vnc <UUID>"。
- --vnc UUID   校验 UUID 与虚拟机 UUID 一致后，连接回环 VNC 并在 stdin/stdout 之间转发原始字节。

配置来自 VM_PLATFORM_AGENT_CONFIG，默认 /etc/vm-platform/agent.json。
只使用 Python 标准库，兼容 Python 3.6.8 与 3.12。
"""

import argparse
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmops  # noqa: E402  (需要在 sys.path 调整之后导入)

DEFAULT_CONFIG_PATH = "/etc/vm-platform/agent.json"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
SOCKET_DIR_MODE = 0o700
SOCKET_FILE_MODE = 0o600
CLIENT_TIMEOUT = 300

OPERATIONS = {
    "status": "status",
    "vms": "vms",
    "images": "images",
    "networks": "networks",
    "create": "create",
    "action": "action",
    "config": "configure",
    "vnc_target": "vnc_target",
}


def log(message):
    """诊断日志只写 stderr（--serve 下进入 systemd journal）。"""
    sys.stderr.write("vm-platform-agent: %s\n" % message)
    sys.stderr.flush()


def encode_response(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _scrub(host, text):
    scrub = getattr(host, "scrub", None)
    if callable(scrub):
        return scrub(text)
    return "" if text is None else str(text)


def dispatch(host, request):
    if not isinstance(request, dict):
        raise vmops.OpError("请求格式无效，必须是 JSON 对象")
    payload = request.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise vmops.OpError("payload 必须是 JSON 对象")
    operation = request.get("operation")
    method_name = OPERATIONS.get(operation) if isinstance(operation, str) else None
    if method_name is None:
        raise vmops.OpError("不支持的操作：%s" % operation)
    method = getattr(host, method_name)
    return method(payload)


def handle_request(host, raw):
    """把一行请求字节转成一行响应文本（不含换行）。"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError:
        return encode_response({"ok": False, "error": "请求不是有效的 UTF-8 文本"})
    try:
        request = json.loads(text)
    except ValueError:
        return encode_response({"ok": False, "error": "请求不是有效的 JSON"})
    operation = request.get("operation") if isinstance(request, dict) else None
    try:
        data = dispatch(host, request)
    except vmops.OpError as exc:
        message = str(exc)
        log("operation %s failed: %s" % (operation, message))
        return encode_response({"ok": False, "error": message})
    except Exception as exc:  # 未预期异常也只回安全信息，不回堆栈
        message = _scrub(host, str(exc) or exc.__class__.__name__)
        log("operation %s crashed: %s" % (operation, message))
        return encode_response({"ok": False, "error": "执行端内部错误：%s" % message})
    log("operation %s ok" % operation)
    return encode_response({"ok": True, "data": data})


class SocketHost(object):
    """CLI transport: all operations execute in the running local agent."""

    def __init__(self, config):
        self.settings = config

    def request(self, operation, payload):
        request = encode_response({"operation": operation, "payload": payload}).encode("utf-8") + b"\n"
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(3720 if operation in ("create", "action", "config") else 100)
            conn.connect(self.settings["socket_path"])
            conn.sendall(request)
            stream = conn.makefile("rb")
            try:
                raw = stream.readline(16 * 1024 * 1024 + 1)
            finally:
                stream.close()
        except OSError:
            raise vmops.OpError("无法连接宿主机执行端：服务未启动或连接中断，请检查执行端服务")
        finally:
            conn.close()
        if len(raw) > 16 * 1024 * 1024 or not raw.endswith(b"\n"):
            raise vmops.OpError("宿主机执行端返回了无效响应")
        try:
            response = json.loads(raw.decode("utf-8"))
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise ValueError("invalid envelope")
            if not response["ok"]:
                raise vmops.OpError(str(response.get("error") or "宿主机操作失败")[:1600])
            return response["data"]
        except (ValueError, KeyError, UnicodeDecodeError):
            raise vmops.OpError("宿主机执行端返回了无效响应")

    def __getattr__(self, name):
        for operation, method_name in OPERATIONS.items():
            if name == method_name:
                return lambda payload: self.request(operation, payload)
        raise AttributeError(name)

    def vnc_connection(self, uuid_text):
        target = self.request("vnc_target", {"uuid": uuid_text})
        if (not isinstance(target, dict) or target.get("uuid") != uuid_text
                or target.get("host") not in vmops.LOOPBACK_HOSTS
                or not isinstance(target.get("port"), int) or not 1 <= target["port"] <= 65535):
            raise vmops.OpError("宿主机返回的控制台身份或地址无效")
        remote = socket.create_connection((target["host"], target["port"]), timeout=10)
        remote.setblocking(True)
        return {"uuid": uuid_text, "name": target.get("name", "")}, target, remote


def run_rpc(host, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        raw = stdin.readline(MAX_REQUEST_BYTES + 1)
    except (OSError, ValueError):
        raw = ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        stdout.write(encode_response({"ok": False, "error": "请求体过大"}) + "\n")
        stdout.flush()
        return 2
    if not raw.strip():
        stdout.write(encode_response({"ok": False, "error": "没有收到请求"}) + "\n")
        stdout.flush()
        return 2
    stdout.write(handle_request(host, raw) + "\n")
    stdout.flush()
    return 0


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("写入失败")
        view = view[written:]


def relay_stream(in_fd, out_fd, remote):
    """在 stdin/stdout 与 VNC socket 之间原样转发字节。"""
    while True:
        try:
            readable, _, _ = select.select([in_fd, remote], [], [])
        except (OSError, ValueError):
            return 0
        if remote in readable:
            try:
                data = remote.recv(65536)
            except OSError:
                return 0
            if not data:
                return 0
            try:
                _write_all(out_fd, data)
            except OSError:
                return 0
        if in_fd in readable:
            try:
                data = os.read(in_fd, 65536)
            except OSError:
                return 0
            if not data:
                return 0
            try:
                remote.sendall(data)
            except OSError:
                return 0


def run_vnc(host, uuid_text):
    text = str(uuid_text or "").strip()
    if not vmops.UUID_RE.match(text):
        log("拒绝的 VNC UUID")
        return 2
    try:
        _domain, _target, remote = host.vnc_connection(text)
    except vmops.OpError as exc:
        log("vnc refused: %s" % str(exc))
        return 3
    except OSError as exc:
        log("vnc connect failed: %s" % (getattr(exc, "strerror", None) or "连接失败"))
        return 3
    try:
        return relay_stream(sys.stdin.fileno(), sys.stdout.fileno(), remote)
    finally:
        try:
            remote.close()
        except OSError:
            pass


def run_ssh(host, config_error=None):
    command = (os.environ.get("SSH_ORIGINAL_COMMAND") or "").strip()
    parts = command.split()
    if parts == ["rpc"]:
        if host is None:
            sys.stdout.write(
                encode_response({"ok": False, "error": "执行端配置不可用：%s" % (config_error or "未知错误")}) + "\n"
            )
            sys.stdout.flush()
            return 4
        return run_rpc(host)
    if len(parts) == 2 and parts[0] == "vnc":
        if not vmops.UUID_RE.match(parts[1]):
            log("拒绝的 VNC UUID")
            return 2
        if host is None:
            log("执行端配置不可用：%s" % (config_error or "未知错误"))
            return 4
        return run_vnc(host, parts[1])
    log("拒绝的 SSH 命令")
    return 2


def _peer_is_root(conn):
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return True
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid == 0
    except OSError:
        return False


def _send(conn, text):
    try:
        conn.sendall((text + "\n").encode("utf-8"))
    except OSError:
        pass


def _serve_client(host, conn):
    try:
        conn.settimeout(CLIENT_TIMEOUT)
        if not _peer_is_root(conn):
            log("拒绝非 root 连接")
            _send(conn, encode_response({"ok": False, "error": "仅允许 root 访问执行端 socket"}))
            return
        stream = conn.makefile("rb")
        try:
            raw = stream.readline(MAX_REQUEST_BYTES + 1)
        finally:
            stream.close()
        if len(raw) > MAX_REQUEST_BYTES:
            _send(conn, encode_response({"ok": False, "error": "请求体过大"}))
            return
        if not raw.strip():
            _send(conn, encode_response({"ok": False, "error": "没有收到请求"}))
            return
        _send(conn, handle_request(host, raw))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def prepare_socket(path):
    directory = os.path.dirname(path)
    if not directory:
        raise vmops.OpError("socket 路径无效")
    if not os.path.isdir(directory):
        os.makedirs(directory, SOCKET_DIR_MODE)
    os.chmod(directory, SOCKET_DIR_MODE)
    if os.path.lexists(path):
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode):
            raise vmops.OpError("socket 路径已被非 socket 文件占用：%s" % path)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(2)
            probe.connect(path)
        except OSError:
            os.unlink(path)
        else:
            raise vmops.OpError("已有执行端实例在监听 %s" % path)
        finally:
            probe.close()


def serve(host):
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        log("--serve 需要 root 身份")
        return 4
    path = host.config.get("socket_path") or vmops.DEFAULT_CONFIG["socket_path"]
    try:
        prepare_socket(path)
    except (vmops.OpError, OSError) as exc:
        log("socket 准备失败：%s" % str(exc))
        return 4
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        os.chmod(path, SOCKET_FILE_MODE)
        server.listen(64)
    except OSError as exc:
        log("监听失败：%s" % (getattr(exc, "strerror", None) or "未知错误"))
        try:
            server.close()
        except OSError:
            pass
        return 4
    log("listening on %s" % path)
    stopping = threading.Event()

    def _stop(_signum, _frame):
        stopping.set()
        try:
            server.close()
        except OSError:
            pass

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass
    try:
        while not stopping.is_set():
            try:
                conn, _address = server.accept()
            except OSError:
                break
            thread = threading.Thread(target=_serve_client, args=(host, conn))
            thread.daemon = True
            thread.start()
    finally:
        try:
            server.close()
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0


def build_host(config_path, socket_override=None):
    config = vmops.load_config(config_path)
    if socket_override:
        config["socket_path"] = os.path.abspath(socket_override)
    if not os.path.isfile(config_path or ""):
        log("配置文件不存在，使用保守默认值（只读、无镜像/磁盘目录）：%s" % (config_path or ""))
    return vmops.Host(config, log=log)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="worker.py", description="VM Platform 执行端")
    parser.add_argument("--serve", action="store_true", help="作为 systemd 服务监听 Unix socket")
    parser.add_argument("--rpc", action="store_true", help="stdin 读一行 JSON，stdout 写一行 JSON")
    parser.add_argument("--ssh", action="store_true", help="SSH forced command 入口")
    parser.add_argument("--vnc", metavar="UUID", help="把 stdin/stdout 转发到该虚拟机的回环 VNC")
    parser.add_argument("--config", help="覆盖 VM_PLATFORM_AGENT_CONFIG")
    parser.add_argument("--socket", help="覆盖配置中的 socket 路径")
    args = parser.parse_args(argv)
    modes = [args.serve, args.rpc, args.ssh, bool(args.vnc)]
    if sum(1 for item in modes if item) != 1:
        parser.error("必须且只能选择一个运行模式")

    config_path = args.config or os.environ.get("VM_PLATFORM_AGENT_CONFIG") or DEFAULT_CONFIG_PATH
    host = None
    config_error = None
    try:
        host = build_host(config_path, args.socket)
    except Exception as exc:  # 配置错误按模式选择输出通道
        config_error = _scrub(None, str(exc) or "配置错误")

    if args.serve:
        if host is None:
            log("执行端配置不可用：%s" % config_error)
            return 4
        return serve(host)
    if host is not None:
        host = SocketHost(host.config)
    if args.rpc:
        if host is None:
            sys.stdout.write(encode_response({"ok": False, "error": "执行端配置不可用：%s" % config_error}) + "\n")
            sys.stdout.flush()
            return 4
        return run_rpc(host)
    if args.ssh:
        return run_ssh(host, config_error)
    if host is None:
        log("执行端配置不可用：%s" % config_error)
        return 4
    return run_vnc(host, args.vnc)


if __name__ == "__main__":
    sys.exit(main())
