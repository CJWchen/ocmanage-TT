from __future__ import annotations

DOCKER_CREATE_MODE = "docker"
HOST_CREATE_MODE = "host"
DOCKER_CREATE_MODE_ALIASES = {"docker", "container"}
HOST_CREATE_MODE_ALIASES = {"host", "local", "systemd", "default", "host-managed", "host_managed"}


def canonical_create_mode(value: object) -> str | None:
    if value in (None, ""):
        return None

    normalized = str(value).strip().lower()
    if normalized in DOCKER_CREATE_MODE_ALIASES:
        return DOCKER_CREATE_MODE
    if normalized in HOST_CREATE_MODE_ALIASES:
        return HOST_CREATE_MODE
    return None


def normalize_runtime_mode(value: object, *, default: str = "systemd") -> str:
    canonical = canonical_create_mode(value)
    if canonical == DOCKER_CREATE_MODE:
        return DOCKER_CREATE_MODE
    if canonical == HOST_CREATE_MODE:
        return "systemd"
    if value in (None, ""):
        return default

    normalized = str(value).strip()
    return normalized or default


def resolve_create_mode(payload: dict) -> str:
    raw_mode = None
    for key in ("runtimeMode", "runtime", "mode"):
        value = payload.get(key)
        if value not in (None, ""):
            raw_mode = value
            break

    if raw_mode is None:
        return DOCKER_CREATE_MODE

    canonical = canonical_create_mode(raw_mode)
    if canonical:
        return canonical
    raise ValueError(f"未知创建模式: {raw_mode}")


def host_managed_instance_profiles(instances: list[dict]) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()

    for item in instances:
        if normalize_runtime_mode(item.get("runtimeMode")) == DOCKER_CREATE_MODE:
            continue
        profile = str(item.get("profile") or item.get("configPath") or "<unknown>").strip()
        if profile in seen:
            continue
        seen.add(profile)
        profiles.append(profile)

    return profiles


def ensure_host_create_allowed(instances: list[dict]) -> None:
    profiles = host_managed_instance_profiles(instances)
    if profiles:
        joined = ", ".join(profiles)
        raise ValueError(f"已存在本机实例: {joined}。本机实例只能有一个，请改用 Docker 实例。")
