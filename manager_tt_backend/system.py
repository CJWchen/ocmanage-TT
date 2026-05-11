from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .config import (
    DEFAULT_TIMEOUT_MS,
    HOME,
    MAX_TIMEOUT_MS,
    OPENCLAW_BIN,
    OPENCLAW_INSTANCE_BIN,
    config_path_for_profile,
    load_json,
)


def shell_join(parts: list[str]) -> str:
    return shlex.join(parts)


def run_shell(
    cmd: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout_ms = min(max(1000, int(timeout_ms)), MAX_TIMEOUT_MS)
    return subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True,
        text=True,
        timeout=timeout_ms / 1000,
        cwd=str(cwd or HOME),
        env={**os.environ, "HOME": str(HOME)},
    )


def run_bin_command(path: Path, args: list[str], timeout_ms: int = 45000, stdin_text: str | None = None) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"缺少可执行文件: {path}")
    result = subprocess.run(
        [str(path), *args],
        capture_output=True,
        text=True,
        input=stdin_text,
        timeout=timeout_ms / 1000,
        cwd=str(HOME),
        env={**os.environ, "HOME": str(HOME)},
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }


def run_openclaw_command(args: list[str], timeout_ms: int = 45000, stdin_text: str | None = None) -> dict:
    return run_bin_command(OPENCLAW_BIN, args, timeout_ms=timeout_ms, stdin_text=stdin_text)


def run_openclaw_instance_command(args: list[str], timeout_ms: int = 45000, stdin_text: str | None = None) -> dict:
    return run_bin_command(OPENCLAW_INSTANCE_BIN, args, timeout_ms=timeout_ms, stdin_text=stdin_text)


def file_mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


def read_systemd_show(service_name: str) -> dict[str, str]:
    cmd = (
        f"systemctl --user show {service_name}"
        " -p Id -p Description -p MainPID -p ActiveState -p SubState"
        " -p UnitFileState -p FragmentPath -p Environment"
    )
    result = run_shell(cmd, timeout_ms=12000)
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value
    return props


def read_service_logs(service_name: str, lines: int = 120) -> str:
    result = run_shell(
        f"journalctl --user -u {service_name} -n {max(1, min(lines, 400))} --no-pager",
        timeout_ms=12000,
    )
    return (result.stdout or result.stderr).strip()


def inspect_docker_runtime(runtime_meta: dict | None) -> dict | None:
    if not runtime_meta:
        return None
    container_name = runtime_meta.get("containerName")
    if not container_name:
        return None
    result = run_shell(
        f"docker inspect {shlex.quote(container_name)}",
        timeout_ms=12000,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {
            "containerName": container_name,
            "error": (result.stderr or result.stdout).strip(),
        }
    try:
        raw = json.loads(result.stdout)[0]
    except Exception as exc:
        return {
            "containerName": container_name,
            "error": str(exc),
        }
    state = raw.get("State", {})
    return {
        "containerName": container_name,
        "image": raw.get("Config", {}).get("Image"),
        "status": state.get("Status"),
        "running": state.get("Running"),
        "startedAt": state.get("StartedAt"),
    }


def read_config_port(profile: str) -> int | None:
    path = config_path_for_profile(profile)
    if not path.exists():
        return None
    try:
        config = load_json(path)
    except Exception:
        return None
    port = config.get("gateway", {}).get("port")
    return port if isinstance(port, int) else None


def list_port_owners(port: int | None) -> list[dict]:
    if not isinstance(port, int):
        return []
    result = run_shell("ss -ltnpH || true", timeout_ms=6000)
    owners = []
    for line in result.stdout.splitlines():
        parts = line.split()
        local_address = parts[3] if len(parts) >= 4 else ""
        if not local_address.endswith(f":{port}"):
            continue
        owners.append(
            {
                "raw": line,
                "localAddress": local_address,
                "peerAddress": parts[4] if len(parts) >= 5 else "",
                "process": parts[5] if len(parts) >= 6 else "",
            }
        )
    return owners


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}.manager-tt")
    backup.write_bytes(path.read_bytes())
    return backup


def read_text_if_exists(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def extract_port_from_unit_text(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"--port\s+(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def extract_profile_from_override(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"--profile\s+([A-Za-z0-9._-]+)", text)
    if not match:
        return "default" if "--unit openclaw-gateway.service" in text else None
    return match.group(1)


def override_uses_docker_runtime(text: str | None) -> bool:
    if not text:
        return False
    return "OPENCLAW_RUNTIME_MODE=docker" in text or "-docker-service run" in text


def docker_runtime_expected_port(runtime: dict) -> int | None:
    runtime_meta = runtime.get("runtimeMeta") or {}
    port = runtime_meta.get("port")
    return port if isinstance(port, int) else None
