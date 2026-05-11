import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.actions import create_instance, delete_instance
from manager_tt_backend.docker_managed import create_instance_via_docker_manager


class CreateInstanceActionTests(unittest.TestCase):
    @patch("manager_tt_backend.actions.create_instance_via_docker_manager")
    def test_create_defaults_to_docker_when_mode_omitted(self, docker_create) -> None:
        docker_create.return_value = {"returncode": 0}

        create_instance({"profile": "designer"})

        docker_create.assert_called_once_with({"profile": "designer"})

    @patch("manager_tt_backend.actions.create_instance_via_host_manager")
    @patch("manager_tt_backend.actions.list_instances")
    def test_host_create_is_rejected_when_host_instance_exists(self, list_instances, host_create) -> None:
        list_instances.return_value = [{"profile": "default", "runtimeMode": "systemd"}]

        with self.assertRaisesRegex(ValueError, "本机实例"):
            create_instance({"profile": "designer", "runtimeMode": "host"})

        host_create.assert_not_called()

    @patch("manager_tt_backend.actions.create_instance_via_host_manager")
    @patch("manager_tt_backend.actions.list_instances")
    def test_host_create_still_works_when_only_docker_instances_exist(self, list_instances, host_create) -> None:
        list_instances.return_value = [{"profile": "designer", "runtimeMode": "docker"}]
        host_create.return_value = {"returncode": 0}

        create_instance({"profile": "ops", "runtimeMode": "host"})

        host_create.assert_called_once_with({"profile": "ops", "runtimeMode": "host"})


class DockerCreateRollbackTests(unittest.TestCase):
    def test_create_rolls_back_written_artifacts_when_service_start_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            openclaw_home = home / ".openclaw"
            openclaw_home.mkdir()
            workspace_dir = openclaw_home / "workspace-designer"
            systemd_dir = home / ".config" / "systemd" / "user"
            systemd_dir.mkdir(parents=True)
            service_name = "openclaw-gateway-designer.service"
            service_path = systemd_dir / service_name
            token_path = home / ".config" / "manager-tt" / "openclaw-bridge" / "designer.token"
            config_path = home / ".openclaw-designer" / "openclaw.json"

            def fake_prepare(profile, port, workspace):
                config_path.parent.mkdir(parents=True, exist_ok=True)
                workspace.mkdir(parents=True, exist_ok=True)
                config_path.write_text('{"gateway":{"port":19789}}\n', encoding="utf-8")
                return {"returncode": 0, "stdout": "", "stderr": ""}

            def fake_install(profile, port):
                service_path.write_text("[Unit]\nDescription=test\n", encoding="utf-8")
                return {"returncode": 0, "stdout": "", "stderr": ""}

            def fake_ensure_bridge_token(profile):
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text("token\n", encoding="utf-8")
                return token_path

            shell_calls = []

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                shell_calls.append(cmd)
                if cmd == "systemctl --user daemon-reload":
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                if cmd == f"systemctl --user enable {service_name}":
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                if cmd == f"systemctl --user start {service_name}":
                    return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
                if cmd.startswith("systemctl --user disable --now"):
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with (
                patch("manager_tt_backend.config.HOME", home),
                patch("manager_tt_backend.config.OPENCLAW_HOME", openclaw_home),
                patch("manager_tt_backend.config.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.config.OPENCLAW_BRIDGE_TOKEN_DIR", token_path.parent),
                patch("manager_tt_backend.docker_managed.HOME", home),
                patch("manager_tt_backend.docker_managed.OPENCLAW_HOME", openclaw_home),
                patch("manager_tt_backend.docker_managed.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.docker_managed.resolve_openclaw_image", return_value="ghcr.io/openclaw/openclaw:2026.5.6"),
                patch("manager_tt_backend.docker_managed.resolve_create_port", return_value=19789),
                patch("manager_tt_backend.docker_managed.prepare_profile_config", side_effect=fake_prepare),
                patch("manager_tt_backend.docker_managed.install_base_service", side_effect=fake_install),
                patch("manager_tt_backend.docker_managed.ensure_bridge_token", side_effect=fake_ensure_bridge_token),
                patch("manager_tt_backend.docker_managed.run_shell", side_effect=fake_run_shell),
            ):
                with self.assertRaisesRegex(RuntimeError, "启动 openclaw-gateway-designer.service 失败|boom"):
                    create_instance_via_docker_manager({"profile": "designer"})

            self.assertFalse(config_path.exists())
            self.assertFalse((home / ".openclaw-designer").exists())
            self.assertFalse(workspace_dir.exists())
            self.assertFalse((home / ".openclaw-designer-docker").exists())
            self.assertFalse((home / ".local" / "bin" / "openclaw-designer-docker-service").exists())
            self.assertFalse((systemd_dir / f"{service_name}.d" / "override.conf").exists())
            self.assertFalse(service_path.exists())
            self.assertFalse(token_path.exists())
            self.assertIn(f"systemctl --user start {service_name}", shell_calls)
            self.assertTrue(any(cmd.startswith("systemctl --user disable --now") for cmd in shell_calls))


class DeleteInstanceTests(unittest.TestCase):
    @patch("manager_tt_backend.actions.run_shell")
    @patch("manager_tt_backend.actions.bridge_token_path_for_profile")
    @patch("manager_tt_backend.actions.docker_control_script_path_for_profile")
    @patch("manager_tt_backend.actions.docker_compose_dir_for_profile")
    @patch("manager_tt_backend.actions.read_runtime_meta")
    @patch("manager_tt_backend.actions.state_dir_for_profile")
    @patch("manager_tt_backend.actions.service_name_for_profile")
    def test_delete_removes_docker_artifacts_when_state_dir_is_removed(
        self,
        service_name_for_profile,
        state_dir_for_profile,
        read_runtime_meta,
        docker_compose_dir_for_profile,
        docker_control_script_path_for_profile,
        bridge_token_path_for_profile,
        run_shell,
    ) -> None:
        service_name_for_profile.return_value = "openclaw-gateway-designer.service"
        state_dir_for_profile.return_value = Path("/tmp/.openclaw-designer")
        read_runtime_meta.return_value = {"runtimeMode": "container"}
        docker_compose_dir_for_profile.return_value = Path("/tmp/.openclaw-designer-docker")
        docker_control_script_path_for_profile.return_value = Path("/tmp/openclaw-designer-docker-service")
        bridge_token_path_for_profile.return_value = Path("/tmp/designer.token")
        run_shell.return_value = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

        delete_instance({"profile": "designer", "removeStateDir": True})

        command = run_shell.call_args.args[0]
        self.assertIn("rm -rf /tmp/.openclaw-designer", command)
        self.assertIn("rm -rf /tmp/.openclaw-designer-docker", command)
        self.assertIn("rm -f /tmp/openclaw-designer-docker-service", command)
        self.assertIn("rm -f /tmp/designer.token", command)


if __name__ == "__main__":
    unittest.main()
