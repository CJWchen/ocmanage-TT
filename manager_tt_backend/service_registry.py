"""Unified service management API.

This module provides a generic service management abstraction that can
be extended to support different service types (OpenClaw, Docker containers,
systemd services, custom processes, etc.).

The service registry maps service IDs to service handlers, allowing
consistent API operations across different service types.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

from .config import normalize_profile, service_name_for_profile, utc_now_iso
from .instances import list_instances, read_instance, openclaw_summary
from .actions import perform_instance_action
from .system import read_service_logs


@dataclasses.dataclass(frozen=True)
class ServiceInfo:
    """Basic service information for listing."""

    id: str
    name: str
    type: str
    status: str
    sub_status: str | None = None
    port: int | None = None
    profile: str | None = None


@dataclasses.dataclass(frozen=True)
class ServiceDetail:
    """Detailed service information."""

    id: str
    name: str
    type: str
    status: str
    sub_status: str | None = None
    port: int | None = None
    profile: str | None = None
    main_pid: int | None = None
    runtime_mode: str | None = None
    config: dict | None = None
    checks: list[dict] | None = None
    metadata: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class ServiceActionResult:
    """Result of a service action (start/stop/restart)."""

    id: str
    action: str
    success: bool
    returncode: int
    stdout: str | None = None
    stderr: str | None = None
    performed_at: str | None = None


@dataclasses.dataclass(frozen=True)
class ServiceLogs:
    """Service log output."""

    id: str
    lines: int
    logs: str


@dataclasses.dataclass(frozen=True)
class ServiceHealth:
    """Service health check result."""

    id: str
    healthy: bool
    checks: list[dict]
    checked_at: str


class ServiceHandler(abc.ABC):
    """Abstract base class for service handlers.

    Each service type (OpenClaw, Docker, systemd, etc.) implements
    this interface to provide consistent management operations.
    """

    @abc.abstractmethod
    def get_service_type(self) -> str:
        """Return the service type identifier (e.g., 'openclaw', 'docker')."""
        ...

    @abc.abstractmethod
    def list_services(self) -> list[ServiceInfo]:
        """List all services managed by this handler."""
        ...

    @abc.abstractmethod
    def get_service(self, service_id: str) -> ServiceDetail | None:
        """Get detailed information for a specific service."""
        ...

    @abc.abstractmethod
    def start_service(self, service_id: str) -> ServiceActionResult:
        """Start the service."""
        ...

    @abc.abstractmethod
    def stop_service(self, service_id: str) -> ServiceActionResult:
        """Stop the service."""
        ...

    @abc.abstractmethod
    def restart_service(self, service_id: str) -> ServiceActionResult:
        """Restart the service."""
        ...

    @abc.abstractmethod
    def get_logs(self, service_id: str, lines: int = 120) -> ServiceLogs:
        """Get service logs."""
        ...

    @abc.abstractmethod
    def check_health(self, service_id: str) -> ServiceHealth:
        """Check service health."""
        ...

    def can_handle(self, service_id: str) -> bool:
        """Check if this handler can manage the given service ID."""
        try:
            return self.get_service(service_id) is not None
        except Exception:
            return False


class OpenClawServiceHandler(ServiceHandler):
    """Handler for OpenClaw gateway services.

    Each OpenClaw profile (default, designer, etc.) is a service
    identified by its profile name.
    """

    SERVICE_TYPE = "openclaw"

    def get_service_type(self) -> str:
        return self.SERVICE_TYPE

    def _service_id_for_profile(self, profile: str) -> str:
        """Convert profile name to service ID."""
        return f"openclaw-{profile}"

    def _profile_for_service_id(self, service_id: str) -> str | None:
        """Convert service ID to profile name."""
        if not service_id.startswith("openclaw-"):
            return None
        return service_id.removeprefix("openclaw-")

    def list_services(self) -> list[ServiceInfo]:
        services = []
        for item in list_instances():
            profile = item.get("profile", "default")
            service_id = self._service_id_for_profile(profile)
            services.append(
                ServiceInfo(
                    id=service_id,
                    name=item.get("serviceName", service_id),
                    type=self.SERVICE_TYPE,
                    status=item.get("activeState", "unknown"),
                    sub_status=item.get("subState"),
                    port=item.get("port"),
                    profile=profile,
                )
            )
        return services

    def get_service(self, service_id: str) -> ServiceDetail | None:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            return None
        try:
            profile = normalize_profile(profile)
            instance = read_instance(profile)
            summary = instance.get("summary", {})
            runtime = instance.get("runtime", {})
            return ServiceDetail(
                id=service_id,
                name=runtime.get("serviceName", service_id),
                type=self.SERVICE_TYPE,
                status=runtime.get("activeState", "unknown"),
                sub_status=runtime.get("subState"),
                port=summary.get("port"),
                profile=profile,
                main_pid=runtime.get("mainPid"),
                runtime_mode=runtime.get("runtimeMode"),
                config=instance.get("config"),
                checks=instance.get("checks"),
                metadata={
                    "feishuRuntime": instance.get("feishuRuntime"),
                    "feishuQrSession": instance.get("feishuQrSession"),
                    "paths": instance.get("paths"),
                    "abstraction": instance.get("abstraction"),
                    "serviceFiles": instance.get("serviceFiles"),
                    "manual": instance.get("manual"),
                },
            )
        except FileNotFoundError:
            return None

    def start_service(self, service_id: str) -> ServiceActionResult:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            raise ValueError(f"Invalid OpenClaw service ID: {service_id}")
        profile = normalize_profile(profile)
        result = perform_instance_action({"profile": profile, "action": "start"})
        return ServiceActionResult(
            id=service_id,
            action="start",
            success=result.get("returncode", 1) == 0,
            returncode=int(result.get("returncode", 1)),
            stdout=result.get("stdout"),
            stderr=result.get("stderr"),
            performed_at=utc_now_iso(),
        )

    def stop_service(self, service_id: str) -> ServiceActionResult:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            raise ValueError(f"Invalid OpenClaw service ID: {service_id}")
        profile = normalize_profile(profile)
        result = perform_instance_action({"profile": profile, "action": "stop"})
        return ServiceActionResult(
            id=service_id,
            action="stop",
            success=result.get("returncode", 1) == 0,
            returncode=int(result.get("returncode", 1)),
            stdout=result.get("stdout"),
            stderr=result.get("stderr"),
            performed_at=utc_now_iso(),
        )

    def restart_service(self, service_id: str) -> ServiceActionResult:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            raise ValueError(f"Invalid OpenClaw service ID: {service_id}")
        profile = normalize_profile(profile)
        result = perform_instance_action({"profile": profile, "action": "restart"})
        return ServiceActionResult(
            id=service_id,
            action="restart",
            success=result.get("returncode", 1) == 0,
            returncode=int(result.get("returncode", 1)),
            stdout=result.get("stdout"),
            stderr=result.get("stderr"),
            performed_at=utc_now_iso(),
        )

    def get_logs(self, service_id: str, lines: int = 120) -> ServiceLogs:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            raise ValueError(f"Invalid OpenClaw service ID: {service_id}")
        profile = normalize_profile(profile)
        service_name = service_name_for_profile(profile)
        logs = read_service_logs(service_name, lines=lines)
        return ServiceLogs(
            id=service_id,
            lines=lines,
            logs=logs,
        )

    def check_health(self, service_id: str) -> ServiceHealth:
        profile = self._profile_for_service_id(service_id)
        if profile is None:
            raise ValueError(f"Invalid OpenClaw service ID: {service_id}")
        profile = normalize_profile(profile)
        try:
            instance = read_instance(profile)
            checks = instance.get("checks", [])
            healthy = all(check.get("ok", False) for check in checks)
            return ServiceHealth(
                id=service_id,
                healthy=healthy,
                checks=checks,
                checked_at=utc_now_iso(),
            )
        except FileNotFoundError:
            return ServiceHealth(
                id=service_id,
                healthy=False,
                checks=[{"name": "service_exists", "ok": False, "message": "Service not found"}],
                checked_at=utc_now_iso(),
            )


class AdapterBasedHandler(ServiceHandler):
    """Handler that wraps ServiceAdapter instances.

    This handler bridges the ServiceAdapter pattern (used by Docker, Process
    adapters) with the ServiceHandler interface used by the registry.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._load_adapters()

    def _load_adapters(self) -> None:
        """Load adapters from configuration."""
        try:
            from .services.service_loader import load_adapters_from_config
            adapters = load_adapters_from_config()
            for adapter in adapters:
                self._adapters[adapter.name] = adapter
        except Exception as exc:
            logger.warning("Failed to load adapters: %s", exc)

    def get_service_type(self) -> str:
        return "configured"

    def list_services(self) -> list[ServiceInfo]:
        services = []
        for adapter in self._adapters.values():
            info = adapter.get_info()
            status = info.status
            state = status.state.value if status else "unknown"
            services.append(
                ServiceInfo(
                    id=adapter.name,
                    name=info.display_name,
                    type=adapter.service_type,
                    status="running" if status and status.running else "stopped",
                    sub_status=state,
                    port=info.port,
                    profile=info.profile,
                )
            )
        return services

    def get_service(self, service_id: str) -> ServiceDetail | None:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            return None
        info = adapter.get_info()
        status = info.status
        return ServiceDetail(
            id=service_id,
            name=info.display_name,
            type=adapter.service_type,
            status="running" if status and status.running else "stopped",
            sub_status=status.state.value if status else None,
            port=info.port,
            profile=info.profile,
            main_pid=status.pid if status else None,
            runtime_mode=info.runtime_mode,
            config=None,
            checks=None,
            metadata=info.extra,
        )

    def start_service(self, service_id: str) -> ServiceActionResult:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            raise ValueError(f"Unknown service: {service_id}")
        success = adapter.start()
        return ServiceActionResult(
            id=service_id,
            action="start",
            success=success,
            returncode=0 if success else 1,
            performed_at=utc_now_iso(),
        )

    def stop_service(self, service_id: str) -> ServiceActionResult:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            raise ValueError(f"Unknown service: {service_id}")
        success = adapter.stop()
        return ServiceActionResult(
            id=service_id,
            action="stop",
            success=success,
            returncode=0 if success else 1,
            performed_at=utc_now_iso(),
        )

    def restart_service(self, service_id: str) -> ServiceActionResult:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            raise ValueError(f"Unknown service: {service_id}")
        success = adapter.restart()
        return ServiceActionResult(
            id=service_id,
            action="restart",
            success=success,
            returncode=0 if success else 1,
            performed_at=utc_now_iso(),
        )

    def get_logs(self, service_id: str, lines: int = 120) -> ServiceLogs:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            raise ValueError(f"Unknown service: {service_id}")
        result = adapter.get_logs(lines)
        return ServiceLogs(
            id=service_id,
            lines=result.lines,
            logs=result.logs,
        )

    def check_health(self, service_id: str) -> ServiceHealth:
        adapter = self._adapters.get(service_id)
        if adapter is None:
            return ServiceHealth(
                id=service_id,
                healthy=False,
                checks=[{"name": "adapter_found", "ok": False, "message": f"No adapter for {service_id}"}],
                checked_at=utc_now_iso(),
            )
        result = adapter.health_check()
        return ServiceHealth(
            id=service_id,
            healthy=result.healthy,
            checks=[{"name": "health_check", "ok": result.healthy, "message": result.message or ""}],
            checked_at=utc_now_iso(),
        )


class ServiceRegistry:
    """Registry of service handlers.

    The registry maps service types to handlers and provides
    a unified interface for service management operations.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ServiceHandler] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """Register built-in service handlers."""
        self.register_handler(OpenClawServiceHandler())
        self.register_handler(AdapterBasedHandler())

    def register_handler(self, handler: ServiceHandler) -> None:
        """Register a service handler."""
        self._handlers[handler.get_service_type()] = handler

    def get_handler(self, service_type: str) -> ServiceHandler | None:
        """Get handler for a service type."""
        return self._handlers.get(service_type)

    def get_handler_for_service(self, service_id: str) -> ServiceHandler | None:
        """Find the handler that can manage a given service ID."""
        for handler in self._handlers.values():
            if handler.can_handle(service_id):
                return handler
        return None

    def list_all_services(self) -> list[ServiceInfo]:
        """List all services from all handlers."""
        services = []
        for handler in self._handlers.values():
            services.extend(handler.list_services())
        return services

    def get_service(self, service_id: str) -> ServiceDetail | None:
        """Get detailed information for a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            return None
        return handler.get_service(service_id)

    def start_service(self, service_id: str) -> ServiceActionResult:
        """Start a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            raise ValueError(f"Unknown service: {service_id}")
        return handler.start_service(service_id)

    def stop_service(self, service_id: str) -> ServiceActionResult:
        """Stop a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            raise ValueError(f"Unknown service: {service_id}")
        return handler.stop_service(service_id)

    def restart_service(self, service_id: str) -> ServiceActionResult:
        """Restart a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            raise ValueError(f"Unknown service: {service_id}")
        return handler.restart_service(service_id)

    def get_logs(self, service_id: str, lines: int = 120) -> ServiceLogs:
        """Get logs for a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            raise ValueError(f"Unknown service: {service_id}")
        return handler.get_logs(service_id, lines)

    def check_health(self, service_id: str) -> ServiceHealth:
        """Check health for a service."""
        handler = self.get_handler_for_service(service_id)
        if handler is None:
            return ServiceHealth(
                id=service_id,
                healthy=False,
                checks=[{"name": "handler_found", "ok": False, "message": f"No handler for service {service_id}"}],
                checked_at=utc_now_iso(),
            )
        return handler.check_health(service_id)


# Global registry instance
_registry: ServiceRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ServiceRegistry:
    """Get the global service registry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ServiceRegistry()
    return _registry


def reset_registry() -> None:
    """Reset the global registry (for testing)."""
    global _registry
    with _registry_lock:
        _registry = None


def service_to_dict(info: ServiceInfo) -> dict:
    """Convert ServiceInfo to dictionary."""
    return {
        "id": info.id,
        "name": info.name,
        "type": info.type,
        "status": info.status,
        "subStatus": info.sub_status,
        "port": info.port,
        "profile": info.profile,
    }


def detail_to_dict(detail: ServiceDetail) -> dict:
    """Convert ServiceDetail to dictionary."""
    return {
        "id": detail.id,
        "name": detail.name,
        "type": detail.type,
        "status": detail.status,
        "subStatus": detail.sub_status,
        "port": detail.port,
        "profile": detail.profile,
        "mainPid": detail.main_pid,
        "runtimeMode": detail.runtime_mode,
        "config": detail.config,
        "checks": detail.checks,
        "metadata": detail.metadata,
        "generatedAt": utc_now_iso(),
    }


def action_result_to_dict(result: ServiceActionResult) -> dict:
    """Convert ServiceActionResult to dictionary."""
    return {
        "id": result.id,
        "action": result.action,
        "success": result.success,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "performedAt": result.performed_at or utc_now_iso(),
    }


def logs_to_dict(logs: ServiceLogs) -> dict:
    """Convert ServiceLogs to dictionary."""
    return {
        "id": logs.id,
        "lines": logs.lines,
        "logs": logs.logs,
    }


def health_to_dict(health: ServiceHealth) -> dict:
    """Convert ServiceHealth to dictionary."""
    return {
        "id": health.id,
        "healthy": health.healthy,
        "checks": health.checks,
        "checkedAt": health.checked_at,
    }
