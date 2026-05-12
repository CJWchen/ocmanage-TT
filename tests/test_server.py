import io
import unittest

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


if __name__ == "__main__":
    unittest.main()
