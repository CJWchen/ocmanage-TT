from __future__ import annotations

import json
import shlex
from pathlib import Path

from .config import (
    OPENCLAW_BIN,
    OPENCLAW_SYSTEMD_DIR,
    bridge_token_path_for_profile,
    config_path_for_profile,
    load_json,
    normalize_profile,
    read_runtime_meta,
    service_name_for_profile,
    state_dir_for_profile,
    utc_now_iso,
    write_json,
)
from .create_modes import DOCKER_CREATE_MODE, HOST_CREATE_MODE, ensure_host_create_allowed, normalize_runtime_mode, resolve_create_mode
from .docker_managed import (
    build_docker_override_text,
    create_instance_via_docker_manager,
    docker_compose_dir_for_profile,
    docker_control_script_path_for_profile,
)
from .host_managed import create_instance_via_host_manager, ensure_instance_via_host_manager
from .instances import (
    find_docker_session_host_path_refs,
    instance_has_failures,
    list_instances,
    read_instance,
    relocate_legacy_docker_session_backups,
    reset_docker_sessions,
)
from .system import backup_file, read_text_if_exists, run_shell


def run_systemctl_action(service_name: str, action: str) -> dict:
    if action not in {"start", "stop", "restart"}:
        raise ValueError(f"不支持的 service action: {action}")
    result = run_shell(f"systemctl --user {action} {service_name}", timeout_ms=30000)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }


def create_instance(payload: dict) -> dict:
    create_mode = resolve_create_mode(payload)
    if create_mode == HOST_CREATE_MODE:
        ensure_host_create_allowed(list_instances())
        return create_instance_via_host_manager(payload)
    return create_instance_via_docker_manager(payload)


def ensure_instance(payload: dict) -> dict:
    return ensure_instance_via_host_manager(payload)


def doctor_repair_instance(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    detail_before = read_instance(profile)
    summary = detail_before["summary"]
    runtime = detail_before["runtime"]
    runtime_mode = runtime.get("runtimeMode", "systemd")
    service_name = runtime["serviceName"]
    override_path = Path(detail_before["paths"]["overridePath"]) if detail_before["paths"].get("overridePath") else None
    runtime_meta = runtime.get("runtimeMeta") or {}

    actions: list[str] = []
    backup_paths: list[str] = []

    if runtime_mode == "docker":
        docker_port = runtime_meta.get("port")
        if not isinstance(docker_port, int):
            raise ValueError(f"{profile} 的 Docker runtime meta 缺少有效端口")
        legacy_backups = relocate_legacy_docker_session_backups(profile)
        if legacy_backups:
            backup_paths.extend(legacy_backups)
            actions.append(f"已迁移 {len(legacy_backups)} 个旧 sessions 备份目录到 .doctor-backups")
        session_backups = find_docker_session_host_path_refs(profile, summary, runtime)
        if session_backups:
            backup_dir = reset_docker_sessions(profile)
            if backup_dir:
                backup_paths.append(backup_dir)
                actions.append(f"检测到 {len(session_backups)} 个 session 文件仍引用宿主机路径，已备份并重置 sessions")
        if summary.get("port") != docker_port:
            config_path = config_path_for_profile(profile)
            config = load_json(config_path)
            gateway = config.setdefault("gateway", {})
            control_ui = gateway.setdefault("controlUi", {})
            gateway["port"] = docker_port
            control_ui["allowedOrigins"] = [
                f"http://localhost:{docker_port}",
                f"http://127.0.0.1:{docker_port}",
            ]
            backup = backup_file(config_path)
            if backup:
                backup_paths.append(str(backup))
            write_json(config_path, config)
            actions.append(f"配置端口已回写为 Docker 端口 {docker_port}")

        desired_override = build_docker_override_text(profile, runtime_meta)
        current_override = read_text_if_exists(override_path)
        if current_override != desired_override:
            if override_path:
                override_path.parent.mkdir(parents=True, exist_ok=True)
                backup = backup_file(override_path)
                if backup:
                    backup_paths.append(str(backup))
                override_path.write_text(desired_override, encoding="utf-8")
                actions.append("已恢复 Docker override")

        compose_path = runtime_meta.get("composePath")
        project_name = runtime_meta.get("projectName")
        if not compose_path or not project_name:
            raise ValueError(f"{profile} 的 Docker runtime meta 不完整，缺少 composePath/projectName")

        run_shell("systemctl --user daemon-reload", timeout_ms=20000)
        actions.append("已执行 daemon-reload")
        restart_result = run_shell(f"systemctl --user restart {service_name}", timeout_ms=45000)
        actions.append(f"已重启 {service_name}")
        compose_ps = run_shell(
            f"docker compose --project-name {shlex.quote(project_name)} -f {shlex.quote(compose_path)} ps",
            timeout_ms=20000,
        )
        detail_after = read_instance(profile)
        return {
            "stdout": restart_result.stdout,
            "stderr": restart_result.stderr,
            "returncode": restart_result.returncode,
            "profile": profile,
            "actions": actions,
            "backupPaths": backup_paths,
            "composePs": compose_ps.stdout or compose_ps.stderr,
            "detailBefore": detail_before,
            "detailAfter": detail_after,
        }

    ensure_payload = {"profile": profile}
    if isinstance(summary.get("port"), int):
        ensure_payload["port"] = summary["port"]
    result = ensure_instance(ensure_payload)
    actions.append("已执行 ensure 恢复默认托管形态")
    detail_after = read_instance(profile)
    return {
        **result,
        "profile": profile,
        "actions": actions,
        "backupPaths": backup_paths,
        "detailBefore": detail_before,
        "detailAfter": detail_after,
    }


def repair_all_instances(_: dict) -> dict:
    actions: list[dict] = []
    overall_returncode = 0

    for item in list_instances():
        profile = normalize_profile(item.get("profile"))
        detail = read_instance(profile)
        runtime_mode = detail["runtime"].get("runtimeMode", "systemd")

        if not instance_has_failures(detail):
            actions.append(
                {
                    "profile": profile,
                    "mode": runtime_mode,
                    "result": "skipped",
                    "message": "未发现异常，跳过",
                    "returncode": 0,
                }
            )
            continue

        try:
            if runtime_mode == "docker":
                result = doctor_repair_instance({"profile": profile})
            else:
                ensure_payload = {"profile": profile}
                port = detail["summary"].get("port")
                if isinstance(port, int):
                    ensure_payload["port"] = port
                result = ensure_instance(ensure_payload)

            returncode = int(result.get("returncode", 1))
            overall_returncode = max(overall_returncode, returncode)
            actions.append(
                {
                    "profile": profile,
                    "mode": runtime_mode,
                    "result": "repaired" if returncode == 0 else "failed",
                    "message": "; ".join(result.get("actions") or []) or result.get("stderr") or result.get("stdout") or "",
                    "returncode": returncode,
                }
            )
        except Exception as exc:
            overall_returncode = max(overall_returncode, 1)
            actions.append(
                {
                    "profile": profile,
                    "mode": runtime_mode,
                    "result": "failed",
                    "message": str(exc),
                    "returncode": 1,
                }
            )

    return {
        "stdout": json.dumps(actions, ensure_ascii=False, indent=2),
        "stderr": "",
        "returncode": overall_returncode,
        "actions": actions,
    }


def delete_instance(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    if profile == "default":
        raise ValueError("默认实例不允许删除")

    remove_state = bool(payload.get("removeStateDir", False))
    runtime_mode = normalize_runtime_mode((read_runtime_meta(profile) or {}).get("runtimeMode"))
    service_name = service_name_for_profile(profile)
    state_dir = state_dir_for_profile(profile)
    service_path = OPENCLAW_SYSTEMD_DIR / service_name
    override_dir = OPENCLAW_SYSTEMD_DIR / f"{service_name}.d"

    commands = [
        f"systemctl --user disable --now {service_name} 2>/dev/null || true",
        f"rm -f {service_path}",
        f"rm -rf {override_dir}",
        "systemctl --user daemon-reload",
    ]
    if remove_state:
        commands.append(f"rm -rf {shlex.quote(str(state_dir))}")
        if runtime_mode == DOCKER_CREATE_MODE:
            commands.extend(
                [
                    f"rm -rf {shlex.quote(str(docker_compose_dir_for_profile(profile)))}",
                    f"rm -f {shlex.quote(str(docker_control_script_path_for_profile(profile)))}",
                    f"rm -f {shlex.quote(str(bridge_token_path_for_profile(profile)))}",
                ]
            )
    result = run_shell(" && ".join(commands), timeout_ms=45000)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }


def perform_instance_action(payload: dict) -> dict:
    action = (payload.get("action") or "").strip()
    if action == "create":
        return create_instance(payload)
    if action == "ensure":
        profile = normalize_profile(payload.get("profile"))
        runtime = read_instance(profile)["runtime"]
        if runtime.get("runtimeMode") == "docker" and not payload.get("forceHostManaged"):
            return doctor_repair_instance({"profile": profile})
        return ensure_instance(payload)
    if action == "repair-all":
        return repair_all_instances(payload)
    if action == "doctor-repair":
        return doctor_repair_instance(payload)
    if action == "delete":
        return delete_instance(payload)

    profile = normalize_profile(payload.get("profile"))
    service_name = service_name_for_profile(profile)
    if action in {"start", "stop", "restart"}:
        return run_systemctl_action(service_name, action)
    if action == "daemon-reload":
        result = run_shell("systemctl --user daemon-reload", timeout_ms=15000)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    raise ValueError(f"未知 action: {action}")


def save_config(payload: dict) -> dict:
    profile = normalize_profile(payload.get("profile"))
    config = payload.get("config")
    if not isinstance(config, dict):
        raise ValueError("config 必须是对象")

    path = config_path_for_profile(profile)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    backup = backup_file(path)
    write_json(path, config)

    validate_result = run_shell(
        f"{OPENCLAW_BIN} {'--profile ' + profile if profile != 'default' else ''} config validate --json || true",
        timeout_ms=20000,
    )
    maybe_restart = None
    if payload.get("restartAfterSave"):
        maybe_restart = run_systemctl_action(service_name_for_profile(profile), "restart")

    return {
        "savedAt": utc_now_iso(),
        "configPath": str(path),
        "backupPath": str(backup) if backup else None,
        "validate": {
            "stdout": validate_result.stdout,
            "stderr": validate_result.stderr,
            "returncode": validate_result.returncode,
        },
        "restart": maybe_restart,
    }
