"""Exercise the real private socket boundary without libvirt or VM writes."""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vmops
import worker


class AgentTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "agent.sock")
        self.client = worker.SocketHost({"socket_path": self.path})

    def responder(self, response):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(1)
        seen = []

        def respond():
            try:
                conn, _ = server.accept()
                with conn:
                    stream = conn.makefile("rb")
                    seen.append(json.loads(stream.readline().decode("utf-8")))
                    stream.close()
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            finally:
                server.close()

        thread = threading.Thread(target=respond)
        thread.daemon = True
        thread.start()
        self.addCleanup(thread.join, 2)
        return seen

    def test_rpc_requires_running_agent(self):
        output = io.StringIO()
        worker.run_rpc(self.client, io.StringIO('{"operation":"status"}\n'), output)
        response = json.loads(output.getvalue())
        self.assertFalse(response["ok"])
        self.assertIn("执行端", response["error"])
        self.assertIn("服务未启动", response["error"])

    def test_config_request_reaches_daemon_with_uuid(self):
        seen = self.responder({"ok": True, "data": {"summary": "configured"}})
        payload = {"uuid": "321c1a77-80d6-4a14-a715-520d5d7839aa", "vcpus": 3}
        result = worker.dispatch(self.client, {"operation": "config", "payload": payload})
        self.assertEqual(result["summary"], "configured")
        self.assertEqual(seen, [{"operation": "config", "payload": payload}])

    def test_daemon_failure_is_preserved(self):
        self.responder({"ok": False, "error": "目标 UUID 不存在"})
        with self.assertRaisesRegex(vmops.OpError, "目标 UUID 不存在"):
            self.client.status({})

    def test_vnc_rejects_mismatched_identity_before_connect(self):
        self.responder({"ok": True, "data": {"uuid": "other", "host": "127.0.0.1", "port": 5900}})
        with mock.patch.object(socket, "create_connection") as connect:
            with self.assertRaisesRegex(vmops.OpError, "身份"):
                self.client.vnc_connection("321c1a77-80d6-4a14-a715-520d5d7839aa")
            connect.assert_not_called()

    def test_cli_wraps_operations_in_socket_transport(self):
        host = vmops.Host(vmops.build_config({"socket_path": self.path}))
        with mock.patch.object(worker, "build_host", return_value=host), mock.patch.object(worker, "run_rpc", return_value=0) as rpc:
            self.assertEqual(worker.main(["--rpc"]), 0)
            self.assertIsInstance(rpc.call_args[0][0], worker.SocketHost)


if __name__ == "__main__":
    unittest.main()
