from __future__ import annotations

import json
import shlex
from typing import Any

from ...core import (
    HealthCheckResult,
    LogResult,
    ServiceAdapter,
    ServiceInfo,
    ServiceState,
    ServiceStatus,
)
from ...system import run_shell


class DockerAdapter(ServiceAdapter):
    """Docker 容器服务适配器

    通过容器名管理任意 Docker 容器。
    支持 start/stop/restart/logs 和可选的健康检查。
    """

    def __init__(
        self,
        container_name: str,
        *,
        display_name: str | None = None,
        health_endpoint: str | None = None,
        health_port: int | None = None,
        profile: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self._container_name = container_name
        self._display_name = display_name or container_name
        self._health_endpoint = health_endpoint
        self._health_port = health_port
        self._profile = profile
        self._extra = extra or {}

    @property
    def name(self) -> str:
        return self._container_name

    @property
    def service_type(self) -> str:
        return "docker"

    def _inspect_container(self) -> dict | None:
        result = run_shell(
            f"docker inspect {shlex.quote(self._container_name)}",
            timeout_ms=15000,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)[0]
        except (json.JSONDecodeError, IndexError):
            return None

    def _get_container_state(self) -> dict:
        info = self._inspect_container()
        if not info:
            return {"status": "not_found", "running": False}
        return info.get("State", {})

    def get_status(self) -> ServiceStatus:
        state = self._get_container_state()

        if state.get("status") == "not_found":
            return ServiceStatus(
                state=ServiceState.UNKNOWN,
                running=False,
                error="容器不存在",
                extra=self._extra,
            )

        status = state.get("Status", "unknown")
        running = bool(state.get("Running", False))
        pid = state.get("Pid")
        started_at = state.get("StartedAt")
        exit_code = state.get("ExitCode")

        if running:
            service_state = ServiceState.RUNNING
        elif status == "exited" or status == "dead":
            service_state = ServiceState.STOPPED
        elif status == "paused":
            service_state = ServiceState.STOPPED
        elif status == "restarting":
            service_state = ServiceState.STARTING
        else:
            service_state = ServiceState.UNKNOWN

        return ServiceStatus(
            state=service_state,
            running=running,
            pid=int(pid) if pid and pid > 0 else None,
            started_at=started_at,
            exit_code=int(exit_code) if exit_code is not None else None,
            extra={"container_status": status, **self._extra},
        )

    def start(self) -> bool:
        result = run_shell(
            f"docker start {shlex.quote(self._container_name)}",
            timeout_ms=60000,
        )
        return result.returncode == 0

    def stop(self) -> bool:
        result = run_shell(
            f"docker stop {shlex.quote(self._container_name)}",
            timeout_ms=60000,
        )
        return result.returncode == 0

    def restart(self) -> bool:
        result = run_shell(
            f"docker restart {shlex.quote(self._container_name)}",
            timeout_ms=90000,
        )
        return result.returncode == 0

    def get_logs(self, lines: int = 100) -> LogResult:
        safe_lines = max(1, min(lines, 2000))
        result = run_shell(
            f"docker logs {shlex.quote(self._container_name)} --tail {safe_lines} 2>&1",
            timeout_ms=30000,
        )
        logs = result.stdout or ""
        log_lines = logs.count("\n") if logs else 0
        truncated = log_lines >= safe_lines
        return LogResult(logs=logs, lines=log_lines, truncated=truncated)

    def health_check(self) -> HealthCheckResult:
        state = self._get_container_state()

        if state.get("status") == "not_found":
            return HealthCheckResult(
                healthy=False,
                message="容器不存在",
            )

        if not state.get("Running"):
            return HealthCheckResult(
                healthy=False,
                message=f"容器未运行: {state.get('Status', 'unknown')}",
            )

        details: dict[str, Any] = {
            "container_name": self._container_name,
            "status": state.get("Status"),
            "started_at": state.get("StartedAt"),
        }

        if self._health_endpoint and self._health_port:
            health_url = self._health_endpoint.format(port=self._health_port)
            check_result = run_shell(
                f"curl -sf -o /dev/null -w '%{{http_code}}' {shlex.quote(health_url)} --connect-timeout 5 --max-time 10 2>/dev/null || echo '000'",
                timeout_ms=15000,
            )
            http_code = (check_result.stdout or "").strip()
            details["http_code"] = http_code
            if http_code.startswith("2"):
                return HealthCheckResult(
                    healthy=True,
                    message="容器运行中且健康检查通过",
                    details=details,
                )
            return HealthCheckResult(
                healthy=False,
                message=f"容器运行中但健康检查失败: HTTP {http_code}",
                details=details,
            )

        return HealthCheckResult(
            healthy=True,
            message="容器运行中",
            details=details,
        )

    def get_info(self) -> ServiceInfo:
        info = self._inspect_container()
        status = self.get_status()

        extra: dict[str, Any] = {**self._extra}
        port: int | None = None

        if info:
            config = info.get("Config", {})
            network_settings = info.get("NetworkSettings", {})
            ports = network_settings.get("Ports") or {}

            extra["image"] = config.get("Image")
            extra["created"] = info.get("Created")

            for container_port, host_bindings in ports.items():
                if host_bindings:
                    for binding in host_bindings:
                        host_port = binding.get("HostPort")
                        if host_port:
                            port = int(host_port)
                            break
                if port:
                    break

        return ServiceInfo(
            name=self._container_name,
            display_name=self._display_name,
            service_type=self.service_type,
            profile=self._profile,
            port=port,
            status=status,
            extra=extra,
        )
