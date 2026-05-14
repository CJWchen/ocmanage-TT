from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...config import (
    config_path_for_profile,
    normalize_profile,
    state_dir_for_profile,
    service_name_for_profile,
    utc_now_iso,
)
from ...core import (
    HealthCheckResult,
    LogResult,
    ServiceAdapter,
    ServiceInfo,
    ServiceState,
    ServiceStatus,
)
from ...instances import (
    build_instance_checks,
    collect_runtime_info,
    extract_config_summary,
    read_instance,
    list_instances,
)
from ...system import run_shell, read_service_logs, list_port_owners


@dataclass
class FeishuSubResource:
    """飞书子资源状态"""
    enabled: bool = False
    channel_running: bool = False
    plugin_loaded: bool = False
    ready: bool = False
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelSubResource:
    """模型子资源配置"""
    primary: str | None = None
    fallback: str | None = None
    provider: str | None = None
    api_key_configured: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class OpenClawAdapter(ServiceAdapter):
    """OpenClaw 服务适配器

    封装 OpenClaw 实例的管理逻辑，支持多 profile 实例。
    """

    def __init__(self, profile: str = "default"):
        self._profile = normalize_profile(profile)
        self._service_name = service_name_for_profile(self._profile)
        self._config_path = config_path_for_profile(self._profile)
        self._state_dir = state_dir_for_profile(self._profile)

    @property
    def name(self) -> str:
        return self._service_name

    @property
    def service_type(self) -> str:
        return "openclaw"

    @property
    def profile(self) -> str:
        return self._profile

    def get_status(self) -> ServiceStatus:
        try:
            runtime = collect_runtime_info(self._profile)
            state = self._map_active_state(runtime.get("activeState", "unknown"))
            return ServiceStatus(
                state=state,
                running=runtime.get("activeState") == "active",
                pid=runtime.get("mainPid"),
                extra={
                    "subState": runtime.get("subState"),
                    "runtimeMode": runtime.get("runtimeMode", "systemd"),
                    "portOwners": runtime.get("portOwners", []),
                },
            )
        except Exception as exc:
            return ServiceStatus(
                state=ServiceState.UNKNOWN,
                running=False,
                error=str(exc),
            )

    def start(self) -> bool:
        result = run_shell(
            f"systemctl --user start {self._service_name}",
            timeout_ms=45000,
        )
        return result.returncode == 0

    def stop(self) -> bool:
        result = run_shell(
            f"systemctl --user stop {self._service_name}",
            timeout_ms=30000,
        )
        return result.returncode == 0

    def restart(self) -> bool:
        result = run_shell(
            f"systemctl --user restart {self._service_name}",
            timeout_ms=45000,
        )
        return result.returncode == 0

    def get_logs(self, lines: int = 100) -> LogResult:
        logs = read_service_logs(self._service_name, lines)
        return LogResult(
            logs=logs,
            lines=len(logs.splitlines()) if logs else 0,
        )

    def health_check(self) -> HealthCheckResult:
        try:
            detail = read_instance(self._profile)
            checks = detail.get("checks", [])
            failed = [c for c in checks if not c.get("ok")]
            if not failed:
                return HealthCheckResult(
                    healthy=True,
                    message="所有检查通过",
                    details={"checkCount": len(checks)},
                )
            return HealthCheckResult(
                healthy=False,
                message=f"{len(failed)} 项检查失败",
                details={
                    "failedChecks": [
                        {"name": c.get("name"), "message": c.get("message")}
                        for c in failed
                    ],
                },
            )
        except FileNotFoundError:
            return HealthCheckResult(
                healthy=False,
                message=f"实例配置不存在: {self._profile}",
            )
        except Exception as exc:
            return HealthCheckResult(
                healthy=False,
                message=str(exc),
            )

    def get_info(self) -> ServiceInfo:
        try:
            detail = read_instance(self._profile)
            summary = detail.get("summary", {})
            runtime = detail.get("runtime", {})
            status = self.get_status()
            return ServiceInfo(
                name=self._service_name,
                display_name=f"OpenClaw ({self._profile})",
                service_type="openclaw",
                profile=self._profile,
                port=summary.get("port"),
                config_path=str(self._config_path),
                state_dir=str(self._state_dir),
                workspace_dir=summary.get("workspace"),
                runtime_mode=runtime.get("runtimeMode", "systemd"),
                status=status,
                extra={
                    "feishu": summary.get("feishu"),
                    "primaryModel": summary.get("primaryModel"),
                    "activeState": runtime.get("activeState"),
                },
            )
        except Exception as exc:
            return ServiceInfo(
                name=self._service_name,
                display_name=f"OpenClaw ({self._profile})",
                service_type="openclaw",
                profile=self._profile,
                status=self.get_status(),
                extra={"error": str(exc)},
            )

    def get_feishu_status(self) -> FeishuSubResource:
        try:
            detail = read_instance(self._profile)
            feishu_summary = detail.get("summary", {}).get("feishu") or {}
            feishu_runtime = detail.get("feishuRuntime") or {}
            enabled = bool(feishu_summary.get("appId"))
            plugin = feishu_runtime.get("plugin") or {}
            channel = feishu_runtime.get("channel") or {}
            return FeishuSubResource(
                enabled=enabled,
                channel_running=bool(channel.get("running")),
                plugin_loaded=bool(plugin.get("loaded")),
                ready=bool(feishu_runtime.get("ready")),
                error=channel.get("lastError") or plugin.get("error"),
                details=feishu_runtime,
            )
        except Exception as exc:
            return FeishuSubResource(
                enabled=False,
                error=str(exc),
            )

    def get_model_config(self) -> ModelSubResource:
        try:
            detail = read_instance(self._profile)
            summary = detail.get("summary", {})
            return ModelSubResource(
                primary=summary.get("primaryModel"),
                provider=summary.get("toolProfile"),
                api_key_configured=summary.get("primaryModel") is not None,
                details={
                    "toolProfile": summary.get("toolProfile"),
                    "browserEnabled": summary.get("browserEnabled"),
                },
            )
        except Exception as exc:
            return ModelSubResource(
                details={"error": str(exc)},
            )

    def _map_active_state(self, state: str) -> ServiceState:
        mapping = {
            "active": ServiceState.RUNNING,
            "running": ServiceState.RUNNING,
            "inactive": ServiceState.STOPPED,
            "stopped": ServiceState.STOPPED,
            "failed": ServiceState.FAILED,
            "activating": ServiceState.STARTING,
            "deactivating": ServiceState.STOPPING,
        }
        return mapping.get(state.lower(), ServiceState.UNKNOWN)


def list_openclaw_adapters() -> list[OpenClawAdapter]:
    """列出所有 OpenClaw 实例的适配器"""
    adapters = []
    for item in list_instances():
        profile = item.get("profile", "default")
        adapters.append(OpenClawAdapter(profile))
    return adapters


def get_openclaw_adapter(profile: str = "default") -> OpenClawAdapter:
    """获取指定 profile 的 OpenClaw 适配器"""
    return OpenClawAdapter(profile)
