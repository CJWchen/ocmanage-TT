import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.actions import create_instance, delete_instance
from manager_tt_backend.docker_managed import create_instance_via_docker_manager
from manager_tt_backend.instances import list_instances


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


class DockerCreatePortGuardTests(unittest.TestCase):
    def test_create_rejects_host_occupied_port_before_writing_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            missing_config = Path(tmp) / "missing-openclaw.json"

            with (
                patch("manager_tt_backend.docker_managed.config_path_for_profile", return_value=missing_config),
                patch(
                    "manager_tt_backend.docker_managed.list_port_owners",
                    return_value=[
                        {
                            "localAddress": "127.0.0.1:21789",
                            "process": 'users:(("docker-proxy",pid=4242,fd=7))',
                        }
                    ],
                ),
                patch("manager_tt_backend.docker_managed.resolve_openclaw_image") as resolve_image,
                patch("manager_tt_backend.docker_managed.prepare_profile_config") as prepare_profile_config,
            ):
                with self.assertRaisesRegex(ValueError, "21789"):
                    create_instance_via_docker_manager({"profile": "designer", "port": 21789})

        resolve_image.assert_not_called()
        prepare_profile_config.assert_not_called()


class DeleteInstanceTests(unittest.TestCase):
    def test_delete_removes_docker_artifacts_when_state_dir_is_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            openclaw_home = home / ".openclaw"
            openclaw_home.mkdir()
            state_dir = home / ".openclaw-designer"
            state_dir.mkdir()
            (state_dir / "openclaw.json").write_text('{"gateway":{"port":19789}}\n', encoding="utf-8")

            compose_dir = home / ".openclaw-designer-docker"
            compose_dir.mkdir()
            control_script_path = home / ".local" / "bin" / "openclaw-designer-docker-service"
            control_script_path.parent.mkdir(parents=True, exist_ok=True)
            control_script_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            token_path = home / ".config" / "manager-tt" / "openclaw-bridge" / "designer.token"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text("token\n", encoding="utf-8")

            systemd_dir = home / ".config" / "systemd" / "user"
            systemd_dir.mkdir(parents=True)
            service_name = "openclaw-gateway-designer.service"
            service_path = systemd_dir / service_name
            override_path = systemd_dir / f"{service_name}.d" / "override.conf"
            service_path.write_text("[Unit]\nDescription=test\n", encoding="utf-8")
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text("[Service]\nExecStart=test\n", encoding="utf-8")

            shell_calls = []

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                shell_calls.append(cmd)
                if cmd == "docker container inspect openclaw-gateway-designer":
                    return type("Result", (), {"stdout": "[]", "stderr": "", "returncode": 0})()
                if cmd == "docker rm -f openclaw-gateway-designer":
                    return type("Result", (), {"stdout": "openclaw-gateway-designer\n", "stderr": "", "returncode": 0})()
                if cmd == "docker network ls --filter label=com.docker.compose.project=openclaw-designer --format '{{.Name}}'":
                    return type("Result", (), {"stdout": "openclaw-designer_default\n", "stderr": "", "returncode": 0})()
                if cmd == "docker network inspect openclaw-designer_default":
                    return type("Result", (), {"stdout": "[]", "stderr": "", "returncode": 0})()
                if cmd == "docker network rm openclaw-designer_default":
                    return type("Result", (), {"stdout": "openclaw-designer_default\n", "stderr": "", "returncode": 0})()
                return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

            with (
                patch("manager_tt_backend.config.HOME", home),
                patch("manager_tt_backend.config.OPENCLAW_HOME", openclaw_home),
                patch("manager_tt_backend.config.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.config.OPENCLAW_BRIDGE_TOKEN_DIR", token_path.parent),
                patch("manager_tt_backend.docker_managed.HOME", home),
                patch("manager_tt_backend.actions.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.docker_managed.run_shell", side_effect=fake_run_shell),
            ):
                result = delete_instance({"profile": "designer", "removeStateDir": True})

            self.assertEqual(result["returncode"], 0)
            self.assertFalse(state_dir.exists())
            self.assertFalse(compose_dir.exists())
            self.assertFalse(control_script_path.exists())
            self.assertFalse(token_path.exists())
            self.assertFalse(service_path.exists())
            self.assertFalse(override_path.exists())
            self.assertEqual(result["archivedPaths"], [])
            self.assertIn(str(state_dir), result["removedPaths"])
            self.assertIn(str(compose_dir), result["removedPaths"])
            self.assertIn(str(control_script_path), result["removedPaths"])
            self.assertIn(str(token_path), result["removedPaths"])
            self.assertIn(
                "systemctl --user disable --now openclaw-gateway-designer.service 2>/dev/null || true",
                shell_calls,
            )
            self.assertIn("docker container inspect openclaw-gateway-designer", shell_calls)
            self.assertIn("docker rm -f openclaw-gateway-designer", shell_calls)
            self.assertIn(
                "docker network ls --filter label=com.docker.compose.project=openclaw-designer --format '{{.Name}}'",
                shell_calls,
            )
            self.assertIn("docker network rm openclaw-designer_default", shell_calls)
            self.assertIn("systemctl --user daemon-reload", shell_calls)

    def test_delete_preserves_state_but_archives_discovery_files(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            openclaw_home = home / ".openclaw"
            openclaw_home.mkdir()
            state_dir = home / ".openclaw-designer"
            state_dir.mkdir()
            config_path = state_dir / "openclaw.json"
            runtime_meta_path = state_dir / ".openclaw-runtime.json"
            bridge_tool_path = state_dir / "tools" / "openclaw_host_bridge.py"
            bridge_tool_path.parent.mkdir(parents=True, exist_ok=True)
            workspace_file = state_dir / "workspace" / "keep.txt"
            workspace_file.parent.mkdir(parents=True, exist_ok=True)
            workspace_file.write_text("keep me\n", encoding="utf-8")

            config_payload = {"gateway": {"port": 19789}}
            runtime_meta_payload = {"runtimeMode": "docker", "port": 19789}
            config_path.write_text(json.dumps(config_payload, ensure_ascii=False) + "\n", encoding="utf-8")
            runtime_meta_path.write_text(json.dumps(runtime_meta_payload, ensure_ascii=False) + "\n", encoding="utf-8")
            bridge_tool_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

            compose_dir = home / ".openclaw-designer-docker"
            compose_dir.mkdir()
            (compose_dir / "docker-compose.yml").write_text("services:\n", encoding="utf-8")
            control_script_path = home / ".local" / "bin" / "openclaw-designer-docker-service"
            control_script_path.parent.mkdir(parents=True, exist_ok=True)
            control_script_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            token_path = home / ".config" / "manager-tt" / "openclaw-bridge" / "designer.token"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text("token\n", encoding="utf-8")

            systemd_dir = home / ".config" / "systemd" / "user"
            systemd_dir.mkdir(parents=True)
            service_name = "openclaw-gateway-designer.service"
            service_path = systemd_dir / service_name
            override_path = systemd_dir / f"{service_name}.d" / "override.conf"
            service_path.write_text("[Unit]\nDescription=test\n", encoding="utf-8")
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text("[Service]\nExecStart=test\n", encoding="utf-8")

            shell_calls = []

            def fake_run_shell(cmd, timeout_ms=15000, cwd=None):
                shell_calls.append(cmd)
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with (
                patch("manager_tt_backend.config.HOME", home),
                patch("manager_tt_backend.config.OPENCLAW_HOME", openclaw_home),
                patch("manager_tt_backend.config.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.config.OPENCLAW_BRIDGE_TOKEN_DIR", token_path.parent),
                patch("manager_tt_backend.docker_managed.HOME", home),
                patch("manager_tt_backend.actions.OPENCLAW_SYSTEMD_DIR", systemd_dir),
                patch("manager_tt_backend.actions.run_shell", side_effect=fake_run_shell),
                patch("manager_tt_backend.docker_managed.run_shell", side_effect=fake_run_shell),
            ):
                result = delete_instance({"profile": "designer", "removeStateDir": False})
                items = list_instances()

            self.assertEqual(result["returncode"], 0)
            self.assertEqual(items, [])
            self.assertFalse(config_path.exists())
            self.assertFalse(runtime_meta_path.exists())
            self.assertTrue(workspace_file.exists())
            self.assertFalse(bridge_tool_path.exists())
            self.assertFalse(compose_dir.exists())
            self.assertFalse(control_script_path.exists())
            self.assertFalse(token_path.exists())
            self.assertFalse(service_path.exists())
            self.assertFalse(override_path.exists())

            archived_configs = sorted(state_dir.glob("openclaw.json.deleted.*.manager-tt"))
            archived_runtime_meta = sorted(state_dir.glob(".openclaw-runtime.json.deleted.*.manager-tt"))
            self.assertEqual(len(archived_configs), 1)
            self.assertEqual(len(archived_runtime_meta), 1)
            self.assertEqual(json.loads(archived_configs[0].read_text(encoding="utf-8")), config_payload)
            self.assertEqual(json.loads(archived_runtime_meta[0].read_text(encoding="utf-8")), runtime_meta_payload)
            self.assertEqual(result["archivedPaths"], [str(archived_configs[0]), str(archived_runtime_meta[0])])
            self.assertIn(
                "systemctl --user disable --now openclaw-gateway-designer.service 2>/dev/null || true",
                shell_calls,
            )
            self.assertTrue(
                any(cmd.startswith("docker compose --project-name openclaw-designer -f ") for cmd in shell_calls)
            )
            self.assertIn("systemctl --user daemon-reload", shell_calls)


if __name__ == "__main__":
    unittest.main()
