import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.actions import apply_tencent_model_module
from manager_tt_backend.model_modules import (
    TENCENT_PRIMARY_MODELS,
    apply_tencent_model_package,
)


def _base_config() -> dict:
    return {
        "gateway": {
            "port": 19789,
            "bind": "127.0.0.1",
        },
        "models": {
            "mode": "replace",
            "providers": {
                "qwen": {
                    "baseUrl": "https://example.com/v1",
                    "apiKey": "qwen-secret",
                    "api": "openai-completions",
                    "models": [{"id": "qwen-max", "name": "Qwen Max", "input": ["text"]}],
                }
            },
        },
        "agents": {
            "defaults": {
                "workspace": "/tmp/workspace-designer",
                "model": {"primary": "qwen/qwen-max"},
                "models": {
                    "qwen/qwen-max": {"alias": "Qwen"},
                    "tencent-coding-plan/legacy": {"alias": "legacy"},
                },
                "timeoutSeconds": 300,
            }
        },
        "plugins": {
            "allow": ["feishu", "qwen"],
            "entries": {
                "feishu": {"enabled": True},
                "qwen": {"enabled": True},
                "openai": {"enabled": False, "config": {"region": "cn"}},
            },
        },
        "tools": {"profile": "designer"},
        "channels": {"feishu": {"enabled": True}},
        "hooks": {"enabled": True},
        "meta": {"lastTouchedVersion": "2026.5.1"},
        "wizard": {"done": True},
    }


class TencentModelModuleMergeTests(unittest.TestCase):
    def test_merge_preserves_non_tencent_settings_and_instance_private_fields(self) -> None:
        source = _base_config()

        merged = apply_tencent_model_package(source, "sk-test-1234", "tencent-coding-plan/glm-5")

        self.assertEqual(source["models"]["mode"], "replace")
        self.assertEqual(merged["gateway"], source["gateway"])
        self.assertEqual(merged["tools"], source["tools"])
        self.assertEqual(merged["channels"], source["channels"])
        self.assertEqual(merged["hooks"], source["hooks"])
        self.assertEqual(merged["meta"], source["meta"])
        self.assertEqual(merged["wizard"], source["wizard"])
        self.assertEqual(merged["agents"]["defaults"]["workspace"], "/tmp/workspace-designer")
        self.assertEqual(merged["agents"]["defaults"]["timeoutSeconds"], 300)
        self.assertEqual(merged["models"]["mode"], "merge")
        self.assertIn("qwen", merged["models"]["providers"])
        self.assertEqual(merged["models"]["providers"]["qwen"], source["models"]["providers"]["qwen"])
        self.assertEqual(merged["agents"]["defaults"]["model"]["primary"], "tencent-coding-plan/glm-5")
        self.assertEqual(merged["agents"]["defaults"]["models"]["qwen/qwen-max"], {"alias": "Qwen"})
        self.assertNotIn("tencent-coding-plan/legacy", merged["agents"]["defaults"]["models"])
        for model_name in TENCENT_PRIMARY_MODELS:
            self.assertIn(model_name, merged["agents"]["defaults"]["models"])
            self.assertEqual(merged["agents"]["defaults"]["models"][model_name], {})

    def test_plugins_openai_is_enabled_and_allowlist_only_appends_when_present(self) -> None:
        merged = apply_tencent_model_package(_base_config(), "sk-test-1234", TENCENT_PRIMARY_MODELS[0])
        self.assertEqual(merged["plugins"]["allow"], ["feishu", "qwen", "openai"])
        self.assertTrue(merged["plugins"]["entries"]["openai"]["enabled"])
        self.assertEqual(merged["plugins"]["entries"]["openai"]["config"], {"region": "cn"})

        no_allow = _base_config()
        no_allow["plugins"].pop("allow")
        merged_no_allow = apply_tencent_model_package(no_allow, "sk-test-1234", TENCENT_PRIMARY_MODELS[0])
        self.assertNotIn("allow", merged_no_allow["plugins"])


class TencentModelModuleApplyTests(unittest.TestCase):
    def _write_config(self, root: Path) -> Path:
        path = root / "openclaw.json"
        path.write_text(json.dumps(_base_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_dry_run_does_not_write_config(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            original_text = config_path.read_text(encoding="utf-8")
            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.probe_tencent_model_package") as probe,
                patch("manager_tt_backend.actions.run_shell") as run_shell,
            ):
                result = apply_tencent_model_module(
                    {
                        "profile": "designer",
                        "apiKey": "sk-test-1234",
                        "primaryModel": "tencent-coding-plan/glm-5",
                        "dryRun": True,
                        "probeAfterApply": False,
                        "restartAfterSave": False,
                    }
                )

            self.assertEqual(result["status"], "preview")
            self.assertFalse(result["writePerformed"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
            self.assertEqual(list(config_path.parent.glob("openclaw.json.bak.*.manager-tt")), [])
            probe.assert_not_called()
            run_shell.assert_not_called()

    def test_invalid_primary_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "primaryModel"):
            apply_tencent_model_module(
                {
                    "profile": "designer",
                    "apiKey": "sk-test-1234",
                    "primaryModel": "tencent-coding-plan/not-real",
                    "dryRun": True,
                }
            )

    def test_validate_failure_rolls_back_persisted_config(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            original_text = config_path.read_text(encoding="utf-8")

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 1, "stdout": '{"ok":false}', "stderr": "validate boom"})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.actions.probe_tencent_model_package") as probe,
            ):
                result = apply_tencent_model_module(
                    {
                        "profile": "designer",
                        "apiKey": "sk-test-1234",
                        "primaryModel": "tencent-coding-plan/glm-5",
                        "dryRun": False,
                        "probeAfterApply": True,
                        "restartAfterSave": False,
                    }
                )

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["writePerformed"])
            self.assertTrue(result["rollbackPerformed"])
            self.assertEqual(result["validate"]["returncode"], 1)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_text)
            probe.assert_not_called()

    def test_probe_success_returns_applied_status(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch(
                    "manager_tt_backend.actions.probe_tencent_model_package",
                    return_value={"ok": True, "status": "ok", "message": "probe 成功"},
                ),
            ):
                result = apply_tencent_model_module(
                    {
                        "profile": "designer",
                        "apiKey": "sk-test-1234",
                        "primaryModel": "tencent-coding-plan/glm-5",
                        "dryRun": False,
                        "probeAfterApply": True,
                        "restartAfterSave": False,
                    }
                )

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "applied")
            self.assertEqual(persisted["agents"]["defaults"]["model"]["primary"], "tencent-coding-plan/glm-5")

    def test_probe_failure_returns_applied_with_probe_failure_status(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch(
                    "manager_tt_backend.actions.probe_tencent_model_package",
                    return_value={"ok": False, "status": "http_error", "message": "401 unauthorized"},
                ),
            ):
                result = apply_tencent_model_module(
                    {
                        "profile": "designer",
                        "apiKey": "sk-test-1234",
                        "primaryModel": "tencent-coding-plan/glm-5",
                        "dryRun": False,
                        "probeAfterApply": True,
                        "restartAfterSave": False,
                    }
                )

            self.assertEqual(result["status"], "applied_with_probe_failure")
            self.assertEqual(result["probe"]["status"], "http_error")

    def test_string_false_flags_do_not_flip_to_true(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                return type("Result", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

            with (
                patch("manager_tt_backend.actions.config_path_for_profile", return_value=config_path),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.actions.probe_tencent_model_package") as probe,
                patch("manager_tt_backend.actions.run_systemctl_action") as restart,
            ):
                result = apply_tencent_model_module(
                    {
                        "profile": "designer",
                        "apiKey": "sk-test-1234",
                        "primaryModel": "tencent-coding-plan/glm-5",
                        "dryRun": "false",
                        "probeAfterApply": "false",
                        "restartAfterSave": "false",
                    }
                )

            self.assertEqual(result["status"], "applied")
            self.assertFalse(result["dryRun"])
            self.assertFalse(result["probeAfterApply"])
            self.assertFalse(result["restartAfterSave"])
            self.assertTrue(result["writePerformed"])
            self.assertNotIn("probe", result)
            probe.assert_not_called()
            restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
