from __future__ import annotations

import datetime as dt
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 58080
    bridge_port: int = 58081
    bridge_host: str = os.environ.get("OPENCLAW_DOCKER_BRIDGE_HOST", "172.17.0.1").strip() or "172.17.0.1"
    docker_bridge_enabled: bool = True


settings = ServerSettings()

HOME = Path.home()
PACKAGE_ROOT = Path(__file__).resolve().parent
MANAGER_ROOT = PACKAGE_ROOT.parent
OPENCLAW_HOME = HOME / ".openclaw"
OPENCLAW_SYSTEMD_DIR = HOME / ".config" / "systemd" / "user"
MANAGER_CONFIG_DIR = HOME / ".config" / "manager-tt"
OPENCLAW_BRIDGE_TOKEN_DIR = MANAGER_CONFIG_DIR / "openclaw-bridge"
MANAGER_AUDIT_LOG = MANAGER_CONFIG_DIR / "openclaw-action-audit.jsonl"
OPENCLAW_INSTANCE_BIN = HOME / ".local" / "bin" / "openclaw-instance"
OPENCLAW_BIN = HOME / ".npm-global" / "bin" / "openclaw"
OPENCLAW_LOG_DIR = Path("/tmp/openclaw")
RUNTIME_META_NAME = ".openclaw-runtime.json"
CONTAINER_STATE_DIR = "/home/node/.openclaw"
CONTAINER_WORKSPACE_DIR = f"{CONTAINER_STATE_DIR}/workspace"

DEFAULT_TIMEOUT_MS = 15000
MAX_TIMEOUT_MS = 60000

OPENCLAW_RESERVED_PROFILES = {"default"}
EXEC_DANGEROUS_PATTERNS = [
    "rm -rf",
    "mkfs",
    "dd if=",
    "> /dev/",
    ":(){ :|:& };:",
]


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def ensure_safe_profile_name(profile: str) -> str:
    value = (profile or "").strip()
    if not value:
        raise ValueError("profile 不能为空")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(ch not in allowed for ch in value):
        raise ValueError(f"非法 profile: {profile}")
    return value


def normalize_profile(profile: str | None) -> str:
    value = (profile or "default").strip()
    if value in {"", "openclaw"}:
        return "default"
    return ensure_safe_profile_name(value)


def service_name_for_profile(profile: str) -> str:
    return "openclaw-gateway.service" if profile == "default" else f"openclaw-gateway-{profile}.service"


def state_dir_for_profile(profile: str) -> Path:
    return OPENCLAW_HOME if profile == "default" else HOME / f".openclaw-{profile}"


def config_path_for_profile(profile: str) -> Path:
    return state_dir_for_profile(profile) / "openclaw.json"


def default_workspace_dir_for_profile(profile: str) -> Path:
    return state_dir_for_profile(profile) / "workspace"


def runtime_meta_path_for_profile(profile: str) -> Path:
    return state_dir_for_profile(profile) / RUNTIME_META_NAME


def override_path_for_service(service_name: str) -> Path:
    return OPENCLAW_SYSTEMD_DIR / f"{service_name}.d" / "override.conf"


def bridge_token_path_for_profile(profile: str) -> Path:
    return OPENCLAW_BRIDGE_TOKEN_DIR / f"{profile}.token"


def read_bridge_token(profile: str) -> str | None:
    path = bridge_token_path_for_profile(profile)
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def ensure_bridge_token(profile: str) -> Path:
    path = bridge_token_path_for_profile(profile)
    OPENCLAW_BRIDGE_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_bridge_token(profile)
    if existing:
        return path
    path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def bridge_base_url() -> str:
    explicit = os.environ.get("OPENCLAW_DOCKER_BRIDGE_BASE_URL", "").strip()
    if explicit:
        return explicit
    return f"http://host.docker.internal:{settings.bridge_port}"


def build_host_control_bridge(profile: str) -> dict:
    token_path = bridge_token_path_for_profile(profile)
    return {
        "enabled": settings.docker_bridge_enabled,
        "listenHost": settings.bridge_host,
        "listenPort": settings.bridge_port,
        "baseUrl": bridge_base_url(),
        "tokenPath": str(token_path),
        "tokenExists": token_path.exists(),
    }


def read_runtime_meta(profile: str) -> dict | None:
    path = runtime_meta_path_for_profile(profile)
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def write_runtime_meta(profile: str, payload: dict) -> None:
    write_json(runtime_meta_path_for_profile(profile), payload)


def list_openclaw_configs() -> list[Path]:
    paths = [config_path_for_profile("default")]
    paths.extend(sorted(HOME.glob(".openclaw-*/openclaw.json")))
    return [path for path in paths if path.exists()]
