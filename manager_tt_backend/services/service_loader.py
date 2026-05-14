"""Service loader that creates adapters from configuration."""

from __future__ import annotations

from typing import Any

from ..service_config.services_config import (
    ServiceDefinition,
    get_services_config,
    ServicesConfig,
)
from ..core import ServiceAdapter
from .docker_generic import DockerAdapter
from .process import ProcessAdapter


def create_docker_adapter(definition: ServiceDefinition) -> DockerAdapter:
    """Create a Docker adapter from a service definition."""
    health_endpoint = None
    health_port = None
    if definition.health_check and definition.health_check.enabled:
        health_endpoint = definition.health_check.endpoint
        health_port = definition.port

    return DockerAdapter(
        container_name=definition.container_name or definition.id,
        display_name=definition.name,
        health_endpoint=health_endpoint,
        health_port=health_port,
        profile=None,
        extra={
            "description": definition.description,
            "icon": definition.icon,
            **definition.extra,
        },
    )


def create_process_adapter(definition: ServiceDefinition) -> ProcessAdapter:
    """Create a Process adapter from a service definition."""
    port = definition.primary_port or definition.port
    if port is None and definition.ports:
        port = definition.ports[0]

    return ProcessAdapter(
        name=definition.id,
        display_name=definition.name,
        start_script=definition.start_script or "",
        stop_script=definition.stop_script,
        port=port,
        log_file=definition.log_file,
        working_dir=definition.working_dir,
        extra={
            "description": definition.description,
            "icon": definition.icon,
            "ports": definition.ports or ([definition.port] if definition.port else []),
            "process_patterns": definition.process_patterns or [],
            **definition.extra,
        },
    )


def create_adapter(definition: ServiceDefinition) -> ServiceAdapter | None:
    """Create an adapter from a service definition."""
    adapter_type = definition.type

    if adapter_type == "docker":
        return create_docker_adapter(definition)
    elif adapter_type == "process":
        return create_process_adapter(definition)
    else:
        return None


def load_adapters_from_config() -> list[ServiceAdapter]:
    """Load all adapters from the services configuration."""
    config = get_services_config()
    adapters: list[ServiceAdapter] = []

    for definition in config.services:
        adapter = create_adapter(definition)
        if adapter:
            adapters.append(adapter)

    return adapters


def get_adapter_registry() -> dict[str, ServiceAdapter]:
    """Get a dictionary of service ID to adapter."""
    adapters = load_adapters_from_config()
    return {adapter.name: adapter for adapter in adapters}
