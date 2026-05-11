import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from manager_tt_backend.instances import build_docker_bridge_alignment_check, list_instances


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


if __name__ == "__main__":
    unittest.main()
