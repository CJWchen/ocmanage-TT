import json
import struct
import termios
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.actions import apply_feishu_channel_module, save_config
from manager_tt_backend.config import OPENCLAW_BIN
from manager_tt_backend.feishu_modules import apply_feishu_channel_package, summarize_feishu_channel
from manager_tt_backend.feishu_qr_sessions import (
    build_feishu_qr_command,
    build_feishu_qr_env,
    normalize_feishu_qr_input,
    sanitize_terminal_output,
    set_pty_window_size,
)


def _base_config() -> dict:
    return {
        "gateway": {
            "port": 19789,
            "bind": "127.0.0.1",
        },
        "channels": {
            "feishu": {
                "enabled": False,
                "groupPolicy": "allowlist",
                "requireMention": True,
            },
            "slack": {"enabled": True},
        },
        "plugins": {
            "entries": {
                "feishu": {"enabled": False},
            }
        },
        "meta": {"lastTouchedVersion": "2026.5.1"},
    }


class FeishuModuleMergeTests(unittest.TestCase):
    def test_merge_preserves_existing_channel_settings_for_top_level_single_account(self) -> None:
        source = _base_config()

        merged, target = apply_feishu_channel_package(source, "cli_test", "secret-1234")

        self.assertEqual(target, {"mode": "top-level", "accountId": None})
        self.assertFalse(source["channels"]["feishu"]["enabled"])
        self.assertTrue(merged["channels"]["feishu"]["enabled"])
        self.assertEqual(merged["channels"]["feishu"]["appId"], "cli_test")
        self.assertEqual(merged["channels"]["feishu"]["appSecret"], "secret-1234")
        self.assertEqual(merged["channels"]["feishu"]["domain"], "feishu")
        self.assertEqual(merged["channels"]["feishu"]["connectionMode"], "websocket")
        self.assertEqual(merged["channels"]["feishu"]["groupPolicy"], "allowlist")
        self.assertEqual(merged["channels"]["slack"], source["channels"]["slack"])

        summary = summarize_feishu_channel(merged)
        self.assertEqual(summary["accountMode"], "top-level")
        self.assertEqual(summary["appId"], "cli_test")
        self.assertTrue(summary["hasAppSecret"])

    def test_merge_updates_default_account_when_multi_account_config_exists(self) -> None:
        source = _base_config()
        source["channels"]["feishu"] = {
            "enabled": True,
            "defaultAccount": "main",
            "groupPolicy": "open",
            "accounts": {
                "main": {
                    "appId": "cli_old",
                    "appSecret": "old-secret",
                    "name": "Primary",
                },
                "backup": {
                    "appId": "cli_backup",
                    "appSecret": "backup-secret",
                    "enabled": False,
                },
            },
        }

        merged, target = apply_feishu_channel_package(source, "cli_new", "new-secret")

        self.assertEqual(target, {"mode": "account", "accountId": "main"})
        self.assertEqual(merged["channels"]["feishu"]["defaultAccount"], "main")
        self.assertEqual(merged["channels"]["feishu"]["accounts"]["main"]["appId"], "cli_new")
        self.assertEqual(merged["channels"]["feishu"]["accounts"]["main"]["appSecret"], "new-secret")
        self.assertEqual(merged["channels"]["feishu"]["accounts"]["backup"], source["channels"]["feishu"]["accounts"]["backup"])
        self.assertNotIn("appId", merged["channels"]["feishu"])

        summary = summarize_feishu_channel(merged)
        self.assertEqual(summary["accountMode"], "account")
        self.assertEqual(summary["accountId"], "main")
        self.assertEqual(summary["accountCount"], 2)
        self.assertEqual(summary["appId"], "cli_new")


class FeishuModuleApplyTests(unittest.TestCase):
    @staticmethod
    def _pending_login_runtime() -> dict:
        return {
            "status": "pending_login",
            "ready": False,
            "feishuRuntime": {
                "status": "pending_login",
                "ready": False,
                "plugin": {"loaded": True, "status": "loaded"},
                "channel": {"configured": False, "running": False, "probeOk": None},
                "issues": [],
            },
        }

    def _write_config(self, root: Path) -> Path:
        path = root / "openclaw.json"
        path.write_text(json.dumps(_base_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_minimal_config(self, root: Path) -> Path:
        path = root / "openclaw.json"
        path.write_text(
            json.dumps(
                {
                    "gateway": {
                        "port": 19789,
                        "bind": "127.0.0.1",
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_dry_run_does_not_write_and_does_not_touch_qr_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            original_text = config_path.read_text(encoding="utf-8")
            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.ensure_feishu_qr_session_unlocked") as ensure_unlocked,
                patch("manager_tt_backend.actions.run_shell") as run_shell,
            ):
                result = apply_feishu_channel_module(
                    {
                        "profile": "designer",
                        "appId": "cli_test",
                        "appSecret": "secret-1234",
                        "dryRun": True,
                        "restartAfterSave": False,
                    }
                )

            self.assertEqual(result["status"], "preview")
            self.assertFalse(result["writePerformed"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
            ensure_unlocked.assert_not_called()
            run_shell.assert_not_called()

    def test_apply_creates_feishu_channel_for_minimal_new_instance_config(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_minimal_config(Path(tmp))

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.actions.ensure_feishu_runtime", return_value=self._pending_login_runtime()) as runtime_sync,
            ):
                result = apply_feishu_channel_module(
                    {
                        "profile": "designer",
                        "appId": "cli_test",
                        "appSecret": "secret-1234",
                        "dryRun": False,
                        "restartAfterSave": False,
                    }
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "applied")
            self.assertTrue(result["writePerformed"])
            self.assertFalse(result["rollbackPerformed"])
            self.assertEqual(result["target"], {"mode": "top-level", "accountId": None})
            self.assertEqual(saved["channels"]["feishu"]["appId"], "cli_test")
            self.assertEqual(saved["channels"]["feishu"]["appSecret"], "secret-1234")
            self.assertTrue(saved["channels"]["feishu"]["enabled"])
            self.assertEqual(saved["channels"]["feishu"]["domain"], "feishu")
            self.assertEqual(saved["channels"]["feishu"]["connectionMode"], "websocket")
            runtime_sync.assert_called_once_with("designer", restart_gateway=False)

    def test_validate_failure_rolls_back_persisted_config(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            original_text = config_path.read_text(encoding="utf-8")

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 1, "stdout": '{"ok":false}', "stderr": "validate boom"})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
            ):
                result = apply_feishu_channel_module(
                    {
                        "profile": "designer",
                        "appId": "cli_test",
                        "appSecret": "secret-1234",
                        "dryRun": False,
                        "restartAfterSave": False,
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["writePerformed"])
            self.assertTrue(result["rollbackPerformed"])
            self.assertEqual(result["validate"]["returncode"], 1)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)

    def test_save_config_is_blocked_when_qr_session_is_active(self) -> None:
        with patch("manager_tt_backend.actions.ensure_feishu_qr_session_unlocked", side_effect=RuntimeError("qr locked")):
            with self.assertRaisesRegex(RuntimeError, "qr locked"):
                save_config({"profile": "designer", "config": _base_config(), "restartAfterSave": False})

    def test_save_config_validate_failure_rolls_back_and_skips_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            original_text = config_path.read_text(encoding="utf-8")

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 1, "stdout": '{"ok":false}', "stderr": "validate boom"})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.actions.run_systemctl_action") as restart,
                patch(
                    "manager_tt_backend.actions.gather_feishu_runtime_status",
                    return_value={"status": "pending_login", "ready": False},
                ),
            ):
                result = save_config(
                    {
                        "profile": "designer",
                        "config": _base_config(),
                        "restartAfterSave": True,
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["writePerformed"])
            self.assertTrue(result["rollbackPerformed"])
            self.assertEqual(result["validate"]["returncode"], 1)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
            restart.assert_not_called()

    def test_save_config_string_false_does_not_trigger_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.actions.run_systemctl_action") as restart,
                patch(
                    "manager_tt_backend.actions.gather_feishu_runtime_status",
                    return_value={"status": "pending_login", "ready": False},
                ),
            ):
                result = save_config(
                    {
                        "profile": "designer",
                        "config": _base_config(),
                        "restartAfterSave": "false",
                    }
                )

            self.assertEqual(result["status"], "saved")
            self.assertFalse(result["restartAfterSave"])
            restart.assert_not_called()


class FeishuQrSessionUtilityTests(unittest.TestCase):
    def test_build_command_uses_named_profile_only_when_needed(self) -> None:
        self.assertEqual(
            build_feishu_qr_command("default"),
            [str(OPENCLAW_BIN), "channels", "login", "--channel", "feishu", "--verbose"],
        )
        self.assertEqual(
            build_feishu_qr_command("designer"),
            [str(OPENCLAW_BIN), "--profile", "designer", "channels", "login", "--channel", "feishu", "--verbose"],
        )

    def test_build_command_uses_runtime_container_for_docker_profiles(self) -> None:
        runtime = {
            "profile": "designer",
            "runtimeMode": "docker",
            "runtimeMeta": {"containerName": "openclaw-gateway-designer"},
            "dockerRuntime": {"containerName": "openclaw-gateway-designer"},
        }

        self.assertEqual(
            build_feishu_qr_command("designer", runtime=runtime),
            [
                str(OPENCLAW_BIN),
                "--container",
                "openclaw-gateway-designer",
                "channels",
                "login",
                "--channel",
                "feishu",
                "--verbose",
            ],
        )

    def test_terminal_output_sanitizer_removes_ansi_sequences(self) -> None:
        cleaned = sanitize_terminal_output("\x1b[2K\rhello\x1b[?25l\nworld\x1b[0m")

        self.assertEqual(cleaned, "\nhello\nworld")

    def test_build_qr_env_sets_sane_terminal_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            env = build_feishu_qr_env(home=Path("/tmp/feishu-home"), columns=96, rows=28)

        self.assertEqual(env["HOME"], "/tmp/feishu-home")
        self.assertEqual(env["COLUMNS"], "96")
        self.assertEqual(env["LINES"], "28")
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_set_pty_window_size_writes_requested_dimensions(self) -> None:
        with patch("manager_tt_backend.feishu_qr_sessions.fcntl.ioctl") as ioctl:
            set_pty_window_size(42, columns=96, rows=28)

        ioctl.assert_called_once_with(
            42,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 28, 96, 0, 0),
        )

    def test_normalize_qr_input_converts_lf_to_carriage_return(self) -> None:
        self.assertEqual(normalize_feishu_qr_input("\n"), "\r")
        self.assertEqual(normalize_feishu_qr_input("hello\n"), "hello\r")
        self.assertEqual(normalize_feishu_qr_input("hello\r\nworld\n"), "hello\rworld\r")


if __name__ == "__main__":
    unittest.main()
