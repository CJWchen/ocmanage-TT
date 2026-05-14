"""Service configuration loader.

Loads service definitions from services.yaml and provides
structured access to service configurations.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

SERVICES_CONFIG_PATH = PACKAGE_ROOT / "service_config" / "services.yaml"


@dataclasses.dataclass(frozen=True)
class HealthCheckConfig:
    """Health check configuration for a service."""
    enabled: bool = False
    endpoint: str | None = None


@dataclasses.dataclass(frozen=True)
class ServiceDefinition:
    """Definition of a service from the configuration file."""
    id: str
    name: str
    type: str
    description: str | None = None
    port: int | None = None
    ports: list[int] | None = None
    primary_port: int | None = None
    icon: str | None = None
    container_name: str | None = None
    working_dir: str | None = None
    start_script: str | None = None
    stop_script: str | None = None
    log_file: str | None = None
    process_patterns: list[str] | None = None
    health_check: HealthCheckConfig | None = None
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class IconMapping:
    """Icon mapping for service types."""
    ai: str = "🤖"
    api: str = "🔌"
    desktop: str = "🖥️"
    design: str = "🎨"
    default: str = "📦"


def _parse_health_check(raw: dict | None) -> HealthCheckConfig | None:
    if not raw:
        return None
    return HealthCheckConfig(
        enabled=bool(raw.get("enabled", False)),
        endpoint=raw.get("endpoint"),
    )


def _parse_service(raw: dict) -> ServiceDefinition:
    health_raw = raw.get("health_check")
    ports_raw = raw.get("ports")
    return ServiceDefinition(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        type=raw.get("type", "process"),
        description=raw.get("description"),
        port=raw.get("port"),
        ports=ports_raw if isinstance(ports_raw, list) else None,
        primary_port=raw.get("primary_port"),
        icon=raw.get("icon"),
        container_name=raw.get("container_name"),
        working_dir=raw.get("working_dir"),
        start_script=raw.get("start_script"),
        stop_script=raw.get("stop_script"),
        log_file=raw.get("log_file"),
        process_patterns=raw.get("process_patterns"),
        health_check=_parse_health_check(health_raw),
        extra=raw.get("extra", {}),
    )


def _parse_icons(raw: dict | None) -> IconMapping:
    if not raw:
        return IconMapping()
    return IconMapping(
        ai=raw.get("ai", "🤖"),
        api=raw.get("api", "🔌"),
        desktop=raw.get("desktop", "🖥️"),
        design=raw.get("design", "🎨"),
        default=raw.get("default", "📦"),
    )


@dataclasses.dataclass
class ServicesConfig:
    """Loaded services configuration."""
    services: list[ServiceDefinition]
    icons: IconMapping

    def get_service(self, service_id: str) -> ServiceDefinition | None:
        for svc in self.services:
            if svc.id == service_id:
                return svc
        return None

    def get_services_by_type(self, service_type: str) -> list[ServiceDefinition]:
        return [svc for svc in self.services if svc.type == service_type]

    def get_icon(self, icon_name: str | None) -> str:
        if not icon_name:
            return self.icons.default
        return getattr(self.icons, icon_name, self.icons.default)


def load_services_config() -> ServicesConfig:
    """Load services configuration from YAML file."""
    if yaml is None:
        return ServicesConfig(services=[], icons=IconMapping())

    if not SERVICES_CONFIG_PATH.exists():
        return ServicesConfig(services=[], icons=IconMapping())

    try:
        with SERVICES_CONFIG_PATH.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return ServicesConfig(services=[], icons=IconMapping())

    services_raw = raw.get("services", [])
    services = [_parse_service(s) for s in services_raw if isinstance(s, dict)]

    icons_raw = raw.get("icons")
    icons = _parse_icons(icons_raw)

    return ServicesConfig(services=services, icons=icons)


_config: ServicesConfig | None = None


def get_services_config() -> ServicesConfig:
    """Get the global services configuration."""
    global _config
    if _config is None:
        _config = load_services_config()
    return _config


def reset_services_config() -> None:
    """Reset the global configuration (for testing)."""
    global _config
    _config = None