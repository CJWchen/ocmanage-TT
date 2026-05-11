from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from .config import (
    HOME,
    OPENCLAW_HOME,
    OPENCLAW_SYSTEMD_DIR,
    OPENCLAW_RESERVED_PROFILES,
    bridge_base_url,
    bridge_token_path_for_profile,
    config_path_for_profile,
    ensure_bridge_token,
    list_openclaw_configs,
    load_json,
    normalize_profile,
    override_path_for_service,
    runtime_meta_path_for_profile,
    service_name_for_profile,
    settings,
    state_dir_for_profile,
    write_runtime_meta,
)
from .system import list_port_owners, run_openclaw_command, run_shell

DEFAULT_PORT = 18789
PORT_STEP = 1000
MIN_PORT_GAP = 120
MAX_PORT = 64789
OPENCLAW_IMAGE_REPO = "ghcr.io/openclaw/openclaw"
VERSION_PATTERN = re.compile(r"\b(\d+(?:\.\d+){2,}(?:[-A-Za-z0-9.]+)?)\b")


def docker_compose_dir_for_profile(profile: str) -> Path:
    return HOME / f".openclaw-{profile}-docker"


def docker_compose_path_for_profile(profile: str) -> Path:
    return docker_compose_dir_for_profile(profile) / "docker-compose.yml"


def docker_control_script_path_for_profile(profile: str) -> Path:
    return HOME / ".local" / "bin" / f"openclaw-{profile}-docker-service"


def docker_project_name_for_profile(profile: str) -> str:
    return f"openclaw-{profile}"


def docker_container_name_for_profile(profile: str) -> str:
    return f"openclaw-gateway-{profile}"


def docker_workspace_dir_for_profile(profile: str) -> Path:
    return OPENCLAW_HOME / ("workspace" if profile == "default" else f"workspace-{profile}")


def resolve_create_port(payload: dict) -> int:
    port = payload.get("port")
    if port in (None, ""):
        return find_next_available_gateway_port(read_configured_gateway_ports())

    value = int(port)
    if value <= 0 or value > 65535:
        raise ValueError(f"非法端口: {port}")
    ensure_port_available_for_create(value)
    return value


def read_configured_gateway_ports() -> list[int]:
    ports: list[int] = []
    for path in list_openclaw_configs():
        try:
            config = load_json(path)
        except Exception:
            continue
        port = config.get("gateway", {}).get("port")
        if isinstance(port, int) and port > 0:
            ports.append(port)
    return ports


def suggest_next_gateway_port(existing_ports: list[int]) -> int:
    candidate = DEFAULT_PORT
    while candidate <= MAX_PORT:
        if all(abs(candidate - port) >= MIN_PORT_GAP for port in existing_ports):
            return candidate
        candidate += PORT_STEP
    raise ValueError("未找到可用端口")


def find_next_available_gateway_port(existing_ports: list[int]) -> int:
    blocked_ports = list(existing_ports)
    while True:
        candidate = suggest_next_gateway_port(blocked_ports)
        if not list_port_owners(candidate):
            return candidate
        blocked_ports.append(candidate)


def _format_port_owner(owner: dict) -> str:
    local_address = str(owner.get("localAddress") or owner.get("raw") or "<unknown>")
    process = str(owner.get("process") or "").strip()
    if process:
        return f"{local_address} ({process})"
    return local_address


def ensure_port_available_for_create(port: int) -> None:
    owners = list_port_owners(port)
    if not owners:
        return

    snippets = [_format_port_owner(owner) for owner in owners[:3]]
    detail = "；".join(snippets)
    if len(owners) > 3:
        detail = f"{detail}；以及另外 {len(owners) - 3} 个监听"
    raise ValueError(f"端口 {port} 已被宿主机占用，请改用其他端口。当前监听: {detail}")


def build_docker_control_script_text(compose_dir: Path, project_name: str) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"STACK_DIR={compose_dir}",
            f"PROJECT_NAME={project_name}",
            'COMPOSE_FILE="$STACK_DIR/docker-compose.yml"',
            'COMPOSE=(docker compose --project-name "$PROJECT_NAME" -f "$COMPOSE_FILE")',
            "",
            'case "${1:-run}" in',
            "  run)",
            '    cd "$STACK_DIR"',
            '    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true',
            '    exec "${COMPOSE[@]}" up --remove-orphans',
            "    ;;",
            "  stop)",
            '    cd "$STACK_DIR"',
            '    exec "${COMPOSE[@]}" down --remove-orphans',
            "    ;;",
            "  ps)",
            '    cd "$STACK_DIR"',
            '    exec "${COMPOSE[@]}" ps',
            "    ;;",
            "  logs)",
            '    cd "$STACK_DIR"',
            '    exec "${COMPOSE[@]}" logs --tail=200',
            "    ;;",
            "  pull)",
            '    cd "$STACK_DIR"',
            '    exec "${COMPOSE[@]}" pull',
            "    ;;",
            "  *)",
            '    echo "usage: $(basename "$0") [run|stop|ps|logs|pull]" >&2',
            "    exit 2",
            "    ;;",
            "esac",
            "",
        ]
    )


def build_openclaw_host_bridge_script_text(profile: str) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "from __future__ import annotations",
            "",
            "import argparse",
            "import json",
            "import os",
            "import urllib.error",
            "import urllib.parse",
            "import urllib.request",
            "from pathlib import Path",
            "",
            f"DEFAULT_PROFILE = {profile!r}",
            'DEFAULT_BASE_URL = os.environ.get("OPENCLAW_HOST_BRIDGE_BASE_URL", "http://host.docker.internal:58081")',
            'DEFAULT_TOKEN_PATH = Path(os.environ.get("OPENCLAW_HOST_BRIDGE_TOKEN_PATH", "/run/openclaw-host-bridge/token"))',
            "",
            "",
            "def load_token(path: Path) -> str:",
            '    token = path.read_text(encoding="utf-8").strip()',
            "    if not token:",
            '        raise SystemExit(f"empty bridge token: {path}")',
            "    return token",
            "",
            "",
            "def request_json(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:",
            "    body = None",
            '    headers = {"Accept": "application/json", "X-OpenClaw-Bridge-Token": token}',
            "    if payload is not None:",
            '        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")',
            '        headers["Content-Type"] = "application/json"',
            "    request = urllib.request.Request(url, data=body, headers=headers, method=method)",
            "    try:",
            "        with urllib.request.urlopen(request, timeout=30) as response:",
            '            raw = response.read().decode("utf-8") or "{}"',
            "            return response.status, json.loads(raw)",
            "    except urllib.error.HTTPError as exc:",
            '        raw = exc.read().decode("utf-8", errors="replace")',
            "        try:",
            '            payload = json.loads(raw or "{}")',
            "        except json.JSONDecodeError:",
            '            payload = {"error": raw}',
            "        return exc.code, payload",
            "",
            "",
            "def main() -> int:",
            '    parser = argparse.ArgumentParser(description="Call the host OpenClaw control bridge from inside a dockerized profile.")',
            '    parser.add_argument("action", choices=["status", "start", "stop", "restart", "doctor-repair"])',
            '    parser.add_argument("--profile", default=os.environ.get("OPENCLAW_HOST_BRIDGE_PROFILE", DEFAULT_PROFILE))',
            '    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)',
            '    parser.add_argument("--token-path", default=str(DEFAULT_TOKEN_PATH))',
            "    args = parser.parse_args()",
            "",
            "    token = load_token(Path(args.token_path))",
            '    if args.action == "status":',
            '        query = urllib.parse.urlencode({"profile": args.profile})',
            '        code, payload = request_json("GET", f"{args.base_url}/api/openclaw/bridge/status?{query}", token)',
            "    else:",
            "        code, payload = request_json(",
            '            "POST",',
            '            f"{args.base_url}/api/openclaw/bridge/action",',
            "            token,",
            '            {"profile": args.profile, "action": args.action},',
            "        )",
            "",
            "    print(json.dumps(payload, ensure_ascii=False, indent=2))",
            "    return 0 if 200 <= code < 300 else 1",
            "",
            "",
            'if __name__ == "__main__":',
            "    raise SystemExit(main())",
            "",
        ]
    )


def build_docker_compose_text(
    *,
    profile: str,
    image: str,
    port: int,
    state_dir: Path,
    workspace_dir: Path,
    bridge_token_path: Path,
) -> str:
    bridge_url = bridge_base_url()
    return "\n".join(
        [
            "services:",
            "  openclaw-gateway:",
            f"    image: {image}",
            f"    container_name: {docker_container_name_for_profile(profile)}",
            "    init: true",
            "    ports:",
            f'      - "127.0.0.1:{port}:{port}"',
            "    healthcheck:",
            "      test:",
            "        - CMD-SHELL",
            f'        - node -e "fetch(\'http://127.0.0.1:{port}/healthz\').then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"',
            "      interval: 3m",
            "      timeout: 10s",
            "      start_period: 15s",
            "      retries: 3",
            "    environment:",
            "      HOME: /home/node",
            "      OPENCLAW_STATE_DIR: /home/node/.openclaw",
            "      OPENCLAW_CONFIG_PATH: /home/node/.openclaw/openclaw.json",
            '      OPENCLAW_DISABLE_BONJOUR: "1"',
            f"      OPENCLAW_HOST_BRIDGE_BASE_URL: {bridge_url}",
            f"      OPENCLAW_HOST_BRIDGE_PROFILE: {profile}",
            "      OPENCLAW_HOST_BRIDGE_TOKEN_PATH: /run/openclaw-host-bridge/token",
            "    volumes:",
            f"      - {state_dir}:/home/node/.openclaw",
            f"      - {bridge_token_path}:/run/openclaw-host-bridge/token:ro",
            f"      - {workspace_dir}:{workspace_dir}",
            "    extra_hosts:",
            '      - "host.docker.internal:host-gateway"',
            "    security_opt:",
            "      - no-new-privileges:true",
            "    cap_drop:",
            "      - NET_RAW",
            "      - NET_ADMIN",
            "    command:",
            "      - node",
            "      - openclaw.mjs",
            "      - gateway",
            "      - --allow-unconfigured",
            "      - --bind",
            "      - lan",
            "      - --port",
            f'      - "{port}"',
            "",
        ]
    )


def build_docker_override_text(profile: str, runtime_meta: dict) -> str:
    compose_dir = runtime_meta.get("composeDir")
    control_script = runtime_meta.get("controlScriptPath")
    service_name = service_name_for_profile(profile)
    if not compose_dir or not control_script:
        raise ValueError(f"{profile} 缺少 Docker runtime meta，无法重建 override")
    return "\n".join(
        [
            "[Service]",
            f"WorkingDirectory={compose_dir}",
            "ExecStart=",
            f"ExecStart={control_script} run",
            "ExecStop=",
            f"ExecStop={control_script} stop",
            "Environment=OPENCLAW_GATEWAY_PORT=",
            f"Environment=OPENCLAW_SYSTEMD_UNIT={service_name}",
            "Environment=OPENCLAW_RUNTIME_MODE=docker",
            "",
        ]
    )


def build_docker_runtime_meta(
    *,
    profile: str,
    image: str,
    port: int,
    compose_dir: Path,
    control_script_path: Path,
    workspace_dir: Path,
    bridge_token_path: Path,
    bridge_tool_path: Path,
) -> dict:
    return {
        "runtimeMode": "docker",
        "profile": profile,
        "serviceName": service_name_for_profile(profile),
        "containerName": docker_container_name_for_profile(profile),
        "projectName": docker_project_name_for_profile(profile),
        "composeDir": str(compose_dir),
        "composePath": str(compose_dir / "docker-compose.yml"),
        "controlScriptPath": str(control_script_path),
        "image": image,
        "port": port,
        "workspaceMode": "host-path-preserved",
        "workspaceHostPath": str(workspace_dir),
        "workspaceContainerPath": str(workspace_dir),
        "healthzUrl": f"http://127.0.0.1:{port}/healthz",
        "rollbackOverridePath": str(compose_dir / "backups" / "override.conf.pre-docker"),
        "hostControlBridge": {
            "baseUrl": bridge_base_url(),
            "port": settings.bridge_port,
            "tokenPath": str(bridge_token_path),
            "toolPath": str(bridge_tool_path),
        },
    }


def resolve_openclaw_image() -> str:
    result = run_openclaw_command(["--version"], timeout_ms=10000)
    text = ((result.get("stdout") or "") + "\n" + (result.get("stderr") or "")).strip()
    if int(result.get("returncode", 1)) != 0:
        raise RuntimeError(f"读取 OpenClaw 版本失败: {text or 'unknown error'}")

    match = VERSION_PATTERN.search(text)
    if not match:
        raise RuntimeError(f"无法从版本输出中解析 tag: {text or '<empty>'}")
    return f"{OPENCLAW_IMAGE_REPO}:{match.group(1)}"


def profile_openclaw_args(profile: str, args: list[str]) -> list[str]:
    if profile == "default":
        return args
    return ["--profile", profile, *args]


def require_success(result: dict, step: str) -> None:
    if int(result.get("returncode", 1)) == 0:
        return
    detail = (result.get("stderr") or result.get("stdout") or "").strip()
    raise RuntimeError(f"{step}失败: {detail or 'unknown error'}")


def _remove_file_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def _remove_tree_if_exists(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _remove_empty_dir_if_exists(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _command_output(result) -> str:
    return "\n".join(part for part in ((result.stdout or "").strip(), (result.stderr or "").strip()) if part).strip()


def _docker_resource_missing(detail: str) -> bool:
    lowered = detail.lower()
    return "no such" in lowered or "not found" in lowered


def _docker_resource_exists(resource: str, name: str) -> bool:
    result = run_shell(f"docker {resource} inspect {shlex.quote(name)}", timeout_ms=15000)
    if result.returncode == 0:
        return True

    detail = _command_output(result)
    if _docker_resource_missing(detail):
        return False
    raise RuntimeError(f"检查 Docker {resource} {name} 失败: {detail or 'unknown error'}")


def _list_docker_project_networks(project_name: str) -> list[str]:
    result = run_shell(
        f"docker network ls --filter label=com.docker.compose.project={shlex.quote(project_name)} --format '{{{{.Name}}}}'",
        timeout_ms=15000,
    )
    if result.returncode != 0:
        detail = _command_output(result)
        raise RuntimeError(f"列出 Docker 网络失败: {detail or 'unknown error'}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def teardown_docker_runtime(profile: str, runtime_meta: dict | None = None) -> dict:
    runtime_meta = runtime_meta if isinstance(runtime_meta, dict) else {}
    compose_path_value = runtime_meta.get("composePath")
    compose_path = (
        Path(compose_path_value)
        if isinstance(compose_path_value, str) and compose_path_value
        else docker_compose_path_for_profile(profile)
    )
    project_name = runtime_meta.get("projectName") if isinstance(runtime_meta.get("projectName"), str) else None
    container_name = runtime_meta.get("containerName") if isinstance(runtime_meta.get("containerName"), str) else None
    project_name = project_name or docker_project_name_for_profile(profile)
    container_name = container_name or docker_container_name_for_profile(profile)

    actions: list[str] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    if compose_path.exists():
        result = run_shell(
            f"docker compose --project-name {shlex.quote(project_name)} -f {shlex.quote(str(compose_path))} down --remove-orphans",
            timeout_ms=45000,
            cwd=compose_path.parent,
        )
        stdout_parts.append((result.stdout or "").strip())
        stderr_parts.append((result.stderr or "").strip())
        if result.returncode != 0:
            detail = _command_output(result)
            raise RuntimeError(f"停止 Docker compose 项目 {project_name} 失败: {detail or 'unknown error'}")
        actions.append(f"已执行 docker compose down: {project_name}")
        return {
            "stdout": "\n".join(part for part in stdout_parts if part),
            "stderr": "\n".join(part for part in stderr_parts if part),
            "actions": actions,
        }

    actions.append(f"compose 文件缺失，改用容器/网络兜底清理: {project_name}")
    if _docker_resource_exists("container", container_name):
        container_result = run_shell(f"docker rm -f {shlex.quote(container_name)}", timeout_ms=30000)
        stdout_parts.append((container_result.stdout or "").strip())
        stderr_parts.append((container_result.stderr or "").strip())
        if container_result.returncode != 0:
            detail = _command_output(container_result)
            raise RuntimeError(f"删除 Docker 容器 {container_name} 失败: {detail or 'unknown error'}")
        actions.append(f"已删除 Docker 容器: {container_name}")
    else:
        actions.append(f"Docker 容器已不存在: {container_name}")

    network_names = _list_docker_project_networks(project_name)
    if not network_names:
        actions.append(f"未发现 compose 项目网络: {project_name}")
    for network_name in network_names:
        if not _docker_resource_exists("network", network_name):
            actions.append(f"Docker 网络已不存在: {network_name}")
            continue
        network_result = run_shell(f"docker network rm {shlex.quote(network_name)}", timeout_ms=30000)
        stdout_parts.append((network_result.stdout or "").strip())
        stderr_parts.append((network_result.stderr or "").strip())
        if network_result.returncode != 0:
            detail = _command_output(network_result)
            raise RuntimeError(f"删除 Docker 网络 {network_name} 失败: {detail or 'unknown error'}")
        actions.append(f"已删除 Docker 网络: {network_name}")

    return {
        "stdout": "\n".join(part for part in stdout_parts if part),
        "stderr": "\n".join(part for part in stderr_parts if part),
        "actions": actions,
    }


def rollback_failed_docker_create(snapshot: dict, service_name: str) -> list[str]:
    errors: list[str] = []

    def attempt(step: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            errors.append(f"{step}: {exc}")

    service_path = snapshot["servicePath"]
    override_path = snapshot["overridePath"]
    rollback_override_path = snapshot["rollbackOverridePath"]
    service_was_new = not snapshot["servicePathExisted"]
    override_was_new = not snapshot["overridePathExisted"]

    if service_was_new or override_was_new:
        attempt(
            "disable_service",
            lambda: run_shell(f"systemctl --user disable --now {service_name} >/dev/null 2>&1 || true", timeout_ms=20000),
        )

    if snapshot["overridePathExisted"] and rollback_override_path.exists():
        attempt(
            "restore_override",
            lambda: (
                override_path.parent.mkdir(parents=True, exist_ok=True),
                override_path.write_bytes(rollback_override_path.read_bytes()),
            ),
        )
    elif override_was_new:
        attempt("remove_override", lambda: _remove_file_if_exists(override_path))

    if service_was_new:
        attempt("remove_service", lambda: _remove_file_if_exists(service_path))

    if snapshot["composeDirExisted"]:
        attempt("remove_override_backup", lambda: _remove_file_if_exists(rollback_override_path))
    else:
        attempt("remove_compose_dir", lambda: _remove_tree_if_exists(snapshot["composeDir"]))

    if snapshot["controlScriptExisted"] is False:
        attempt("remove_control_script", lambda: _remove_file_if_exists(snapshot["controlScriptPath"]))

    if snapshot["bridgeToolExisted"] is False:
        attempt("remove_bridge_tool", lambda: _remove_file_if_exists(snapshot["bridgeToolPath"]))

    if snapshot["runtimeMetaExisted"] is False:
        attempt("remove_runtime_meta", lambda: _remove_file_if_exists(snapshot["runtimeMetaPath"]))

    if snapshot["configPathExisted"] is False:
        attempt("remove_config", lambda: _remove_file_if_exists(snapshot["configPath"]))

    if snapshot["stateDirExisted"]:
        attempt("cleanup_state_tools_dir", lambda: _remove_empty_dir_if_exists(snapshot["bridgeToolPath"].parent))
    else:
        attempt("remove_state_dir", lambda: _remove_tree_if_exists(snapshot["stateDir"]))

    if snapshot["workspaceDirExisted"] is False:
        attempt("remove_workspace_dir", lambda: _remove_tree_if_exists(snapshot["workspaceDir"]))

    if snapshot["bridgeTokenExisted"] is False:
        attempt("remove_bridge_token", lambda: _remove_file_if_exists(snapshot["bridgeTokenPath"]))

    if service_was_new or override_was_new or snapshot["overridePathExisted"]:
        attempt("daemon_reload", lambda: run_shell("systemctl --user daemon-reload", timeout_ms=20000))

    return errors


def prepare_profile_config(profile: str, port: int, workspace_dir: Path) -> dict:
    state_dir = state_dir_for_profile(profile)
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "agents": {
            "defaults": {
                "workspace": str(workspace_dir),
            }
        },
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": port,
            "controlUi": {
                "allowedOrigins": [
                    f"http://localhost:{port}",
                    f"http://127.0.0.1:{port}",
                ]
            },
        },
    }
    return run_openclaw_command(
        profile_openclaw_args(profile, ["config", "patch", "--stdin"]),
        timeout_ms=30000,
        stdin_text=json.dumps(payload, ensure_ascii=False),
    )


def install_base_service(profile: str, port: int) -> dict:
    return run_openclaw_command(
        profile_openclaw_args(profile, ["gateway", "install", "--force", "--port", str(port)]),
        timeout_ms=45000,
    )


def create_instance_via_docker_manager(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    if profile in OPENCLAW_RESERVED_PROFILES:
        raise ValueError("默认实例不能用 create，请使用 ensure")

    config_path = config_path_for_profile(profile)
    if config_path.exists():
        raise ValueError(f"profile '{profile}' 已存在: {config_path}")

    port = resolve_create_port(payload)
    image = resolve_openclaw_image()
    state_dir = state_dir_for_profile(profile)
    compose_dir = docker_compose_dir_for_profile(profile)
    compose_path = compose_dir / "docker-compose.yml"
    compose_backup_dir = compose_dir / "backups"
    control_script_path = docker_control_script_path_for_profile(profile)
    workspace_dir = docker_workspace_dir_for_profile(profile)
    bridge_token_path = bridge_token_path_for_profile(profile)
    bridge_token_existed = bridge_token_path.exists()
    bridge_token_path = ensure_bridge_token(profile)
    bridge_tool_path = state_dir / "tools" / "openclaw_host_bridge.py"
    runtime_meta = build_docker_runtime_meta(
        profile=profile,
        image=image,
        port=port,
        compose_dir=compose_dir,
        control_script_path=control_script_path,
        workspace_dir=workspace_dir,
        bridge_token_path=bridge_token_path,
        bridge_tool_path=bridge_tool_path,
    )
    service_name = service_name_for_profile(profile)
    override_path = override_path_for_service(service_name)
    service_path = OPENCLAW_SYSTEMD_DIR / service_name
    runtime_meta_path = runtime_meta_path_for_profile(profile)
    rollback_override_path = compose_backup_dir / "override.conf.pre-docker"
    snapshot = {
        "configPath": config_path,
        "configPathExisted": config_path.exists(),
        "stateDir": state_dir,
        "stateDirExisted": state_dir.exists(),
        "workspaceDir": workspace_dir,
        "workspaceDirExisted": workspace_dir.exists(),
        "composeDir": compose_dir,
        "composeDirExisted": compose_dir.exists(),
        "controlScriptPath": control_script_path,
        "controlScriptExisted": control_script_path.exists(),
        "bridgeToolPath": bridge_tool_path,
        "bridgeToolExisted": bridge_tool_path.exists(),
        "runtimeMetaPath": runtime_meta_path,
        "runtimeMetaExisted": runtime_meta_path.exists(),
        "bridgeTokenPath": bridge_token_path,
        "bridgeTokenExisted": bridge_token_existed,
        "servicePath": service_path,
        "servicePathExisted": service_path.exists(),
        "overridePath": override_path,
        "overridePathExisted": override_path.exists(),
        "rollbackOverridePath": rollback_override_path,
    }

    actions: list[str] = []
    try:
        config_result = prepare_profile_config(profile, port, workspace_dir)
        require_success(config_result, "写入配置")
        actions.append(f"已初始化 {profile} 配置")

        install_result = install_base_service(profile, port)
        require_success(install_result, "安装 systemd service")
        actions.append(f"已安装 {service_name} 基础 service")

        compose_backup_dir.mkdir(parents=True, exist_ok=True)
        compose_dir.mkdir(parents=True, exist_ok=True)
        control_script_path.parent.mkdir(parents=True, exist_ok=True)
        bridge_tool_path.parent.mkdir(parents=True, exist_ok=True)

        if override_path.exists():
            rollback_override_path.write_bytes(override_path.read_bytes())

        compose_path.write_text(
            build_docker_compose_text(
                profile=profile,
                image=image,
                port=port,
                state_dir=state_dir,
                workspace_dir=workspace_dir,
                bridge_token_path=bridge_token_path,
            ),
            encoding="utf-8",
        )
        actions.append(f"已写入 compose: {compose_path}")

        control_script_path.write_text(
            build_docker_control_script_text(compose_dir, docker_project_name_for_profile(profile)),
            encoding="utf-8",
        )
        control_script_path.chmod(0o755)
        actions.append(f"已写入控制脚本: {control_script_path}")

        bridge_tool_path.write_text(build_openclaw_host_bridge_script_text(profile), encoding="utf-8")
        bridge_tool_path.chmod(0o755)
        actions.append(f"已写入宿主机控制桥脚本: {bridge_tool_path}")

        write_runtime_meta(profile, runtime_meta)
        actions.append("已写入 Docker runtime meta")

        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(build_docker_override_text(profile, runtime_meta), encoding="utf-8")
        actions.append("已切换 systemd override 到 Docker 控制脚本")

        daemon_reload = run_shell("systemctl --user daemon-reload", timeout_ms=20000)
        if daemon_reload.returncode != 0:
            raise RuntimeError((daemon_reload.stderr or daemon_reload.stdout).strip() or "daemon-reload 失败")
        actions.append("已执行 daemon-reload")

        enable_result = run_shell(f"systemctl --user enable {service_name}", timeout_ms=20000)
        if enable_result.returncode != 0:
            raise RuntimeError((enable_result.stderr or enable_result.stdout).strip() or f"enable {service_name} 失败")
        actions.append(f"已启用 {service_name}")

        start_result = run_shell(f"systemctl --user start {service_name}", timeout_ms=45000)
        if start_result.returncode != 0:
            raise RuntimeError((start_result.stderr or start_result.stdout).strip() or f"启动 {service_name} 失败")
        actions.append(f"已启动 {service_name}")

        stdout_parts = [
            (config_result.get("stdout") or "").strip(),
            (install_result.get("stdout") or "").strip(),
            (enable_result.stdout or "").strip(),
            (start_result.stdout or "").strip(),
        ]
        stderr_parts = [
            (config_result.get("stderr") or "").strip(),
            (install_result.get("stderr") or "").strip(),
            (enable_result.stderr or "").strip(),
            (start_result.stderr or "").strip(),
        ]
        return {
            "stdout": "\n".join(part for part in stdout_parts if part),
            "stderr": "\n".join(part for part in stderr_parts if part),
            "returncode": 0,
            "actions": actions,
            "profile": profile,
            "runtimeMode": "docker",
        }
    except Exception as exc:
        rollback_errors = rollback_failed_docker_create(snapshot, service_name)
        if rollback_errors:
            raise RuntimeError(f"{exc}；回滚不完整: {'; '.join(rollback_errors)}") from exc
        raise
