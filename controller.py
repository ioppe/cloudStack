#!/usr/bin/env python3
"""云栈 controller. Python 3.6+, standard library only."""
import base64
from contextlib import contextmanager
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

VERSION = "1.1.0"
BASE = Path(__file__).resolve().parent
HOST_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
VM_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
COOKIE = "vm_platform_session"
MAX_BODY = 131072


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return "pbkdf2_sha256$260000${}${}".format(salt, digest)


def check_password(password, encoded):
    try:
        kind, rounds, salt, digest = encoded.split("$")
        if kind != "pbkdf2_sha256" or not 100000 <= int(rounds) <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, AttributeError, TypeError):
        return False


def validate_host(data, existing=None, allow_local=False):
    if not isinstance(data, dict):
        raise APIError(400, "主机信息必须是对象")
    hid = str(data.get("id", existing["id"] if existing else ""))
    if not HOST_ID.fullmatch(hid):
        raise APIError(400, "主机标识须以小写字母开头，使用字母、数字、下划线或连字符")
    if existing and hid != existing["id"]:
        raise APIError(400, "主机标识不能修改")
    name = str(data.get("name", "")).strip()
    address = str(data.get("address", "")).strip()
    transport = str(data.get("transport", "ssh"))
    user = str(data.get("ssh_user", "root"))
    try:
        port = int(data.get("ssh_port", 22))
    except (ValueError, TypeError):
        raise APIError(400, "SSH 端口无效")
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise APIError(400, "请输入有效的主机名称")
    try:
        ipaddress.ip_address(address)
        valid_address = True
    except ValueError:
        valid_address = bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", address)) and ".." not in address
    if not valid_address or not USER_RE.fullmatch(user) or not 1 <= port <= 65535:
        raise APIError(400, "主机地址、SSH 用户或端口无效")
    if transport not in ("local", "ssh"):
        raise APIError(400, "连接方式无效")
    if transport == "local" and not (allow_local or (existing and existing["transport"] == "local")):
        raise APIError(400, "本地连接只能在服务初始化配置中登记")
    host = dict(id=hid, name=name, address=address, transport=transport, ssh_user=user, ssh_port=port)
    if existing and existing["transport"] == "local":
        if any(host[k] != existing[k] for k in ("address", "transport", "ssh_user", "ssh_port")):
            raise APIError(400, "本地主机只允许编辑显示名称")
    return host


class Platform:
    def __init__(self, config):
        self.config = config
        self.db_path = config["db_path"]
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.login_attempts = {}
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hosts (id TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, host_id TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            """)
            if not db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
                for data in config.get("hosts", []):
                    host = validate_host(data, allow_local=True)
                    db.execute("INSERT INTO hosts VALUES (?,?)", (host["id"], json.dumps(host)))
                if not config.get("password_hash"):
                    raise RuntimeError("必须配置管理员 password_hash")
                db.execute("INSERT INTO settings VALUES ('password_hash',?)", (config["password_hash"],))
                db.execute("INSERT INTO settings VALUES ('initialized','yes')")
            for row in db.execute("SELECT id,data FROM tasks WHERE status='running'").fetchall():
                task = json.loads(row["data"])
                task.update(status="failed", error="平台服务曾中断，请核对宿主机实际状态后再操作", finished_at=int(time.time()))
                db.execute("UPDATE tasks SET status='failed',data=? WHERE id=?", (json.dumps(task), row["id"]))
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def hosts(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT config FROM hosts ORDER BY id")]

    def host(self, hid):
        if not HOST_ID.fullmatch(hid):
            raise APIError(404, "主机不存在")
        with self.db() as db:
            row = db.execute("SELECT config FROM hosts WHERE id=?", (hid,)).fetchone()
        if row is None:
            raise APIError(404, "主机不存在")
        return json.loads(row[0])

    def busy(self, hid):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM tasks WHERE host_id=? AND status='running'", (hid,)).fetchone())

    def save_host(self, data, hid=None):
        with self.guard:
            existing = self.host(hid) if hid else None
            if existing and self.busy(hid):
                raise APIError(409, "该主机仍有执行中的任务，请稍后修改")
            host = validate_host(data, existing)
            with self.db() as db:
                if not hid and db.execute("SELECT 1 FROM hosts WHERE id=?", (host["id"],)).fetchone():
                    raise APIError(409, "主机标识已存在")
                db.execute("INSERT OR REPLACE INTO hosts VALUES (?,?)", (host["id"], json.dumps(host)))
            return host

    def delete_host(self, hid):
        with self.guard:
            self.host(hid)
            if self.busy(hid):
                raise APIError(409, "该主机仍有执行中的任务，请稍后解除纳管")
            with self.db() as db:
                db.execute("DELETE FROM hosts WHERE id=?", (hid,))

    def password_hash(self):
        with self.db() as db:
            return db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()[0]

    def login(self, username, password, ip):
        now = time.time()
        with self.guard:
            attempts = [t for t in self.login_attempts.get(ip, []) if now - t < 60]
            self.login_attempts[ip] = attempts
            if len(attempts) >= 8:
                raise APIError(429, "登录尝试过多，请一分钟后重试")
        if username != self.config.get("username", "admin") or not check_password(password, self.password_hash()):
            with self.guard:
                self.login_attempts[ip].append(now)
            raise APIError(401, "用户名或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), csrf, now + 28800))
        return token, csrf

    def session(self, token):
        if not token:
            raise APIError(401, "请先登录")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            row = db.execute("SELECT csrf,expires FROM sessions WHERE token_hash=?", (digest,)).fetchone()
        if row is None or row["expires"] <= time.time():
            raise APIError(401, "登录已过期，请重新登录")
        return row["csrf"]

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def ssh_command(self, host, remote_command):
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ConnectionAttempts=1",
                "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR",
                "-o", "UserKnownHostsFile=" + self.config["known_hosts"], "-i", self.config["ssh_key"],
                "-p", str(host["ssh_port"]), "--", host["ssh_user"] + "@" + host["address"], remote_command]

    def worker_command(self, host, mode="rpc", vm_uuid=None):
        if mode not in ("rpc", "vnc") or (mode == "vnc" and not VM_UUID.fullmatch(vm_uuid or "")):
            raise APIError(400, "执行请求无效")
        if host["transport"] == "local":
            command = [self.config.get("worker_python", sys.executable), str(BASE / "worker.py"), "--" + mode]
            return command + ([vm_uuid] if mode == "vnc" else [])
        return self.ssh_command(host, "rpc" if mode == "rpc" else "vnc " + vm_uuid)

    def agent_call(self, host, operation, payload=None):
        request = json.dumps({"operation": operation, "payload": payload or {}}, ensure_ascii=False).encode() + b"\n"
        timeout = 3720 if operation in ("create", "config", "action") else 100
        command = self.worker_command(host)
        env = os.environ.copy()
        env["VM_PLATFORM_AGENT_CONFIG"] = self.config.get("agent_config", "/etc/vm-platform/agent.json")
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            out, err = proc.communicate(request, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise APIError(502, "宿主机响应超时，请核对实际状态：" + host["name"])
        if proc.returncode != 0:
            # Do not return command lines, keys or remote environment in user-facing errors.
            raise APIError(502, "无法连接宿主机执行端：{}（请检查 SSH 信任、端口和执行端服务）".format(host["name"]))
        try:
            response = json.loads(out.decode())
            if not response.get("ok"):
                raise APIError(502, str(response.get("error", "宿主机操作失败"))[:1600])
            return response["data"]
        except (ValueError, KeyError, UnicodeDecodeError, AttributeError):
            raise APIError(502, "宿主机返回了无效响应：" + host["name"])

    def task(self, hid, operation, payload):
        with self.guard:
            host = self.host(hid)
            target = str(payload.get("uuid") or payload.get("name", ""))
            if operation != "create" and not VM_UUID.fullmatch(target):
                raise APIError(400, "虚拟机 UUID 无效")
            if operation == "create" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", target):
                raise APIError(400, "虚拟机名称无效")
            with self.db() as db:
                if db.execute("SELECT 1 FROM tasks WHERE host_id=? AND status='running'", (hid,)).fetchone():
                    raise APIError(409, "该主机有执行中的管理任务，请等待完成")
                labels = {"create": "创建虚拟机", "config": "修改虚拟机配置", "start": "启动", "shutdown": "正常关机", "stop": "强制停止", "reboot": "重启", "suspend": "暂停", "resume": "恢复", "delete": "删除虚拟机"}
                task = dict(id=uuid.uuid4().hex, host_id=hid, host_name=host["name"], vm_uuid=payload.get("uuid", ""), vm_name=payload.get("name", ""), title=labels.get(payload.get("action"), labels.get(operation, operation)), status="running", created_at=int(time.time()), finished_at=None, error="", result=None)
                db.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", (task["id"], hid, target, "running", json.dumps(task, ensure_ascii=False)))
            # Copy the registration now: editing or switching the UI never retargets a job.
            self.pool.submit(self.run_task, task, host, operation, dict(payload))
            return task

    def run_task(self, task, host, operation, payload):
        try:
            result = self.agent_call(host, operation, payload)
            safe = {k: result[k] for k in ("summary", "name", "uuid", "removed", "skipped", "removed_count", "skipped_count") if k in result} if isinstance(result, dict) else {"summary": "操作已完成"}
            task.update(status="success", result=safe, vm_uuid=safe.get("uuid", task["vm_uuid"]), vm_name=safe.get("name", task["vm_name"]))
        except Exception as exc:
            task.update(status="failed", error=str(exc)[:1600])
        task["finished_at"] = int(time.time())
        with self.db() as db:
            db.execute("UPDATE tasks SET status=?,data=? WHERE id=?", (task["status"], json.dumps(task, ensure_ascii=False), task["id"]))

    def tasks(self, hid=None):
        with self.db() as db:
            if hid:
                self.host(hid)
                rows = db.execute("SELECT data FROM tasks WHERE host_id=? ORDER BY rowid DESC LIMIT 100", (hid,))
            else:
                rows = db.execute("SELECT data FROM tasks ORDER BY rowid DESC LIMIT 100")
            return [json.loads(row[0]) for row in rows]


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VMPlatform/" + VERSION

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Access logs contain routes/status only, never request bodies, cookies or console tokens.
        sys.stderr.write("{} {}\n".format(self.address_string(), fmt % args))

    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, data, status=200, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[COOKIE].value if COOKIE in cookie else ""
        except Exception:
            return ""

    def require_auth(self, write=False):
        csrf = self.app.session(self.token())
        if write and not hmac.compare_digest(csrf, self.headers.get("X-CSRF-Token", "")):
            raise APIError(403, "请求校验失败，请刷新页面后重试")
        return csrf

    def origin_check(self, required=False):
        origin = self.headers.get("Origin", "")
        if required and not origin:
            raise APIError(403, "控制台连接缺少来源校验")
        if origin:
            parts = urlparse(origin)
            if parts.scheme not in ("http", "https") or parts.netloc != self.headers.get("Host", ""):
                raise APIError(403, "请求来源无效")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "请求长度无效")
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise APIError(413, "请求内容过大")
        try:
            payload = json.loads(self.rfile.read(length).decode()) if length else {}
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "请求必须是有效 JSON")
        if not isinstance(payload, dict):
            raise APIError(400, "请求必须是 JSON 对象")
        return payload

    def send_static(self, relative):
        root = (BASE / "static").resolve()
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise APIError(404, "文件不存在")
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
        body = path.read_bytes()
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if method == "GET" and path == "/health":
            return self.send_json({"ok": True, "version": VERSION})
        if method == "GET" and path in ("/", "/index.html"):
            return self.send_static("index.html")
        if method == "GET" and path.startswith("/static/"):
            return self.send_static(path[len("/static/"):])
        if method == "GET" and path == "/console":
            self.require_auth()
            return self.send_static("console.html")
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise APIError(405, "不支持该方法")
        payload = self.read_json() if method in ("POST", "PUT", "DELETE") else {}
        if method == "POST":
            self.origin_check()
        if method == "POST" and path == "/api/login":
            token, csrf = self.app.login(str(payload.get("username", "")), str(payload.get("password", "")), self.client_address[0])
            return self.send_json({"username": self.app.config.get("username", "admin"), "csrf_token": csrf}, cookie=COOKIE + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800")
        csrf = self.require_auth(write=method != "GET")
        if method == "GET" and path == "/api/session":
            return self.send_json({"username": self.app.config.get("username", "admin"), "csrf_token": csrf})
        if method == "POST" and path == "/api/logout":
            self.app.logout(self.token())
            return self.send_json({"ok": True}, cookie=COOKIE + "=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        if method == "POST" and path == "/api/password":
            if not check_password(str(payload.get("current_password", "")), self.app.password_hash()):
                raise APIError(403, "当前密码错误")
            password = str(payload.get("password", ""))
            if not 12 <= len(password) <= 200:
                raise APIError(400, "新密码长度须为 12 到 200 字符")
            with self.app.db() as db:
                db.execute("UPDATE settings SET value=? WHERE key='password_hash'", (hash_password(password),))
                db.execute("DELETE FROM sessions")
            return self.send_json({"ok": True})
        if path == "/api/hosts":
            if method == "GET":
                return self.send_json({"hosts": self.app.hosts()})
            if method == "POST":
                return self.send_json({"host": self.app.save_host(payload)}, 201)
        if method == "GET" and path == "/api/tasks":
            return self.send_json({"tasks": self.app.tasks(parse_qs(parsed.query).get("host_id", [None])[0])})
        match = re.fullmatch(r"/api/hosts/([^/]+)(?:/(.*))?", path)
        if not match:
            raise APIError(404, "接口不存在")
        hid, rest = match.groups()
        host = self.app.host(hid)
        if rest is None:
            if method == "PUT":
                return self.send_json({"host": self.app.save_host(payload, hid)})
            if method == "DELETE":
                self.app.delete_host(hid)
                return self.send_json({"ok": True})
        if method == "GET" and rest in ("status", "vms", "images", "networks"):
            data = self.app.agent_call(host, rest)
            if rest == "status":
                data = {"host": host, "online": True, "status": data}
            elif rest == "vms":
                vms = data["vms"] if isinstance(data, dict) else data
                for vm in vms:
                    vm["host_id"] = hid
                data = {"vms": vms}
            return self.send_json(data)
        if method == "POST" and rest == "vms":
            if payload.get("kind") not in ("disk", "iso"):
                raise APIError(400, "创建类型须为 ISO 或磁盘镜像")
            return self.send_json({"task": self.app.task(hid, "create", payload)}, 202)
        vm_match = re.fullmatch(r"vms/([0-9a-f-]{36})/(action|config|vnc)", rest or "")
        if vm_match:
            vm_uuid, operation = vm_match.groups()
            if not VM_UUID.fullmatch(vm_uuid):
                raise APIError(400, "虚拟机 UUID 无效")
            if method == "GET" and operation == "vnc":
                self.origin_check(required=True)
                return self.console(host, vm_uuid)
            if method == "POST" and operation in ("action", "config"):
                if operation == "action" and payload.get("action") not in ("start", "shutdown", "stop", "reboot", "suspend", "resume", "delete"):
                    raise APIError(400, "虚拟机操作无效")
                payload["uuid"] = vm_uuid
                return self.send_json({"task": self.app.task(hid, operation, payload)}, 202)
        raise APIError(404, "接口不存在")

    def console(self, host, vm_uuid):
        self.app.agent_call(host, "vnc_target", {"uuid": vm_uuid})
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            valid = len(base64.b64decode(key, validate=True)) == 16
        except Exception:
            valid = False
        if not valid or self.headers.get("Upgrade", "").lower() != "websocket" or self.headers.get("Sec-WebSocket-Version") != "13":
            raise APIError(400, "需要有效的 WebSocket 连接")
        env = os.environ.copy()
        env["VM_PLATFORM_AGENT_CONFIG"] = self.app.config.get("agent_config", "/etc/vm-platform/agent.json")
        proc = subprocess.Popen(self.app.worker_command(host, "vnc", vm_uuid), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0)
        upgraded = False
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 12)
            banner = os.read(proc.stdout.fileno(), 65536) if ready else b""
            if not banner.startswith(b"RFB "):
                raise APIError(502, "无法连接该虚拟机的 VNC 控制台")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            accept = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(accept).decode())
            if "binary" in self.headers.get("Sec-WebSocket-Protocol", "").split(","):
                self.send_header("Sec-WebSocket-Protocol", "binary")
            self.end_headers()
            upgraded = True
            self.connection.settimeout(15)
            ws_send(self.connection, banner)
            while True:
                ready, _, _ = select.select([self.connection, proc.stdout], [], [], 60)
                if proc.stdout in ready:
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    ws_send(self.connection, data)
                if self.connection in ready:
                    opcode, data = ws_recv(self.connection)
                    if opcode == 8:
                        ws_send(self.connection, b"", 8)
                        break
                    if opcode == 9:
                        ws_send(self.connection, data, 10)
                    elif opcode in (0, 1, 2):
                        proc.stdin.write(data)
                        proc.stdin.flush()
        except (OSError, EOFError, ValueError):
            if not upgraded:
                raise APIError(502, "控制台通道不可用")
        finally:
            self.close_connection = True
            proc.stdin.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()

    def handle_method(self, method):
        try:
            self.dispatch(method)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json({"error": "平台处理请求失败，请检查服务日志和宿主机连接"}, 500)

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")

    def do_PUT(self):
        self.handle_method("PUT")

    def do_DELETE(self):
        self.handle_method("DELETE")


def read_exact(sock, size):
    chunks = []
    while size:
        data = sock.recv(size)
        if not data:
            raise EOFError()
        chunks.append(data)
        size -= len(data)
    return b"".join(chunks)


def ws_recv(sock):
    a, b = read_exact(sock, 2)
    if a & 0x70 or not b & 0x80:
        raise ValueError("invalid websocket frame")
    length = b & 127
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]
    if length > 8388608:
        raise ValueError("websocket frame too large")
    mask = read_exact(sock, 4)
    body = read_exact(sock, length)
    return a & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(body))


def ws_send(sock, data, opcode=2):
    length = len(data)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    sock.sendall(header + data)


def main():
    os.umask(0o077)
    config_path = os.environ.get("VM_PLATFORM_CONFIG", "/etc/vm-platform/controller.json")
    with open(config_path, encoding="utf-8") as stream:
        config = json.load(stream)
    app = Platform(config)
    server = Server((config.get("bind", "0.0.0.0"), int(config.get("port", 8089))), Handler)
    server.app = app
    print("VM Platform {} listening on {}:{}".format(VERSION, *server.server_address), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
