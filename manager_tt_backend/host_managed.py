from __future__ import annotations

from .config import OPENCLAW_RESERVED_PROFILES, normalize_profile
from .system import run_openclaw_instance_command


# Docker-first creation is intentionally not implemented yet.
# New instances still delegate to the host-managed openclaw-instance flow.
def create_instance_via_host_manager(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    if profile in OPENCLAW_RESERVED_PROFILES:
        raise ValueError("默认实例不能用 create，请使用 ensure")
    args = ["create", profile]
    port = payload.get("port")
    if port not in (None, ""):
        args.append(str(int(port)))
    return run_openclaw_instance_command(args)


def ensure_instance_via_host_manager(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    args = ["ensure"] if profile == "default" else ["ensure", profile]
    port = payload.get("port")
    if port not in (None, ""):
        args.append(str(int(port)))
    return run_openclaw_instance_command(args)
