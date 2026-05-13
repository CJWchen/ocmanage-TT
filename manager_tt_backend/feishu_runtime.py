from __future__ import annotations

import json
import re
from pathlib import Path

from .config import (
    CONTAINER_STATE_DIR,
    OPENCLAW_BIN,
    config_path_for_profile,
    load_json,
    normalize_profile,
    read_runtime_meta,
    service_name_for_profile,
    state_dir_for_profile,
    utc_now_iso,
)
from .config_modules import truncate_text
from .create_modes import normalize_runtime_mode
from .feishu_modules import summarize_feishu_channel
from .system import inspect_docker_runtime, read_service_logs, run_openclaw_command

FEISHU_PLUGIN_ID = "feishu"
FEISHU_PLUGIN_PACKAGE = "@openclaw/feishu"
_FEISHU_READY_LOG_MARKERS = ("websocket client started", "ws client ready")
_FEISHU_START_LOG_MARKERS = ("starting feishu[",)
_FEISHU_ERROR_MARKERS = (
    "plugin not installed: feishu",
    "missing scope: operator.admin",
    "channel login failed",
    "wizardcancellederror",
)
_DOCKER_BIND_MOUNT_PATTERN = re.compile(
    r"^\s*-\s*(?P<host>/[^:\s]+):(?P<container>/[^:\s]+)(?::[^#\s]+)?\s*$"
)


def read_feishu_runtime_context(profile: str) -> dict:
    normalized_profile = normalize_profile(profile)
    runtime_meta = read_runtime_meta(normalized_profile) or {}
    runtime_mode = normalize_runtime_mode(runtime_meta.get("runtimeMode"))
    docker_runtime = inspect_docker_runtime(runtime_meta) if runtime_mode == "docker" else None
    return {
        "profile": normalized_profile,
        "runtimeMode": runtime_mode,
        "runtimeMeta": runtime_meta,
        "dockerRuntime": docker_runtime,
        "serviceName": service_name_for_profile(normalized_profile),
    }


def ensure_runtime_context(profile: str, runtime: dict | None = None) -> dict:
    normalized_profile = normalize_profile(profile)
    if not isinstance(runtime, dict):
        return read_feishu_runtime_context(normalized_profile)

    runtime_meta = runtime.get("runtimeMeta") if isinstance(runtime.get("runtimeMeta"), dict) else read_runtime_meta(normalized_profile) or {}
    runtime_mode = normalize_runtime_mode(runtime.get("runtimeMode") or runtime_meta.get("runtimeMode"))
    docker_runtime = (
        runtime.get("dockerRuntime")
        if isinstance(runtime.get("dockerRuntime"), dict)
        else (inspect_docker_runtime(runtime_meta) if runtime_mode == "docker" else None)
    )
    return {
        **runtime,
        "profile": normalized_profile,
        "runtimeMode": runtime_mode,
        "runtimeMeta": runtime_meta,
        "dockerRuntime": docker_runtime,
        "serviceName": runtime.get("serviceName") or service_name_for_profile(normalized_profile),
    }


def build_runtime_openclaw_args(profile: str, subcommand_args: list[str], runtime: dict | None = None) -> list[str]:
    runtime_context = ensure_runtime_context(profile, runtime)
    args: list[str] = []
    if runtime_context.get("runtimeMode") == "docker":
        container_name = resolve_runtime_container_name(runtime_context)
        if not container_name:
            raise ValueError(f"{runtime_context['profile']} 缺少 Docker 容器名，无法进入运行态执行 OpenClaw 命令")
        args.extend(["--container", container_name])
    elif runtime_context["profile"] != "default":
        args.extend(["--profile", runtime_context["profile"]])
    args.extend(subcommand_args)
    return args


def build_runtime_openclaw_command(profile: str, subcommand_args: list[str], runtime: dict | None = None) -> list[str]:
    return [str(OPENCLAW_BIN), *build_runtime_openclaw_args(profile, subcommand_args, runtime)]


def run_runtime_openclaw_command(
    profile: str,
    subcommand_args: list[str],
    *,
    runtime: dict | None = None,
    timeout_ms: int = 45000,
) -> dict:
    return run_openclaw_command(build_runtime_openclaw_args(profile, subcommand_args, runtime), timeout_ms=timeout_ms)


def resolve_runtime_container_name(runtime: dict | None) -> str | None:
    runtime = runtime if isinstance(runtime, dict) else {}
    docker_runtime = runtime.get("dockerRuntime") if isinstance(runtime.get("dockerRuntime"), dict) else {}
    runtime_meta = runtime.get("runtimeMeta") if isinstance(runtime.get("runtimeMeta"), dict) else {}
    for value in (docker_runtime.get("containerName"), runtime_meta.get("containerName")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def docker_runtime_is_running(runtime: dict | None) -> bool:
    runtime = runtime if isinstance(runtime, dict) else {}
    docker_runtime = runtime.get("dockerRuntime") if isinstance(runtime.get("dockerRuntime"), dict) else {}
    return bool(docker_runtime.get("running"))


def normalize_absolute_path(path: str | None) -> str | None:
    value = path.strip() if isinstance(path, str) else ""
    if not value or not value.startswith("/"):
        return None
    return str(Path(value))


def read_runtime_compose_bind_mounts(runtime: dict | None) -> list[tuple[str, str]]:
    runtime = runtime if isinstance(runtime, dict) else {}
    runtime_meta = runtime.get("runtimeMeta") if isinstance(runtime.get("runtimeMeta"), dict) else {}
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
        match = _DOCKER_BIND_MOUNT_PATTERN.match(line)
        if not match:
            continue
        host_path = normalize_absolute_path(match.group("host"))
        container_path = normalize_absolute_path(match.group("container"))
        if host_path and container_path:
            mounts.append((host_path, container_path))
    return mounts


def collect_runtime_state_aliases(profile: str, runtime: dict | None = None) -> list[str]:
    aliases: list[str] = [CONTAINER_STATE_DIR]
    host_state_dir = normalize_absolute_path(str(state_dir_for_profile(profile)))
    if not host_state_dir:
        return aliases
    for host_path, container_path in read_runtime_compose_bind_mounts(runtime):
        if host_path != host_state_dir or container_path in aliases:
            continue
        aliases.append(container_path)
    return aliases


def plugin_path_is_runtime_aligned(profile: str, runtime: dict | None, candidate_path: str | None) -> bool:
    candidate = normalize_absolute_path(candidate_path)
    if not candidate:
        return False
    for alias in collect_runtime_state_aliases(profile, runtime):
        if candidate == alias or candidate.startswith(f"{alias}/"):
            return True
    return False


def read_feishu_config_summary(profile: str) -> dict:
    path = config_path_for_profile(profile)
    if not path.exists():
        return {}
    try:
        return summarize_feishu_channel(load_json(path))
    except Exception:
        return {}


def config_requests_feishu(config_summary: dict) -> bool:
    return bool(
        config_summary.get("enabled")
        or config_summary.get("hasAppId")
        or config_summary.get("hasAppSecret")
        or config_summary.get("accountCount")
    )


def inspect_feishu_plugin(profile: str, *, runtime: dict | None = None) -> dict:
    runtime_context = ensure_runtime_context(profile, runtime)
    result = run_runtime_openclaw_command(
        profile,
        ["plugins", "inspect", FEISHU_PLUGIN_ID, "--json", "--runtime"],
        runtime=runtime_context,
        timeout_ms=20000,
    )
    payload = parse_openclaw_json_result(result)
    plugin = payload.get("plugin") if isinstance(payload.get("plugin"), dict) else {}
    install = payload.get("install") if isinstance(payload.get("install"), dict) else {}
    channel_ids = plugin.get("channelIds") if isinstance(plugin.get("channelIds"), list) else []
    install_path = install.get("installPath") if isinstance(install.get("installPath"), str) else None
    source = plugin.get("source") if isinstance(plugin.get("source"), str) else None
    path_aligned = True
    if runtime_context.get("runtimeMode") == "docker":
        path_aligned = plugin_path_is_runtime_aligned(runtime_context["profile"], runtime_context, install_path or source)

    loaded = (
        result.get("returncode") == 0
        and plugin.get("status") == "loaded"
        and FEISHU_PLUGIN_ID in channel_ids
        and path_aligned
    )
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), list) else []
    error = None
    if result.get("returncode") != 0:
        error = truncate_text(first_non_empty(result.get("stderr"), result.get("stdout"), "Plugin not found"))
    elif not loaded:
        error = "Feishu 插件已存在，但当前运行态没有把它加载为可用 channel"

    return {
        "checkedAt": utc_now_iso(),
        "command": build_runtime_openclaw_command(profile, ["plugins", "inspect", FEISHU_PLUGIN_ID, "--json", "--runtime"], runtime_context),
        "returncode": int(result.get("returncode", 1)),
        "loaded": loaded,
        "status": plugin.get("status") or ("missing" if result.get("returncode") else "unknown"),
        "enabled": bool(plugin.get("enabled")),
        "activated": bool(plugin.get("activated")),
        "channelIds": [item for item in channel_ids if isinstance(item, str)],
        "version": plugin.get("version"),
        "source": source,
        "installPath": install_path,
        "origin": plugin.get("origin"),
        "pathAligned": path_aligned,
        "diagnostics": diagnostics,
        "error": error,
    }


def inspect_feishu_channel(profile: str, *, runtime: dict | None = None) -> dict:
    runtime_context = ensure_runtime_context(profile, runtime)
    result = run_runtime_openclaw_command(
        profile,
        ["channels", "status", "--json", "--probe"],
        runtime=runtime_context,
        timeout_ms=30000,
    )
    payload = parse_openclaw_json_result(result)
    channels = payload.get("channels") if isinstance(payload.get("channels"), dict) else {}
    channel_entry = channels.get(FEISHU_PLUGIN_ID) if isinstance(channels.get(FEISHU_PLUGIN_ID), dict) else {}
    account_map = payload.get("channelAccounts") if isinstance(payload.get("channelAccounts"), dict) else {}
    account_entries = account_map.get(FEISHU_PLUGIN_ID) if isinstance(account_map.get(FEISHU_PLUGIN_ID), list) else []
    default_map = payload.get("channelDefaultAccountId") if isinstance(payload.get("channelDefaultAccountId"), dict) else {}
    default_account_id = default_map.get(FEISHU_PLUGIN_ID) if isinstance(default_map.get(FEISHU_PLUGIN_ID), str) else None

    account = {}
    if account_entries:
        if default_account_id:
            for item in account_entries:
                if isinstance(item, dict) and item.get("accountId") == default_account_id:
                    account = item
                    break
        if not account:
            first = account_entries[0]
            account = first if isinstance(first, dict) else {}

    probe = account.get("probe") if isinstance(account.get("probe"), dict) else channel_entry.get("probe")
    probe = probe if isinstance(probe, dict) else {}
    configured_channels = payload.get("configuredChannels") if isinstance(payload.get("configuredChannels"), list) else []
    gateway_reachable = payload.get("gatewayReachable")
    config_only = bool(payload.get("configOnly"))
    running = coalesce_bool(account.get("running"), channel_entry.get("running"))
    configured = coalesce_bool(account.get("configured"), channel_entry.get("configured"))
    ready = bool(running and probe.get("ok") is True)

    error = None
    if result.get("returncode") != 0:
        error = truncate_text(first_non_empty(result.get("stderr"), result.get("stdout"), "读取 Feishu channel 状态失败"))
    elif isinstance(payload.get("error"), str) and payload.get("error").strip():
        error = truncate_text(payload.get("error"))

    return {
        "checkedAt": utc_now_iso(),
        "command": build_runtime_openclaw_command(profile, ["channels", "status", "--json", "--probe"], runtime_context),
        "returncode": int(result.get("returncode", 1)),
        "gatewayReachable": gateway_reachable if isinstance(gateway_reachable, bool) else None,
        "configOnly": config_only,
        "configuredChannels": [item for item in configured_channels if isinstance(item, str)],
        "configured": configured,
        "running": running,
        "ready": ready,
        "defaultAccountId": default_account_id or account.get("accountId"),
        "accountId": account.get("accountId"),
        "restartPending": coalesce_bool(account.get("restartPending"), channel_entry.get("restartPending")),
        "reconnectAttempts": account.get("reconnectAttempts"),
        "lastError": first_non_empty(account.get("lastError"), channel_entry.get("lastError")),
        "lastStartAt": first_non_empty(account.get("lastStartAt"), channel_entry.get("lastStartAt")),
        "lastStopAt": first_non_empty(account.get("lastStopAt"), channel_entry.get("lastStopAt")),
        "appId": first_non_empty(account.get("appId"), probe.get("appId")),
        "botName": probe.get("botName"),
        "botOpenId": probe.get("botOpenId"),
        "probeOk": probe.get("ok") if isinstance(probe.get("ok"), bool) else None,
        "probe": probe,
        "error": error,
    }


def inspect_feishu_logs(service_name: str, *, lines: int = 160) -> dict:
    text = read_service_logs(service_name, lines=lines)
    relevant: list[str] = []
    started = False
    ready = False
    errors: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line:
            continue
        if "feishu" in lowered or "websocket client started" in lowered or "ws client ready" in lowered or "operator.admin" in lowered:
            relevant.append(line)
        if any(marker in lowered for marker in _FEISHU_START_LOG_MARKERS):
            started = True
        if any(marker in lowered for marker in _FEISHU_READY_LOG_MARKERS):
            ready = True
        if any(marker in lowered for marker in _FEISHU_ERROR_MARKERS):
            errors.append(line)
    return {
        "checkedAt": utc_now_iso(),
        "serviceName": service_name,
        "started": started,
        "ready": ready,
        "errors": errors[-6:],
        "recentLines": relevant[-12:],
    }


def summarize_feishu_runtime_health(config_summary: dict, plugin: dict, channel: dict, logs: dict, runtime: dict) -> dict:
    requested = config_requests_feishu(config_summary)
    issues: list[str] = []
    notes: list[str] = []
    ready_evidence = None
    ready = False

    if not requested:
        return {
            "status": "disabled",
            "ready": False,
            "readyEvidence": None,
            "issues": [],
            "notes": [],
        }

    if not config_summary.get("hasAppId"):
        issues.append("Feishu 配置缺少 appId")
    if not config_summary.get("hasAppSecret"):
        issues.append("Feishu 配置缺少 appSecret")
    if not plugin.get("loaded"):
        issues.append(plugin.get("error") or "Feishu 插件尚未被运行态加载")
    if logs.get("errors"):
        issues.extend(line for line in logs["errors"] if line not in issues)
    if channel.get("lastError"):
        issues.append(truncate_text(str(channel["lastError"])))
    if channel.get("error") and channel["error"] not in issues:
        issues.append(channel["error"])

    if plugin.get("loaded") and channel.get("ready"):
        ready = True
        ready_evidence = "probe"
    elif plugin.get("loaded") and logs.get("ready") and "missing scope: operator.read" in (channel.get("error") or ""):
        ready = True
        ready_evidence = "logs"
        notes.append("Gateway probe 受 operator.read 权限限制，ready 状态根据最近服务日志推断")

    if ready:
        status = "ready"
    elif not plugin.get("loaded"):
        status = "plugin_missing"
    elif channel.get("restartPending"):
        status = "pending_restart"
    elif channel.get("configured") is False and config_summary.get("hasAppId") and config_summary.get("hasAppSecret"):
        status = "pending_login"
    elif channel.get("running") is False and channel.get("configured"):
        status = "not_running"
    elif logs.get("started"):
        status = "running_unverified"
    elif runtime.get("runtimeMode") == "docker" and not docker_runtime_is_running(runtime):
        status = "runtime_down"
    else:
        status = "runtime_blocked"

    deduped_issues = dedupe_strings(issues)
    deduped_notes = dedupe_strings(notes)
    return {
        "status": status,
        "ready": ready,
        "readyEvidence": ready_evidence,
        "issues": deduped_issues,
        "notes": deduped_notes,
    }


def gather_feishu_runtime_status(profile: str, *, runtime: dict | None = None) -> dict:
    runtime_context = ensure_runtime_context(profile, runtime)
    config_summary = read_feishu_config_summary(runtime_context["profile"])
    plugin = inspect_feishu_plugin(runtime_context["profile"], runtime=runtime_context) if config_requests_feishu(config_summary) else {}
    channel = inspect_feishu_channel(runtime_context["profile"], runtime=runtime_context) if config_requests_feishu(config_summary) else {}
    logs = inspect_feishu_logs(runtime_context["serviceName"]) if config_requests_feishu(config_summary) else {}
    health = summarize_feishu_runtime_health(config_summary, plugin, channel, logs, runtime_context)
    return {
        "checkedAt": utc_now_iso(),
        "profile": runtime_context["profile"],
        "runtimeMode": runtime_context.get("runtimeMode"),
        "serviceName": runtime_context.get("serviceName"),
        "containerName": resolve_runtime_container_name(runtime_context),
        "config": config_summary,
        "plugin": plugin,
        "channel": channel,
        "logs": logs,
        **health,
    }


def install_feishu_plugin(profile: str, *, runtime: dict | None = None) -> dict:
    runtime_context = ensure_runtime_context(profile, runtime)
    result = run_runtime_openclaw_command(
        profile,
        ["plugins", "install", FEISHU_PLUGIN_PACKAGE, "--force"],
        runtime=runtime_context,
        timeout_ms=60000,
    )
    return {
        "performedAt": utc_now_iso(),
        "command": build_runtime_openclaw_command(profile, ["plugins", "install", FEISHU_PLUGIN_PACKAGE, "--force"], runtime_context),
        "returncode": int(result.get("returncode", 1)),
        "stdout": truncate_text(result.get("stdout"), limit=4000),
        "stderr": truncate_text(result.get("stderr"), limit=4000),
    }


def ensure_feishu_plugin_available(profile: str, *, runtime: dict | None = None) -> dict:
    runtime_context = ensure_runtime_context(profile, runtime)
    if runtime_context.get("runtimeMode") == "docker" and not docker_runtime_is_running(runtime_context):
        raise RuntimeError(f"{runtime_context['profile']} 的 Docker 容器未运行，无法启动 Feishu QR 向导")

    before = inspect_feishu_plugin(runtime_context["profile"], runtime=runtime_context)
    actions: list[str] = []
    install = None
    current = before
    if not before.get("loaded"):
        install = install_feishu_plugin(runtime_context["profile"], runtime=runtime_context)
        if install["returncode"] != 0:
            detail = first_non_empty(install.get("stderr"), install.get("stdout"), before.get("error"))
            raise RuntimeError(f"安装 Feishu 插件失败: {detail}")
        actions.append("已安装 Feishu 插件")
        current = inspect_feishu_plugin(runtime_context["profile"], runtime=runtime_context)
        if not current.get("loaded"):
            raise RuntimeError(current.get("error") or "Feishu 插件安装后仍未被运行态识别")
    return {
        "performedAt": utc_now_iso(),
        "profile": runtime_context["profile"],
        "runtimeMode": runtime_context.get("runtimeMode"),
        "actions": actions,
        "install": install,
        "plugin": current,
    }


def ensure_feishu_runtime(
    profile: str,
    *,
    install_plugin: bool = True,
    restart_gateway: bool = False,
) -> dict:
    runtime_context = read_feishu_runtime_context(profile)
    before = gather_feishu_runtime_status(runtime_context["profile"], runtime=runtime_context)
    current = before
    actions: list[str] = []
    install = None
    restart = None

    if restart_gateway and runtime_context.get("runtimeMode") == "docker" and not docker_runtime_is_running(runtime_context):
        restart = restart_feishu_gateway(runtime_context["profile"])
        actions.append(f"已重启 {runtime_context['serviceName']}")
        if restart["returncode"] != 0:
            return {
                "performedAt": utc_now_iso(),
                "profile": runtime_context["profile"],
                "status": "failed",
                "error": "运行态启动失败",
                "actions": actions,
                "restart": restart,
                "before": before,
                "feishuRuntime": gather_feishu_runtime_status(runtime_context["profile"]),
            }
        runtime_context = read_feishu_runtime_context(runtime_context["profile"])
        current = gather_feishu_runtime_status(runtime_context["profile"], runtime=runtime_context)

    if install_plugin and not current.get("plugin", {}).get("loaded"):
        install = install_feishu_plugin(runtime_context["profile"], runtime=runtime_context)
        if install["returncode"] == 0:
            actions.append("已在运行态安装 Feishu 插件")
        runtime_context = read_feishu_runtime_context(runtime_context["profile"])
        current = gather_feishu_runtime_status(runtime_context["profile"], runtime=runtime_context)

    if restart_gateway:
        restart = restart_feishu_gateway(runtime_context["profile"])
        if restart["returncode"] == 0:
            actions.append(f"已重启 {runtime_context['serviceName']}")
        runtime_context = read_feishu_runtime_context(runtime_context["profile"])
        current = gather_feishu_runtime_status(runtime_context["profile"], runtime=runtime_context)

    status = current.get("status") or "runtime_blocked"
    error = None
    if restart and restart.get("returncode") != 0:
        status = "failed"
        error = "Feishu 配置已保存，但网关重启失败"
    elif install and install.get("returncode") != 0:
        status = "failed"
        error = "Feishu 配置已保存，但插件安装失败"

    return {
        "performedAt": utc_now_iso(),
        "profile": runtime_context["profile"],
        "status": status,
        "ready": bool(current.get("ready")),
        "error": error,
        "actions": actions,
        "install": install,
        "restart": restart,
        "before": before,
        "feishuRuntime": current,
    }


def restart_feishu_gateway(profile: str) -> dict:
    normalized_profile = normalize_profile(profile)
    service_name = service_name_for_profile(normalized_profile)
    from .actions import run_systemctl_action

    result = run_systemctl_action(service_name, "restart")
    return {
        "performedAt": utc_now_iso(),
        "serviceName": service_name,
        **result,
    }


def parse_openclaw_json_result(result: dict) -> dict:
    for source in (result.get("stdout"), result.get("stderr")):
        payload = extract_json_document(source)
        if isinstance(payload, dict):
            return payload
    combined = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if isinstance(part, str) and part.strip())
    payload = extract_json_document(combined)
    return payload if isinstance(payload, dict) else {}


def extract_json_document(text: str | None) -> dict | list | None:
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start == -1 or end == -1 or end <= start:
            continue
        snippet = candidate[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
    return None


def coalesce_bool(*values: object) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                return candidate
    return None


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result
