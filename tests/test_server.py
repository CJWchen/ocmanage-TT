import io
import unittest
from unittest.mock import patch

from manager_tt_backend.server import ExecHandler


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


if __name__ == "__main__":
    unittest.main()
