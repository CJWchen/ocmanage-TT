from __future__ import annotations

import hmac
import json
import re
import datetime as dt
from pathlib import Path

from .config import (
    CONTAINER_STATE_DIR,
    CONTAINER_WORKSPACE_DIR,
    MANAGER_AUDIT_LOG,
    MANAGER_ROOT,
    OPENCLAW_BIN,
    OPENCLAW_INSTANCE_BIN,
    append_jsonl,
    build_host_control_bridge,
    config_path_for_profile,
    default_workspace_dir_for_profile,
    list_openclaw_configs,
    load_json,
    normalize_profile,
    override_path_for_service,
    read_bridge_token,
    read_runtime_meta,
    service_name_for_profile,
    settings,
    state_dir_for_profile,
    utc_now_iso,
)
from .create_modes import normalize_runtime_mode
from .system import (
    docker_runtime_expected_port,
    extract_port_from_unit_text,
    extract_profile_from_override,
    file_mtime_iso,
    inspect_docker_runtime,
    list_port_owners,
    override_uses_docker_runtime,
    read_config_port,
    read_systemd_show,
    read_text_if_exists,
    shell_join,
)

SESSION_PATH_FIELDS = (
    "workspaceDir",
    "cwd",
    "path",
    "filePath",
    "baseDir",
    "rootDir",
    "configPath",
    "sessionFile",
)
SESSION_PATH_VALUE_PATTERN = re.compile(
    rf'"(?P<field>{"|".join(SESSION_PATH_FIELDS)})"\s*:\s*"(?P<path>(?:\\.|[^"])*)"'
)
DOCKER_BIND_MOUNT_PATTERN = re.compile(
    r"^\s*-\s*(?P<host>/[^:\s]+):(?P<container>/[^:\s]+)(?::[^#\s]+)?\s*$"
)


def parse_feishu_channel(config: dict) -> dict:
    feishu = config.get("channels", {}).get("feishu", {})
    secret_provider = feishu.get("appSecret") if isinstance(feishu.get("appSecret"), dict) else None
    return {
        "enabled": bool(feishu.get("enabled")),
        "appId": feishu.get("appId"),
        "usesSecretProvider": bool(secret_provider),
        "domain": feishu.get("domain"),
        "connectionMode": feishu.get("connectionMode"),
        "dmPolicy": feishu.get("dmPolicy"),
        "groupPolicy": feishu.get("groupPolicy"),
        "requireMention": feishu.get("requireMention"),
    }


def extract_config_summary(profile: str, path: Path, config: dict) -> dict:
    gateway = config.get("gateway", {})
    defaults = config.get("agents", {}).get("defaults", {})
    models = defaults.get("model", {})
    tools = config.get("tools", {})
    return {
        "profile": profile,
        "configPath": str(path),
        "stateDir": str(path.parent),
        "port": gateway.get("port"),
        "bind": gateway.get("bind"),
        "workspace": defaults.get("workspace"),
        "primaryModel": models.get("primary"),
        "toolProfile": tools.get("profile"),
        "metaVersion": config.get("meta", {}).get("lastTouchedVersion"),
        "metaTouchedAt": config.get("meta", {}).get("lastTouchedAt"),
        "browserEnabled": config.get("browser", {}).get("enabled"),
        "feishu": parse_feishu_channel(config),
    }


def translate_container_state_path_to_host(profile: str, path: str) -> str:
    candidate = path.strip()
    if not candidate.startswith(CONTAINER_STATE_DIR):
        return candidate
    relative = candidate.removeprefix(CONTAINER_STATE_DIR).lstrip("/")
    host_state_dir = state_dir_for_profile(profile)
    return str(host_state_dir / relative) if relative else str(host_state_dir)


def resolve_workspace_path(profile: str, workspace: str | None, runtime: dict | None = None) -> str:
    runtime_meta = (runtime or {}).get("runtimeMeta") or {}
    runtime_mode = (runtime or {}).get("runtimeMode")
    workspace_host_path = runtime_meta.get("workspaceHostPath")
    if runtime_mode == "docker" and isinstance(workspace_host_path, str) and workspace_host_path.startswith("/"):
        return workspace_host_path

    candidate = (workspace or "").strip() if isinstance(workspace, str) else ""
    if not candidate:
        return str(default_workspace_dir_for_profile(profile))
    if candidate.startswith(CONTAINER_STATE_DIR):
        return translate_container_state_path_to_host(profile, candidate)
    return candidate


def build_host_visible_paths(profile: str, summary: dict, runtime: dict) -> dict:
    runtime_meta = runtime.get("runtimeMeta") or {}
    paths = {
        "stateDir": str(state_dir_for_profile(profile)),
        "workspaceDir": resolve_workspace_path(profile, summary.get("workspace"), runtime),
        "configPath": str(config_path_for_profile(profile)),
        "serviceName": runtime["serviceName"],
        "overridePath": runtime.get("overridePath"),
    }
    if runtime_meta.get("composeDir"):
        paths["composeDir"] = runtime_meta["composeDir"]
    if runtime_meta.get("controlScriptPath"):
        paths["controlScriptPath"] = runtime_meta["controlScriptPath"]
    return paths


def normalize_absolute_path(path: str | None) -> str | None:
    if not isinstance(path, str):
        return None
    candidate = path.strip()
    if not candidate.startswith("/"):
        return None
    if candidate != "/":
        candidate = candidate.rstrip("/")
    return candidate or "/"


def path_is_within_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def read_compose_bind_mounts(runtime_meta: dict) -> list[tuple[str, str]]:
    compose_path_value = runtime_meta.get("composePath")
    compose_path = Path(compose_path_value) if isinstance(compose_path_value, str) and compose_path_value else None
    if not compose_path or not compose_path.exists():
        return []
    try:
        lines = compose_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    mounts: list[tuple[str, str]] = []
    for line in lines:
        match = DOCKER_BIND_MOUNT_PATTERN.match(line)
        if not match:
            continue
        host_path = normalize_absolute_path(match.group("host"))
        container_path = normalize_absolute_path(match.group("container"))
        if host_path and container_path:
            mounts.append((host_path, container_path))
    return mounts


def collect_docker_path_mappings(profile: str, summary: dict | None = None, runtime: dict | None = None) -> list[dict]:
    runtime_meta = (runtime or {}).get("runtimeMeta") or {}
    workspace = summary.get("workspace") if summary else None
    host_state_dir = normalize_absolute_path(str(state_dir_for_profile(profile)))
    host_workspace_dir = normalize_absolute_path(runtime_meta.get("workspaceHostPath")) or normalize_absolute_path(
        resolve_workspace_path(profile, workspace, runtime)
    )
    container_workspace_dir = normalize_absolute_path(runtime_meta.get("workspaceContainerPath")) or normalize_absolute_path(
        CONTAINER_WORKSPACE_DIR
    )

    mappings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add_mapping(host_path: str | None, container_path: str | None, source: str) -> None:
        if not host_path or not container_path:
            return
        key = (host_path, container_path)
        if key in seen:
            return
        seen.add(key)
        mappings.append(
            {
                "hostPath": host_path,
                "containerPath": container_path,
                "source": source,
            }
        )

    add_mapping(host_state_dir, normalize_absolute_path(CONTAINER_STATE_DIR), "state-dir-default")
    add_mapping(host_workspace_dir, container_workspace_dir, "workspace-default")
    for host_path, container_path in read_compose_bind_mounts(runtime_meta):
        add_mapping(host_path, container_path, "compose")
    return mappings


def build_path_translation(profile: str, summary: dict | None = None, runtime: dict | None = None) -> dict:
    host_state_dir = state_dir_for_profile(profile)
    runtime_meta = (runtime or {}).get("runtimeMeta") or {}
    host_workspace_dir = runtime_meta.get("workspaceHostPath")
    container_workspace_dir = runtime_meta.get("workspaceContainerPath")
    if not isinstance(host_workspace_dir, str) or not host_workspace_dir.startswith("/"):
        workspace = summary.get("workspace") if summary else None
        host_workspace_dir = resolve_workspace_path(profile, workspace, runtime)
    if not isinstance(container_workspace_dir, str) or not container_workspace_dir.startswith("/"):
        container_workspace_dir = CONTAINER_WORKSPACE_DIR
    mappings = collect_docker_path_mappings(profile, summary, runtime)
    state_aliases = [item["containerPath"] for item in mappings if item["hostPath"] == str(host_state_dir)]
    workspace_aliases = [item["containerPath"] for item in mappings if item["hostPath"] == host_workspace_dir]
    return {
        "hostStateDir": str(host_state_dir),
        "containerStateDir": CONTAINER_STATE_DIR,
        "hostWorkspaceDir": host_workspace_dir,
        "containerWorkspaceDir": container_workspace_dir,
        "containerStateAliases": state_aliases or [CONTAINER_STATE_DIR],
        "containerWorkspaceAliases": workspace_aliases or [container_workspace_dir],
        "bindMounts": mappings,
    }


def build_human_runtime_rules(profile: str, runtime: dict) -> list[str]:
    service_name = runtime["serviceName"]
    rules = [
        f"对人类沟通时一律使用 service 名称 {service_name}，不要暴露容器内部名词。",
        "对人类展示路径时一律使用宿主机路径，不要直接说 /home/node/.openclaw/...。",
    ]
    if runtime.get("runtimeMode") == "docker":
        rules.append("重启或修复时优先走宿主机控制桥，不要让人类手动执行 docker restart。")
    return rules


def collect_runtime_info(profile: str) -> dict:
    service_name = service_name_for_profile(profile)
    props = read_systemd_show(service_name)
    port = read_config_port(profile)
    port_owners = list_port_owners(port) if isinstance(port, int) else []
    fragment_path = Path(props["FragmentPath"]) if props.get("FragmentPath") else None
    runtime_override_path = override_path_for_service(service_name)
    runtime_meta = read_runtime_meta(profile)
    docker_runtime = inspect_docker_runtime(runtime_meta) if runtime_meta else None
    return {
        "serviceName": service_name,
        "mainPid": int(props.get("MainPID", "0") or "0"),
        "activeState": props.get("ActiveState", "unknown"),
        "subState": props.get("SubState", "unknown"),
        "unitFileState": props.get("UnitFileState", "unknown"),
        "description": props.get("Description"),
        "fragmentPath": props.get("FragmentPath"),
        "environment": props.get("Environment", ""),
        "overridePath": str(runtime_override_path),
        "serviceFileMtime": file_mtime_iso(fragment_path) if fragment_path else None,
        "overrideFileMtime": file_mtime_iso(runtime_override_path),
        "portOwners": port_owners,
        "serviceFileExists": bool(fragment_path and fragment_path.exists()),
        "overrideFileExists": runtime_override_path.exists(),
        "runtimeMode": normalize_runtime_mode((runtime_meta or {}).get("runtimeMode")),
        "runtimeMeta": runtime_meta,
        "dockerRuntime": docker_runtime,
        "hostControlBridge": (
            build_host_control_bridge(profile)
            if normalize_runtime_mode((runtime_meta or {}).get("runtimeMode")) == "docker"
            else None
        ),
    }


def find_docker_session_host_path_refs(profile: str, summary: dict | None = None, runtime: dict | None = None) -> list[str]:
    sessions_dir = state_dir_for_profile(profile) / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return []
    translation = build_path_translation(profile, summary, runtime)
    mappings = translation.get("bindMounts") or []
    translated_roots = sorted(
        {
            item["hostPath"]
            for item in mappings
            if item.get("hostPath") != item.get("containerPath")
            and isinstance(item.get("hostPath"), str)
        },
        key=len,
        reverse=True,
    )
    preserved_roots = sorted(
        {
            item["hostPath"]
            for item in mappings
            if item.get("hostPath") == item.get("containerPath")
            and isinstance(item.get("hostPath"), str)
        },
        key=len,
        reverse=True,
    )
    if not translated_roots:
        return []
    hits: list[str] = []
    for path in sorted(sessions_dir.glob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            data = path.read_text(encoding="utf-8")
        except Exception:
            continue
        path_values: list[str] = []
        for match in SESSION_PATH_VALUE_PATTERN.finditer(data):
            raw_path = match.group("path")
            try:
                value = json.loads(f'"{raw_path}"')
            except json.JSONDecodeError:
                value = raw_path
            normalized = normalize_absolute_path(value if isinstance(value, str) else None)
            if normalized:
                path_values.append(normalized)
        if any(
            any(path_is_within_root(value, root) for root in translated_roots)
            and not any(path_is_within_root(value, root) for root in preserved_roots)
            for value in path_values
        ):
            hits.append(str(path))
    return hits


def reset_docker_sessions(profile: str) -> str | None:
    sessions_dir = state_dir_for_profile(profile) / "agents" / "main" / "sessions"
    if not sessions_dir.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = state_dir_for_profile(profile) / ".doctor-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    for legacy_dir in sorted(sessions_dir.parent.glob("sessions.bak.*.docker-reset")):
        legacy_target = backup_root / legacy_dir.name
        if legacy_target.exists():
            continue
        legacy_dir.rename(legacy_target)
    backup_dir = backup_root / f"sessions-main.{stamp}.docker-reset"
    sessions_dir.rename(backup_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return str(backup_dir)


def relocate_legacy_docker_session_backups(profile: str) -> list[str]:
    sessions_parent = state_dir_for_profile(profile) / "agents" / "main"
    backup_root = state_dir_for_profile(profile) / ".doctor-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for legacy_dir in sorted(sessions_parent.glob("sessions.bak.*.docker-reset")):
        legacy_target = backup_root / legacy_dir.name
        if legacy_target.exists():
            continue
        legacy_dir.rename(legacy_target)
        moved.append(str(legacy_target))
    return moved


def build_bridge_status(profile: str) -> dict:
    detail = read_instance(profile)
    summary = detail["summary"]
    runtime = detail["runtime"]
    port = summary.get("port")
    response = {
        "generatedAt": utc_now_iso(),
        "profile": profile,
        "serviceName": runtime["serviceName"],
        "runtimeMode": runtime.get("runtimeMode", "systemd"),
        "service": {
            "activeState": runtime.get("activeState"),
            "subState": runtime.get("subState"),
            "mainPid": runtime.get("mainPid"),
        },
        "gateway": {
            "port": port,
            "bind": summary.get("bind"),
            "healthzUrl": f"http://127.0.0.1:{port}/healthz" if isinstance(port, int) else None,
        },
        "hostPaths": build_host_visible_paths(profile, summary, runtime),
        "humanRules": build_human_runtime_rules(profile, runtime),
        "checks": detail["checks"],
    }
    if runtime.get("runtimeMode") == "docker":
        response["docker"] = runtime.get("dockerRuntime")
        response["pathTranslation"] = build_path_translation(profile, summary, runtime)
        response["hostControlBridge"] = runtime.get("hostControlBridge")
    return response


def summarize_bridge_action_result(profile: str, action: str, result: dict) -> dict:
    return {
        "performedAt": utc_now_iso(),
        "profile": profile,
        "action": action,
        "returncode": int(result.get("returncode", 1)),
        "actions": result.get("actions") or [],
        "stdout": (result.get("stdout") or "").strip()[-4000:],
        "stderr": (result.get("stderr") or "").strip()[-4000:],
        "composePs": (result.get("composePs") or "").strip(),
        "status": build_bridge_status(profile),
    }


def require_bridge_token(profile: str, token: str | None) -> None:
    if not settings.docker_bridge_enabled:
        raise PermissionError("宿主机控制桥未启用")
    expected = read_bridge_token(profile)
    if not expected:
        raise PermissionError(f"{profile} 尚未配置宿主机控制桥 token")
    if not token or not hmac.compare_digest(expected, token.strip()):
        raise PermissionError("宿主机控制桥 token 无效")


def record_manager_action(event: dict) -> None:
    append_jsonl(
        MANAGER_AUDIT_LOG,
        {
            "ts": utc_now_iso(),
            **event,
        },
    )


def build_manual_commands(profile: str, summary: dict, runtime: dict) -> dict:
    profile_args = [] if profile == "default" else ["--profile", profile]
    ensure_args = [str(OPENCLAW_INSTANCE_BIN), "ensure"]
    if profile != "default":
        ensure_args.append(profile)
    if isinstance(summary.get("port"), int):
        ensure_args.append(str(summary["port"]))

    runtime_mode = runtime.get("runtimeMode", "systemd")
    commands = [
        {
            "label": "查看 service 状态",
            "command": shell_join(["systemctl", "--user", "status", runtime["serviceName"], "--no-pager", "-l"]),
            "note": "先看 active/subState、MainPID 和最近几行日志。",
        },
    ]

    if runtime_mode == "docker":
        meta = runtime.get("runtimeMeta") or {}
        control_script = meta.get("controlScriptPath")
        compose_dir = meta.get("composeDir")
        container_name = (runtime.get("dockerRuntime") or {}).get("containerName") or meta.get("containerName")
        if control_script:
            commands.extend(
                [
                    {
                        "label": "启动容器实例",
                        "command": shell_join([control_script, "run"]),
                        "note": "当前 profile 已切到 Docker，启动会通过 compose 拉起容器。",
                    },
                    {
                        "label": "停止容器实例",
                        "command": shell_join([control_script, "stop"]),
                        "note": "停止 compose 管理的这个实例。",
                    },
                    {
                        "label": "查看 compose 状态",
                        "command": shell_join([control_script, "ps"]),
                        "note": "快速确认容器是否在跑。",
                    },
                    {
                        "label": "查看容器日志",
                        "command": shell_join([control_script, "logs"]),
                        "note": "Docker 试点时优先看这里。",
                    },
                ]
            )
        if compose_dir:
            commands.append(
                {
                    "label": "查看 compose 文件",
                    "command": shell_join(["sed", "-n", "1,240p", f"{compose_dir}/docker-compose.yml"]),
                    "note": "确认镜像、挂载和端口映射。",
                }
            )
        if container_name:
            commands.append(
                {
                    "label": "检查容器",
                    "command": shell_join(["docker", "inspect", container_name]),
                    "note": "查看容器镜像、状态和启动时间。",
                }
            )
    else:
        commands.extend(
            [
                {
                    "label": "启动实例",
                    "command": shell_join(["systemctl", "--user", "start", runtime["serviceName"]]),
                    "note": "正确的人工启动方式，避免直接手工跑 gateway 进程。",
                },
                {
                    "label": "重启实例",
                    "command": shell_join(["systemctl", "--user", "restart", runtime["serviceName"]]),
                    "note": "改完配置或 override 后优先用这个。",
                },
                {
                    "label": "停止实例",
                    "command": shell_join(["systemctl", "--user", "stop", runtime["serviceName"]]),
                    "note": "只停当前实例，不影响其他 profile。",
                },
            ]
        )

    commands.extend(
        [
            {
                "label": "查看日志",
                "command": shell_join(["journalctl", "--user", "-u", runtime["serviceName"], "-n", "120", "--no-pager"]),
                "note": "排障时最常用。",
            },
            {
                "label": "校验配置",
                "command": shell_join([str(OPENCLAW_BIN), *profile_args, "config", "validate", "--json"]),
                "note": "改配置后先校验，再决定是否重启。",
            },
            {
                "label": "查看 systemd 合成后的 unit",
                "command": shell_join(["systemctl", "--user", "cat", runtime["serviceName"]]),
                "note": "能同时看到主 service 和 override 的最终内容。",
            },
        ]
    )

    if runtime_mode == "docker":
        commands.extend(
            [
                {
                    "label": "医生修复 Docker 漂移",
                    "command": shell_join(
                        [
                            "curl",
                            "-sS",
                            "-X",
                            "POST",
                            "-H",
                            "Content-Type: application/json",
                            "-d",
                            json.dumps({"action": "doctor-repair", "profile": profile}, ensure_ascii=False),
                            f"http://127.0.0.1:{settings.port}/api/openclaw/action",
                        ]
                    ),
                    "note": "当 runtime meta 仍是 Docker，但 override / 端口 / 容器状态已经漂移时，用这个做单实例自愈。",
                },
                {
                    "label": "恢复默认托管形态（退出 Docker）",
                    "command": shell_join(ensure_args),
                    "note": "这是显式退出 Docker 试点的动作，不是普通重启；只有确定要回到默认 supervised 形态时才用。",
                },
            ]
        )
    else:
        commands.append(
            {
                "label": "重新 ensure 默认托管形态",
                "command": shell_join(ensure_args),
                "note": "service / override 被写坏或想恢复默认托管方式时用。",
            }
        )

    return {
        "commands": commands,
        "cautions": [
            "推荐用 systemctl --user 和 openclaw-instance 管理实例，不要直接手工跑 openclaw gateway --port ...。",
            "默认实例 default 不要通过 create/delete 管理，恢复时优先用 ensure。",
            "改完 service 或 override 后，先 daemon-reload，再 restart 对应实例。",
            "如果某个 profile 已切到 Docker 试点，仍然保留同一个 systemd user service 名字，但真正启动的是容器。",
        ],
    }


def build_docker_bridge_alignment_check(runtime: dict) -> dict:
    current_bridge = runtime.get("hostControlBridge") or {}
    if not current_bridge.get("enabled"):
        return {
            "name": "docker_bridge_matches_runtime_meta",
            "ok": False,
            "message": "Docker runtime 需要宿主机控制桥，但当前 manager 未启用 bridge",
        }

    runtime_meta = runtime.get("runtimeMeta") or {}
    persisted_bridge = runtime_meta.get("hostControlBridge")
    if not isinstance(persisted_bridge, dict):
        return {
            "name": "docker_bridge_matches_runtime_meta",
            "ok": False,
            "message": "Docker runtime meta 缺少宿主机控制桥配置",
        }

    mismatches: list[str] = []
    persisted_base_url = persisted_bridge.get("baseUrl")
    if persisted_base_url != current_bridge.get("baseUrl"):
        mismatches.append(f"baseUrl={persisted_base_url} / current={current_bridge.get('baseUrl')}")

    persisted_port = persisted_bridge.get("port")
    if persisted_port != current_bridge.get("listenPort"):
        mismatches.append(f"port={persisted_port} / current={current_bridge.get('listenPort')}")

    persisted_token_path = persisted_bridge.get("tokenPath")
    if persisted_token_path != current_bridge.get("tokenPath"):
        mismatches.append(f"tokenPath={persisted_token_path} / current={current_bridge.get('tokenPath')}")

    return {
        "name": "docker_bridge_matches_runtime_meta",
        "ok": not mismatches,
        "message": (
            "runtime meta 中的宿主机控制桥配置与当前 manager 一致"
            if not mismatches
            else "runtime meta 与当前宿主机控制桥不一致: " + "; ".join(mismatches)
        ),
    }


def build_instance_checks(profile: str, summary: dict, runtime: dict, service_text: str | None, override_text: str | None) -> list[dict]:
    checks: list[dict] = []
    config_port = summary.get("port")
    service_port = extract_port_from_unit_text(service_text)
    override_profile = extract_profile_from_override(override_text)
    override_is_docker = override_uses_docker_runtime(override_text)
    active_state = runtime.get("activeState")
    port_owners = runtime.get("portOwners") or []
    runtime_mode = runtime.get("runtimeMode", "systemd")
    docker_expected_port = docker_runtime_expected_port(runtime)
    docker_runtime = runtime.get("dockerRuntime") or {}

    checks.append(
        {
            "name": "config_exists",
            "ok": Path(summary["configPath"]).exists(),
            "message": "配置文件存在" if Path(summary["configPath"]).exists() else "配置文件不存在",
        }
    )
    checks.append(
        {
            "name": "service_active",
            "ok": active_state == "active",
            "message": f"service 状态: {active_state}/{runtime.get('subState')}",
        }
    )
    checks.append(
        {
            "name": "service_file_exists",
            "ok": runtime.get("serviceFileExists", False),
            "message": "service 文件存在" if runtime.get("serviceFileExists", False) else "service 文件缺失",
        }
    )
    checks.append(
        {
            "name": "override_file_exists",
            "ok": runtime.get("overrideFileExists", False),
            "message": "override 文件存在" if runtime.get("overrideFileExists", False) else "override 文件缺失",
        }
    )
    checks.append(
        {
            "name": "service_port_matches_config",
            "ok": (
                isinstance(config_port, int)
                and config_port == service_port
                if runtime_mode != "docker"
                else isinstance(config_port, int) and isinstance(docker_expected_port, int) and config_port == docker_expected_port
            ),
            "message": (
                f"配置端口={config_port} / service 端口={service_port}"
                if runtime_mode != "docker"
                else f"Docker 模式下以 compose/runtime meta 端口为准：配置端口={config_port} / docker 端口={docker_expected_port}"
            ),
        }
    )
    checks.append(
        {
            "name": "port_listening",
            "ok": bool(port_owners) if isinstance(config_port, int) else False,
            "message": f"端口 {config_port} 当前监听 {len(port_owners)} 个 socket" if isinstance(config_port, int) else "配置文件中没有有效端口",
        }
    )
    checks.append(
        {
            "name": "override_profile_matches",
            "ok": override_profile == profile if runtime_mode != "docker" else override_is_docker,
            "message": (
                f"override profile={override_profile} / expected={profile}"
                if runtime_mode != "docker"
                else ("override 已切到 Docker 控制脚本" if override_is_docker else "override 尚未切到 Docker 控制脚本")
            ),
        }
    )
    if runtime_mode == "docker":
        checks.append(
            {
                "name": "docker_override_matches_runtime",
                "ok": override_is_docker,
                "message": "override 当前仍指向 Docker 控制脚本" if override_is_docker else "runtime meta 标记为 Docker，但 override 已不是 Docker 版本",
            }
        )
        checks.append(
            {
                "name": "docker_port_matches_runtime_meta",
                "ok": isinstance(config_port, int) and isinstance(docker_expected_port, int) and config_port == docker_expected_port,
                "message": f"配置端口={config_port} / docker runtime meta 端口={docker_expected_port}",
            }
        )
        checks.append(
            {
                "name": "docker_container_running",
                "ok": bool(docker_runtime.get("running")),
                "message": f"容器状态={docker_runtime.get('status') or 'unknown'}",
            }
        )
        checks.append(build_docker_bridge_alignment_check(runtime))
        session_refs = find_docker_session_host_path_refs(profile, summary, runtime)
        checks.append(
            {
                "name": "docker_session_paths_container_safe",
                "ok": not session_refs,
                "message": "session 里没有宿主机路径残留" if not session_refs else f"session 仍引用宿主机路径，共 {len(session_refs)} 个文件",
            }
        )
    return checks


def instance_has_failures(detail: dict) -> bool:
    return any(not check.get("ok") for check in detail.get("checks", []))


def read_instance(profile: str) -> dict:
    profile = normalize_profile(profile)
    path = config_path_for_profile(profile)
    if not path.exists():
        raise FileNotFoundError(f"实例配置不存在: {path}")
    config = load_json(path)
    summary = extract_config_summary(profile, path, config)
    runtime = collect_runtime_info(profile)
    summary["workspace"] = resolve_workspace_path(profile, summary.get("workspace"), runtime)
    service_path = Path(runtime["fragmentPath"]) if runtime.get("fragmentPath") else None
    override_path = Path(runtime["overridePath"]) if runtime.get("overridePath") else None
    service_text = read_text_if_exists(service_path)
    override_text = read_text_if_exists(override_path)
    checks = build_instance_checks(profile, summary, runtime, service_text, override_text)
    return {
        "summary": summary,
        "runtime": runtime,
        "config": config,
        "checks": checks,
        "manual": build_manual_commands(profile, summary, runtime),
        "serviceFiles": {
            "serviceText": service_text,
            "overrideText": override_text,
        },
        "paths": {
            "configPath": str(path),
            "stateDir": str(path.parent),
            "workspaceDir": summary["workspace"],
            "servicePath": str(service_path) if service_path else None,
            "overridePath": str(override_path) if override_path else None,
        },
        "abstraction": {
            "hostPaths": build_host_visible_paths(profile, summary, runtime),
            "humanRules": build_human_runtime_rules(profile, runtime),
            "pathTranslation": build_path_translation(profile, summary, runtime) if runtime.get("runtimeMode") == "docker" else None,
        },
    }


def list_instances() -> list[dict]:
    items = []

    for path in list_openclaw_configs():
        profile = "default" if path.parent.name == ".openclaw" else path.parent.name.removeprefix(".openclaw-")
        try:
            config = load_json(path)
            summary = extract_config_summary(profile, path, config)
            runtime = collect_runtime_info(profile)
            summary["workspace"] = resolve_workspace_path(profile, summary.get("workspace"), runtime)
            items.append(
                {
                    **summary,
                    "serviceName": runtime["serviceName"],
                    "activeState": runtime["activeState"],
                    "subState": runtime["subState"],
                    "mainPid": runtime["mainPid"],
                    "runtimeMode": runtime.get("runtimeMode", "systemd"),
                    "hostControlBridge": runtime.get("hostControlBridge"),
                }
            )
        except Exception as exc:
            runtime_meta = read_runtime_meta(profile) or {}
            runtime_mode = normalize_runtime_mode(runtime_meta.get("runtimeMode"))
            items.append(
                {
                    "profile": profile,
                    "configPath": str(path),
                    "runtimeMode": runtime_mode,
                    "hostControlBridge": build_host_control_bridge(profile) if runtime_mode == "docker" else None,
                    "error": str(exc),
                }
            )
    items.sort(key=lambda item: (item.get("profile") != "default", item.get("port") or 0, item.get("profile", "")))
    return items


def openclaw_summary() -> dict:
    instances = list_instances()
    return {
        "generatedAt": utc_now_iso(),
        "instances": instances,
        "managerRoot": str(MANAGER_ROOT),
        "openclawInstanceBin": str(OPENCLAW_INSTANCE_BIN),
        "openclawBin": str(OPENCLAW_BIN),
    }


def build_diagnostics() -> dict:
    instances = list_instances()
    port_map: dict[int, list[str]] = {}
    issues: list[dict] = []

    for item in instances:
        port = item.get("port")
        profile = item.get("profile", "<unknown>")
        if isinstance(port, int):
            port_map.setdefault(port, []).append(profile)
        if item.get("activeState") != "active":
            issues.append(
                {
                    "level": "warn",
                    "profile": profile,
                    "message": f"service 未运行: {item.get('serviceName')} ({item.get('activeState')}/{item.get('subState')})",
                }
            )
        try:
            detail = read_instance(profile)
            for check in detail["checks"]:
                if not check["ok"]:
                    issues.append(
                        {
                            "level": "warn",
                            "profile": profile,
                            "check": check["name"],
                            "message": check["message"],
                        }
                    )
        except Exception as exc:
            issues.append({"level": "error", "profile": profile, "message": str(exc)})

    for port, profiles in sorted(port_map.items()):
        if len(profiles) > 1:
            issues.append(
                {
                    "level": "error",
                    "port": port,
                    "profiles": profiles,
                    "message": f"端口冲突: {port} 被多个 profile 配置: {', '.join(profiles)}",
                }
            )

    for item in instances:
        if item.get("error"):
            issues.append({"level": "error", "profile": item.get("profile"), "message": item["error"]})

    return {
        "generatedAt": utc_now_iso(),
        "issues": issues,
        "portMap": port_map,
        "instances": instances,
    }
