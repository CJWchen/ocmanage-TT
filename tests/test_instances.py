import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.instances import (
    build_docker_bridge_alignment_check,
    build_path_translation,
    find_docker_session_host_path_refs,
    list_instances,
)


class DockerBridgeAlignmentCheckTests(unittest.TestCase):
    def test_bridge_alignment_passes_when_runtime_meta_matches_current_manager(self) -> None:
        runtime = {
            "hostControlBridge": {
                "enabled": True,
                "listenPort": 58081,
                "baseUrl": "http://host.docker.internal:58081",
                "tokenPath": "/tmp/designer.token",
            },
            "runtimeMeta": {
                "hostControlBridge": {
                    "port": 58081,
                    "baseUrl": "http://host.docker.internal:58081",
                    "tokenPath": "/tmp/designer.token",
                }
            },
        }

        check = build_docker_bridge_alignment_check(runtime)

        self.assertTrue(check["ok"])
        self.assertEqual(check["name"], "docker_bridge_matches_runtime_meta")

    def test_bridge_alignment_fails_when_bridge_port_drifted_from_runtime_meta(self) -> None:
        runtime = {
            "hostControlBridge": {
                "enabled": True,
                "listenPort": 58181,
                "baseUrl": "http://host.docker.internal:58181",
                "tokenPath": "/tmp/designer.token",
            },
            "runtimeMeta": {
                "hostControlBridge": {
                    "port": 58081,
                    "baseUrl": "http://host.docker.internal:58081",
                    "tokenPath": "/tmp/designer.token",
                }
            },
        }

        check = build_docker_bridge_alignment_check(runtime)

        self.assertFalse(check["ok"])
        self.assertIn("port=58081 / current=58181", check["message"])


class InstanceListingFallbackTests(unittest.TestCase):
    def test_list_instances_preserves_docker_runtime_mode_when_config_read_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".openclaw-designer" / "openclaw.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{broken", encoding="utf-8")

            with (
                patch("manager_tt_backend.instances.list_openclaw_configs", return_value=[config_path]),
                patch("manager_tt_backend.instances.load_json", side_effect=ValueError("bad config")),
                patch("manager_tt_backend.instances.read_runtime_meta", return_value={"runtimeMode": "container"}),
                patch(
                    "manager_tt_backend.instances.build_host_control_bridge",
                    return_value={"enabled": True, "listenPort": 58081},
                ),
            ):
                items = list_instances()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["runtimeMode"], "docker")
        self.assertEqual(items[0]["hostControlBridge"], {"enabled": True, "listenPort": 58081})
        self.assertIn("bad config", items[0]["error"])


class DockerSessionPathSafetyTests(unittest.TestCase):
    def test_same_path_state_mount_is_treated_as_container_safe(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            state_dir = home / ".openclaw-designer"
            workspace_dir = home / ".openclaw" / "workspace-designer"
            sessions_dir = state_dir / "agents" / "main" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            compose_path = home / ".openclaw-designer-docker" / "docker-compose.yml"
            compose_path.parent.mkdir(parents=True, exist_ok=True)
            compose_path.write_text(
                "\n".join(
                    [
                        "services:",
                        "  openclaw-gateway:",
                        "    volumes:",
                        f"      - {state_dir}:/home/node/.openclaw",
                        f"      - {state_dir}:{state_dir}",
                        f"      - {workspace_dir}:{workspace_dir}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            session_path = sessions_dir / "designer.jsonl"
            session_path.write_text(
                json.dumps(
                    {
                        "cwd": str(state_dir / "npm" / "node_modules" / "@openclaw" / "feishu"),
                        "workspaceDir": str(workspace_dir),
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            runtime = {
                "runtimeMode": "docker",
                "runtimeMeta": {
                    "composePath": str(compose_path),
                    "workspaceHostPath": str(workspace_dir),
                    "workspaceContainerPath": str(workspace_dir),
                },
            }
            summary = {"workspace": str(workspace_dir)}

            with patch("manager_tt_backend.config.HOME", home):
                translation = build_path_translation("designer", summary, runtime)
                hits = find_docker_session_host_path_refs("designer", summary, runtime)

        self.assertIn(str(state_dir), translation["containerStateAliases"])
        self.assertEqual(hits, [])

    def test_translated_state_paths_still_warn_without_same_path_mount(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            state_dir = home / ".openclaw-designer"
            sessions_dir = state_dir / "agents" / "main" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            compose_path = home / ".openclaw-designer-docker" / "docker-compose.yml"
            compose_path.parent.mkdir(parents=True, exist_ok=True)
            compose_path.write_text(
                "\n".join(
                    [
                        "services:",
                        "  openclaw-gateway:",
                        "    volumes:",
                        f"      - {state_dir}:/home/node/.openclaw",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            session_path = sessions_dir / "designer.jsonl"
            session_path.write_text(
                json.dumps({"configPath": str(state_dir / "openclaw.json")}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            runtime = {
                "runtimeMode": "docker",
                "runtimeMeta": {
                    "composePath": str(compose_path),
                },
            }

            with patch("manager_tt_backend.config.HOME", home):
                hits = find_docker_session_host_path_refs("designer", {}, runtime)

        self.assertEqual(hits, [str(session_path)])


if __name__ == "__main__":
    unittest.main()
