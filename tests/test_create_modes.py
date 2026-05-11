import unittest
from pathlib import Path
from unittest.mock import patch

from manager_tt_backend.create_modes import (
    DOCKER_CREATE_MODE,
    HOST_CREATE_MODE,
    canonical_create_mode,
    ensure_host_create_allowed,
    host_managed_instance_profiles,
    normalize_runtime_mode,
    resolve_create_mode,
)
from manager_tt_backend.docker_managed import (
    build_docker_runtime_meta,
    resolve_create_port,
    suggest_next_gateway_port,
)


class CreateModeTests(unittest.TestCase):
    def test_resolve_create_mode_defaults_to_docker(self) -> None:
        self.assertEqual(resolve_create_mode({}), DOCKER_CREATE_MODE)

    def test_resolve_create_mode_maps_host_synonyms(self) -> None:
        self.assertEqual(resolve_create_mode({"runtimeMode": "systemd"}), HOST_CREATE_MODE)
        self.assertEqual(resolve_create_mode({"mode": "local"}), HOST_CREATE_MODE)
        self.assertEqual(canonical_create_mode("container"), DOCKER_CREATE_MODE)

    def test_host_managed_instance_profiles_treats_non_docker_as_host(self) -> None:
        profiles = host_managed_instance_profiles(
            [
                {"profile": "designer", "runtimeMode": "container"},
                {"profile": "default", "runtimeMode": "systemd"},
                {"profile": "legacy"},
            ]
        )

        self.assertEqual(profiles, ["default", "legacy"])

    def test_normalize_runtime_mode_keeps_api_labels_canonical(self) -> None:
        self.assertEqual(normalize_runtime_mode("container"), DOCKER_CREATE_MODE)
        self.assertEqual(normalize_runtime_mode("host-managed"), "systemd")

    def test_ensure_host_create_allowed_rejects_when_host_instance_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "default"):
            ensure_host_create_allowed([{"profile": "default", "runtimeMode": "systemd"}])


class DockerManagedPureLogicTests(unittest.TestCase):
    def test_suggest_next_gateway_port_uses_existing_port_series(self) -> None:
        self.assertEqual(suggest_next_gateway_port([18789, 19789, 20789]), 21789)

    @patch("manager_tt_backend.docker_managed.list_port_owners")
    @patch("manager_tt_backend.docker_managed.read_configured_gateway_ports", return_value=[18789, 19789, 20789])
    def test_resolve_create_port_skips_host_occupied_auto_candidate(self, _, list_port_owners) -> None:
        list_port_owners.side_effect = lambda port: (
            [{"localAddress": "127.0.0.1:21789", "process": "users:((\"docker-proxy\"))"}] if port == 21789 else []
        )

        self.assertEqual(resolve_create_port({}), 22789)

    def test_build_docker_runtime_meta_uses_preserved_workspace_shape(self) -> None:
        meta = build_docker_runtime_meta(
            profile="designer",
            image="ghcr.io/openclaw/openclaw:2026.5.6",
            port=19789,
            compose_dir=Path("/tmp/designer-docker"),
            control_script_path=Path("/tmp/openclaw-designer-docker-service"),
            workspace_dir=Path("/tmp/workspace-designer"),
            bridge_token_path=Path("/tmp/designer.token"),
            bridge_tool_path=Path("/tmp/openclaw_host_bridge.py"),
        )

        self.assertEqual(meta["runtimeMode"], "docker")
        self.assertEqual(meta["workspaceMode"], "host-path-preserved")
        self.assertEqual(meta["workspaceHostPath"], "/tmp/workspace-designer")
        self.assertEqual(meta["workspaceContainerPath"], "/tmp/workspace-designer")


if __name__ == "__main__":
    unittest.main()
