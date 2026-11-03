import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller

TEST_UUID = "11111111-2222-3333-4444-555555555555"


class PlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "Example-test-only-password"
        cls.password_hash = controller.hash_password(cls.password)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"db_path": os.path.join(self.tmp.name, "platform.db"), "password_hash": self.password_hash, "ssh_key": "/test/key", "known_hosts": "/test/known_hosts", "hosts": [dict(id="node30", name="主机30", address="192.168.0.30", transport="local", ssh_user="root", ssh_port=22), dict(id="node150", name="主机150", address="192.168.0.150", transport="ssh", ssh_user="root", ssh_port=22)]}
        self.app = controller.Platform(self.config)

    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def test_password_hash_and_rejection(self):
        self.assertTrue(controller.check_password(self.password, self.password_hash))
        self.assertFalse(controller.check_password("wrong", self.password_hash))
        self.assertNotIn(self.password, self.password_hash)
        with self.assertRaises(controller.APIError):
            self.app.login("admin", "wrong", "127.0.0.1")

    def test_sessions_persist_then_logout_invalidates(self):
        token, csrf = self.app.login("admin", self.password, "127.0.0.1")
        self.assertEqual(csrf, self.app.session(token))
        reopened = controller.Platform(self.config)
        self.assertEqual(csrf, reopened.session(token))
        reopened.pool.shutdown()
        with self.app.db() as db:
            stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertNotEqual(token, stored)
        self.app.logout(token)
        with self.assertRaises(controller.APIError):
            self.app.session(token)

    def test_host_edit_remove_does_not_touch_worker_or_vms(self):
        data = dict(id="extra", name="新增节点", address="192.168.0.151", transport="ssh", ssh_user="root", ssh_port=22)
        with mock.patch.object(self.app, "agent_call") as call:
            self.app.save_host(data)
            data["name"] = "编辑后的节点"
            self.app.save_host(data, "extra")
            self.assertEqual("编辑后的节点", self.app.host("extra")["name"])
            self.app.delete_host("extra")
            call.assert_not_called()
        self.assertEqual(2, len(self.app.hosts()))

    def test_unknown_host_never_falls_back(self):
        with mock.patch.object(self.app, "agent_call") as call:
            with self.assertRaises(controller.APIError):
                self.app.task("unknown", "action", {"uuid": TEST_UUID, "action": "start"})
            call.assert_not_called()

    def test_host_arguments_reject_shell_and_local_override(self):
        for address in ("-oProxyCommand=echo", "host;touch /tmp/x", "../localhost", "a\nb"):
            with self.assertRaises(controller.APIError):
                controller.validate_host(dict(id="bad", name="bad", address=address))
        data = dict(self.app.host("node150"), transport="local")
        with self.assertRaises(controller.APIError):
            controller.validate_host(data, self.app.host("node150"))
        data = dict(self.app.host("node30"), address="192.168.0.150")
        with self.assertRaises(controller.APIError):
            controller.validate_host(data, self.app.host("node30"))

    def test_vnc_uses_selected_host_and_uuid(self):
        command = self.app.worker_command(self.app.host("node150"), "vnc", TEST_UUID)
        self.assertIn("root@192.168.0.150", command)
        self.assertEqual("vnc " + TEST_UUID, command[-1])
        self.assertIn("StrictHostKeyChecking=yes", command)
        with self.assertRaises(controller.APIError):
            self.app.worker_command(self.app.host("node150"), "vnc", "ubuntu;id")

    def test_task_result_does_not_store_password_or_commands(self):
        pending = []
        with mock.patch.object(self.app.pool, "submit", side_effect=lambda *args: pending.append(args)):
            task = self.app.task("node150", "create", {"name": "test", "kind": "disk", "ci_password": "secret-cloud-init"})
        with mock.patch.object(self.app, "agent_call", return_value={"summary": "完成", "uuid": TEST_UUID, "name": "test", "cmd": ["secret-cloud-init"]}) as call:
            self.app.run_task(*pending[0][1:])
            self.assertEqual("node150", call.call_args[0][0]["id"])
        stored = self.app.tasks()[0]
        self.assertEqual("success", stored["status"])
        self.assertNotIn("secret-cloud-init", json.dumps(stored))
        self.assertEqual(TEST_UUID, stored["vm_uuid"])

    def test_failed_remote_task_has_error_without_local_fallback(self):
        pending = []
        with mock.patch.object(self.app.pool, "submit", side_effect=lambda *args: pending.append(args)):
            self.app.task("node150", "action", {"uuid": TEST_UUID, "action": "start"})
        with mock.patch.object(self.app, "agent_call", side_effect=controller.APIError(502, "执行端不可达")) as call:
            self.app.run_task(*pending[0][1:])
            self.assertEqual(1, call.call_count)
            self.assertEqual("node150", call.call_args[0][0]["id"])
        task = self.app.tasks()[0]
        self.assertEqual("failed", task["status"])
        self.assertIn("不可达", task["error"])

    def test_running_task_prevents_retarget_or_unregister(self):
        with mock.patch.object(self.app.pool, "submit"):
            self.app.task("node150", "action", {"uuid": TEST_UUID, "action": "start"})
        with self.assertRaises(controller.APIError):
            self.app.delete_host("node150")
        with self.assertRaises(controller.APIError):
            self.app.save_host(self.app.host("node150"), "node150")

    def test_restart_marks_uncertain_task_failed(self):
        with mock.patch.object(self.app.pool, "submit"):
            self.app.task("node150", "action", {"uuid": TEST_UUID, "action": "start"})
        reopened = controller.Platform(self.config)
        self.assertEqual("failed", reopened.tasks()[0]["status"])
        self.assertIn("实际状态", reopened.tasks()[0]["error"])
        reopened.pool.shutdown()


class HTTPTests(PlatformTests):
    def setUp(self):
        super().setUp()
        self.server = controller.Server(("127.0.0.1", 0), controller.Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, method, path, payload=None, csrf=True, origin=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, json.dumps(payload or {}) if method != "GET" else None, headers)
        response = conn.getresponse()
        status, cookie = response.status, response.getheader("Set-Cookie")
        data = json.loads(response.read())
        conn.close()
        return status, data, cookie

    def login_http(self, password=None):
        status, body, cookie = self.request("POST", "/api/login", {"username": "admin", "password": password or self.password})
        self.assertEqual(200, status)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.cookie, self.csrf = cookie.split(";", 1)[0], body["csrf_token"]

    def test_http_auth_and_csrf(self):
        self.assertEqual(401, self.request("GET", "/api/hosts")[0])
        self.assertEqual(401, self.request("POST", "/api/hosts/node150/vms", {"kind": "disk"})[0])
        self.login_http()
        self.assertEqual(200, self.request("GET", "/api/hosts")[0])
        self.assertEqual(403, self.request("DELETE", "/api/hosts/node150", csrf=False)[0])
        self.assertEqual(403, self.request("POST", "/api/logout", {}, origin="http://attacker.example")[0])
        self.assertEqual(200, self.request("POST", "/api/logout")[0])
        self.assertEqual(401, self.request("GET", "/api/hosts")[0])

    def test_password_change_rejects_wrong_current_and_weak_new_password(self):
        self.login_http()

        status, body, _ = self.request(
            "POST", "/api/password", {"current_password": "wrong-password", "password": "Valid-new-password-12"}
        )
        self.assertEqual(403, status)
        self.assertIn("当前密码", body["error"])

        status, body, _ = self.request(
            "POST", "/api/password", {"current_password": self.password, "password": "too-short"}
        )
        self.assertEqual(400, status)
        self.assertIn("12", body["error"])

        # 失败不强制注销：当前会话仍可用
        self.assertEqual(200, self.request("GET", "/api/hosts")[0])

    def test_password_change_invalidates_sessions_and_persists_hash(self):
        self.login_http()
        new_password = "Another-valid-password-24"

        status, body, _ = self.request(
            "POST", "/api/password", {"current_password": self.password, "password": new_password}
        )
        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, body)

        # 旧会话已由 API 全部失效
        self.assertEqual(401, self.request("GET", "/api/hosts")[0])

        # 旧密码不能再登录，新密码可以
        self.cookie, self.csrf = "", ""
        self.assertEqual(
            401, self.request("POST", "/api/login", {"username": "admin", "password": self.password})[0]
        )
        self.login_http(new_password)
        self.assertEqual(200, self.request("GET", "/api/hosts")[0])

        # 新哈希已写入数据库
        with self.app.db() as db:
            stored = db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()[0]
        self.assertTrue(controller.check_password(new_password, stored))
        self.assertFalse(controller.check_password(self.password, stored))

    def test_selected_host_failure_does_not_break_other_host(self):
        self.login_http()
        def agent(host, operation, payload=None):
            if host["id"] == "node150":
                raise controller.APIError(502, "执行端离线")
            return {"hostname": "node30", "vm_count": 14}
        with mock.patch.object(self.app, "agent_call", side_effect=agent):
            self.assertEqual(502, self.request("GET", "/api/hosts/node150/status")[0])
            status, body, _ = self.request("GET", "/api/hosts/node30/status")
            self.assertEqual(200, status)
            self.assertEqual(14, body["status"]["vm_count"])
            self.assertEqual(404, self.request("GET", "/api/hosts/missing/status")[0])

    def test_console_requires_same_origin_and_websocket_upgrade(self):
        route = "/api/hosts/node150/vms/" + TEST_UUID + "/vnc"
        self.assertEqual(401, self.request("GET", route)[0])
        self.login_http()
        with mock.patch.object(self.app, "agent_call", return_value={"name": "acceptance"}) as agent:
            self.assertEqual(403, self.request("GET", route)[0])
            self.assertEqual(403, self.request("GET", route, origin="http://attacker.example")[0])
            agent.assert_not_called()
            origin = "http://{}:{}".format(*self.server.server_address)
            self.assertEqual(400, self.request("GET", route, origin=origin)[0])

    def test_static_path_cannot_expose_source_or_secrets(self):
        self.assertEqual(404, self.request("GET", "/static/../controller.py")[0])


if __name__ == "__main__":
    unittest.main()
