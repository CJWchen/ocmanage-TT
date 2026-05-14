from __future__ import annotations

import re
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
from ...system import read_service_logs, read_systemd_show, run_shell


class SystemdAdapter(ServiceAdapter):
    """Systemd user service 适配器

    通过 service 名管理 systemd user service。
    使用 systemctl --user 命令进行控制。
    """

    def __init__(
        self,
        service_name: str,
        *,
        display_name: str | None = None,
        profile: str | None = None,
        port: int | None = None,
        health_url: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self._service_name = service_name
        self._display_name = display_name or service_name
        self._profile = profile
        self._port = port
        self._health_url = health_url
        self._extra = extra or {}

    @property
    def name(self) -> str:
        return self._service_name

    @property
    def service_type(self) -> str:
        return "systemd"

    def _parse_active_state(self, state: str, sub_state: str) -> ServiceState:
        state = (state or "unknown").lower()
        sub_state = (sub_state or "unknown").lower()

        if state == "active":
            if sub_state in ("running",):
                return ServiceState.RUNNING
            if sub_state in ("start", "start-pre", "start-post"):
                return ServiceState.STARTING
            return ServiceState.RUNNING
        elif state in ("inactive", "failed"):
            if sub_state in ("stop", "stop-post", "stop-sigterm", "stop-sigkill"):
                return ServiceState.STOPPING
            return ServiceState.STOPPED
        elif state == "activating":
            return ServiceState.STARTING
        elif state == "deactivating":
            return ServiceState.STOPPING
        elif state in ("reloading",):
            return ServiceState.RUNNING

        return ServiceState.UNKNOWN

    def get_status(self) -> ServiceStatus:
        props = read_systemd_show(self._service_name)

        active_state = props.get("ActiveState", "unknown")
        sub_state = props.get("SubState", "unknown")
        state = self._parse_active_state(active_state, sub_state)

        running = active_state.lower() == "active" and sub_state.lower() == "running"

        pid_str = props.get("MainPID", "0")
        pid = int(pid_str) if pid_str.isdigit() else None
        pid = pid if pid and pid > 0 else None

        extra: dict[str, Any] = {
            "unit_file_state": props.get("UnitFileState"),
            "description": props.get("Description"),
            **self._extra,
        }

        return ServiceStatus(
            state=state,
            running=running,
            pid=pid,
            extra=extra,
        )

    def _run_systemctl(self, action: str, timeout_ms: int = 60000) -> bool:
        result = run_shell(
            f"systemctl --user {action} {shlex.quote(self._service_name)}",
            timeout_ms=timeout_ms,
        )
        return result.returncode == 0

    def start(self) -> bool:
        return self._run_systemctl("start", timeout_ms=60000)

    def stop(self) -> bool:
        return self._run_systemctl("stop", timeout_ms=60000)

    def restart(self) -> bool:
        return self._run_systemctl("restart", timeout_ms=90000)

    def get_logs(self, lines: int = 100) -> LogResult:
        safe_lines = max(1, min(lines, 400))
        logs = read_service_logs(self._service_name, lines=safe_lines)
        log_lines = logs.count("\n") if logs else 0
        truncated = log_lines >= safe_lines
        return LogResult(logs=logs, lines=log_lines, truncated=truncated)

    def health_check(self) -> HealthCheckResult:
        status = self.get_status()
        details: dict[str, Any] = {
            "service_name": self._service_name,
        }

        if not status.running:
            return HealthCheckResult(
                healthy=False,
                message=f"服务未运行: {status.state.value}",
                details=details,
            )

        if self._health_url:
            check_result = run_shell(
                f"curl -sf -o /dev/null -w '%{{http_code}}' {shlex.quote(self._health_url)} --connect-timeout 5 --max-time 10 2>/dev/null || echo '000'",
                timeout_ms=15000,
            )
            http_code = (check_result.stdout or "").strip()
            details["http_code"] = http_code
            if http_code.startswith("2"):
                return HealthCheckResult(
                    healthy=True,
                    message="服务运行中且健康检查通过",
                    details=details,
                )
            return HealthCheckResult(
                healthy=False,
                message=f"服务运行中但健康检查失败: HTTP {http_code}",
                details=details,
            )

        return HealthCheckResult(
            healthy=True,
            message="服务运行中",
            details=details,
        )

    def get_info(self) -> ServiceInfo:
        props = read_systemd_show(self._service_name)
        status = self.get_status()

        extra: dict[str, Any] = {
            "unit_file_state": props.get("UnitFileState"),
            "description": props.get("Description"),
            "fragment_path": props.get("FragmentPath"),
            **self._extra,
        }

        return ServiceInfo(
            name=self._service_name,
            display_name=self._display_name,
            service_type=self.service_type,
            profile=self._profile,
            port=self._port,
            config_path=props.get("FragmentPath"),
            status=status,
            extra=extra,
        )

    def enable(self) -> bool:
        """启用服务开机自启"""
        return self._run_systemctl("enable", timeout_ms=20000)

    def disable(self) -> bool:
        """禁用服务开机自启"""
        return self._run_systemctl("disable", timeout_ms=20000)

    def is_enabled(self) -> bool:
        """检查服务是否已启用"""
        props = read_systemd_show(self._service_name)
        state = props.get("UnitFileState", "").lower()
        return state == "enabled"

    def daemon_reload(self) -> bool:
        """重载 systemd 守护进程配置"""
        result = run_shell("systemctl --user daemon-reload", timeout_ms=20000)
        return result.returncode == 0
