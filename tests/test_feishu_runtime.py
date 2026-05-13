import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.config import OPENCLAW_BIN
from manager_tt_backend.feishu_runtime import (
    build_runtime_openclaw_command,
    extract_json_document,
    gather_feishu_runtime_status,
    inspect_feishu_plugin,
    summarize_feishu_runtime_health,
)


class RuntimeCommandTests(unittest.TestCase):
    def test_named_host_profile_uses_profile_flag(self) -> None:
        runtime = {
            "profile": "designer",
            "runtimeMode": "systemd",
            "runtimeMeta": {},
            "dockerRuntime": None,
        }

        self.assertEqual(
            build_runtime_openclaw_command("designer", ["plugins", "inspect", "feishu", "--json"], runtime),
            [str(OPENCLAW_BIN), "--profile", "designer", "plugins", "inspect", "feishu", "--json"],
        )

    def test_docker_profile_uses_container_flag(self) -> None:
        runtime = {
            "profile": "designer",
            "runtimeMode": "docker",
            "runtimeMeta": {"containerName": "openclaw-gateway-designer"},
            "dockerRuntime": {"containerName": "openclaw-gateway-designer"},
        }

        self.assertEqual(
            build_runtime_openclaw_command("designer", ["channels", "status", "--json"], runtime),
            [str(OPENCLAW_BIN), "--container", "openclaw-gateway-designer", "channels", "status", "--json"],
        )

    def test_partial_runtime_context_backfills_profile(self) -> None:
        runtime = {
            "runtimeMode": "systemd",
            "serviceName": "openclaw-gateway-designer.service",
        }

        self.assertEqual(
            build_runtime_openclaw_command("designer", ["channels", "status", "--json"], runtime),
            [str(OPENCLAW_BIN), "--profile", "designer", "channels", "status", "--json"],
        )


class JsonExtractionTests(unittest.TestCase):
    def test_extract_json_document_skips_warning_prefix(self) -> None:
        payload = extract_json_document('Gateway not reachable: missing scope: operator.read\n{"ok":true,"count":1}')

        self.assertEqual(payload, {"ok": True, "count": 1})


class RuntimeHealthTests(unittest.TestCase):
    def test_docker_plugin_path_accepts_same_path_mount_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            compose_path = Path(tmp) / "docker-compose.yml"
            compose_path.write_text(
                "\n".join(
                    [
                        "services:",
                        "  openclaw-gateway:",
                        "    volumes:",
                        "      - /home/yun/.openclaw-designer:/home/node/.openclaw",
                        "      - /home/yun/.openclaw-designer:/home/yun/.openclaw-designer",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = {
                "profile": "designer",
                "runtimeMode": "docker",
                "runtimeMeta": {
                    "composePath": str(compose_path),
                    "containerName": "openclaw-gateway-designer",
                },
                "dockerRuntime": {"containerName": "openclaw-gateway-designer"},
            }

            with patch(
                "manager_tt_backend.feishu_runtime.run_runtime_openclaw_command",
                return_value={
                    "returncode": 0,
                    "stdout": (
                        '{"plugin":{"status":"loaded","enabled":true,"activated":true,'
                        '"channelIds":["feishu"],"version":"2026.5.6",'
                        '"source":"/home/yun/.openclaw-designer/npm/node_modules/@openclaw/feishu/dist/index.js"},'
                        '"install":{"installPath":"/home/yun/.openclaw-designer/npm/node_modules/@openclaw/feishu"}}'
                    ),
                    "stderr": "",
                },
            ):
                plugin = inspect_feishu_plugin("designer", runtime=runtime)

        self.assertTrue(plugin["pathAligned"])
        self.assertTrue(plugin["loaded"])
        self.assertIsNone(plugin["error"])

    def test_gather_runtime_status_accepts_partial_runtime_context(self) -> None:
        with (
            patch(
                "manager_tt_backend.feishu_runtime.read_feishu_config_summary",
                return_value={"enabled": True, "hasAppId": True, "hasAppSecret": True},
            ),
            patch("manager_tt_backend.feishu_runtime.inspect_feishu_plugin", return_value={"loaded": True, "error": None}),
            patch(
                "manager_tt_backend.feishu_runtime.inspect_feishu_channel",
                return_value={"configured": True, "running": True, "ready": True, "error": None, "lastError": None},
            ),
            patch(
                "manager_tt_backend.feishu_runtime.inspect_feishu_logs",
                return_value={"started": True, "ready": True, "errors": [], "recentLines": []},
            ),
        ):
            status = gather_feishu_runtime_status(
                "designer",
                runtime={"runtimeMode": "systemd", "serviceName": "openclaw-gateway-designer.service"},
            )

        self.assertEqual(status["profile"], "designer")
        self.assertEqual(status["serviceName"], "openclaw-gateway-designer.service")
        self.assertEqual(status["status"], "ready")

    def test_plugin_missing_is_reported(self) -> None:
        health = summarize_feishu_runtime_health(
            {"enabled": True, "hasAppId": True, "hasAppSecret": True},
            {"loaded": False, "error": "Plugin not found: feishu"},
            {"configured": False, "running": False, "ready": False, "error": None},
            {"started": False, "ready": False, "errors": [], "recentLines": []},
            {"runtimeMode": "docker", "dockerRuntime": {"running": True}},
        )

        self.assertEqual(health["status"], "plugin_missing")
        self.assertFalse(health["ready"])
        self.assertIn("Plugin not found: feishu", health["issues"])

    def test_pending_login_when_plugin_loaded_but_channel_not_configured(self) -> None:
        health = summarize_feishu_runtime_health(
            {"enabled": True, "hasAppId": True, "hasAppSecret": True},
            {"loaded": True, "error": None},
            {"configured": False, "running": False, "ready": False, "error": None},
            {"started": False, "ready": False, "errors": [], "recentLines": []},
            {"runtimeMode": "docker", "dockerRuntime": {"running": True}},
        )

        self.assertEqual(health["status"], "pending_login")
        self.assertFalse(health["ready"])

    def test_probe_ready_marks_runtime_ready(self) -> None:
        health = summarize_feishu_runtime_health(
            {"enabled": True, "hasAppId": True, "hasAppSecret": True},
            {"loaded": True, "error": None},
            {"configured": True, "running": True, "ready": True, "error": None},
            {"started": True, "ready": True, "errors": [], "recentLines": []},
            {"runtimeMode": "docker", "dockerRuntime": {"running": True}},
        )

        self.assertEqual(health["status"], "ready")
        self.assertTrue(health["ready"])
        self.assertEqual(health["readyEvidence"], "probe")

    def test_logs_can_backfill_ready_when_probe_is_permission_blocked(self) -> None:
        health = summarize_feishu_runtime_health(
            {"enabled": True, "hasAppId": True, "hasAppSecret": True},
            {"loaded": True, "error": None},
            {"configured": True, "running": True, "ready": False, "error": "missing scope: operator.read"},
            {"started": True, "ready": True, "errors": [], "recentLines": ["[ws] ws client ready"]},
            {"runtimeMode": "systemd", "dockerRuntime": None},
        )

        self.assertEqual(health["status"], "ready")
        self.assertTrue(health["ready"])
        self.assertEqual(health["readyEvidence"], "logs")


if __name__ == "__main__":
    unittest.main()
