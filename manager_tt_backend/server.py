from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

from .actions import (
    _coerce_bool_field,
    apply_feishu_channel_module,
    apply_tencent_model_module,
    perform_instance_action,
    save_config,
)
from .config import (
    DEFAULT_TIMEOUT_MS,
    ensure_management_token,
    is_exec_command_allowed,
    MANAGER_ROOT,
    MAX_TIMEOUT_MS,
    normalize_profile,
    service_name_for_profile,
    settings,
    utc_now_iso,
)
from .instances import (
    build_bridge_status,
    build_diagnostics,
    list_instances,
    openclaw_summary,
    read_instance,
    record_manager_action,
    require_bridge_token,
    require_management_token,
    summarize_bridge_action_result,
)
from .feishu_qr_sessions import (
    get_feishu_qr_session_status,
    send_feishu_qr_input,
    start_feishu_qr_session,
    stop_feishu_qr_session,
)
from .system import read_service_logs, run_shell
from .core.logging import (
    RequestLogger,
    get_logger,
    get_request_id,
    setup_logging,
)
from .service_registry import (
    get_registry,
    service_to_dict,
    detail_to_dict,
    action_result_to_dict,
    logs_to_dict,
    health_to_dict,
)

logger = get_logger(__name__)


class ExecHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send_cors(204)

    def do_GET(self):
        self._route_request()

    def do_POST(self):
        self._route_request()

    def _route_request(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self._request_logger = RequestLogger(
            logger,
            self.command,
            path,
            self.client_address[0] if self.client_address else None,
        )
        with self._request_logger:
            self._route_request_internal(parsed, path)

    def _route_request_internal(self, parsed, path):
        # CGI routes
        if path == "/cgi-bin/exec":
            self._handle_exec()
            return
        if path == "/cgi-bin/status":
            self._handle_status()
            return

        # 新的统一服务 API
        if path.startswith("/api/services"):
            self._handle_services_route(parsed, path)
            return

        # 旧版 OpenClaw API（保持向后兼容）
        if path == "/api/openclaw/summary":
            self._handle_openclaw_summary()
            return
        if path == "/api/openclaw/instances":
            self._handle_openclaw_instances()
            return
        if path == "/api/openclaw/instance":
            self._handle_openclaw_instance(parsed.query)
            return
        if path == "/api/openclaw/action":
            self._handle_openclaw_action()
            return
        if path == "/api/openclaw/config":
            self._handle_openclaw_config()
            return
        if path in {"/api/openclaw/config/feishu-channel", "/api/openclaw/config/feishu"}:
            self._handle_openclaw_feishu_channel_module()
            return
        if path == "/api/openclaw/config/tencent-coding-plan":
            self._handle_openclaw_tencent_model_module()
            return
        if path == "/api/openclaw/feishu/qr/status":
            self._handle_openclaw_feishu_qr_status(parsed.query)
            return
        if path == "/api/openclaw/feishu/qr/start":
            self._handle_openclaw_feishu_qr_start()
            return
        if path == "/api/openclaw/feishu/qr/input":
            self._handle_openclaw_feishu_qr_input()
            return
        if path == "/api/openclaw/feishu/qr/stop":
            self._handle_openclaw_feishu_qr_stop()
            return
        if path == "/api/openclaw/logs":
            self._handle_openclaw_logs(parsed.query)
            return
        if path == "/api/openclaw/diagnostics":
            self._handle_openclaw_diagnostics()
            return
        if path == "/api/openclaw/bridge/status":
            self._handle_openclaw_bridge_status(parsed.query)
            return
        if path == "/api/openclaw/bridge/action":
            self._handle_openclaw_bridge_action()
            return

        self._send_json(404, {"error": "not found"})

    def _send_cors(self, code=200):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-OpenClaw-Bridge-Token, X-Manager-Token")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def _send_json(self, code: int, data: dict):
        if hasattr(self, "_request_logger"):
            self._request_logger.set_status(code)
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-OpenClaw-Bridge-Token, X-Manager-Token")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        request_id = get_request_id()
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True

    def _read_json_body(self) -> dict:
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(payload, dict):
            raise ValueError("json body 必须是对象")
        return payload

    def _bridge_token_from_headers(self) -> str | None:
        token = (self.headers.get("X-OpenClaw-Bridge-Token") or "").strip()
        if token:
            return token
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _management_token_from_headers(self) -> str | None:
        token = (self.headers.get("X-Manager-Token") or "").strip()
        if token:
            return token
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _require_bridge_auth(self, profile: str) -> None:
        require_bridge_token(profile, self._bridge_token_from_headers())

    def _require_management_auth(self) -> None:
        require_management_token(self._management_token_from_headers())

    def _request_meta(self) -> dict:
        return {
            "remoteAddr": self.client_address[0] if self.client_address else None,
            "method": self.command,
            "path": self.path,
            "origin": self.headers.get("Origin"),
            "referer": self.headers.get("Referer"),
            "userAgent": self.headers.get("User-Agent"),
        }

    def _handle_exec(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return

        try:
            req = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        cmd = req.get("cmd", "")
        timeout = min(req.get("timeout", DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)

        if not isinstance(cmd, str) or not cmd.strip():
            self._send_json(400, {"error": "empty cmd"})
            return

        # Whitelist validation: only allow known safe commands
        if not is_exec_command_allowed(cmd):
            self._send_json(403, {"error": "command not in allowed list"})
            return

        try:
            result = run_shell(cmd, timeout_ms=timeout)
            self._send_json(
                200,
                {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
            )
        except subprocess.TimeoutExpired:
            self._send_json(408, {"error": "command timed out"})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_status(self):
        self._send_json(
            200,
            {
                "status": "ok",
                "pid": os.getpid(),
                "managerRoot": str(MANAGER_ROOT),
            },
        )

    def _handle_services_route(self, parsed, path: str):
        """Handle unified services API routes.

        Routes:
          GET  /api/services              - List all services
          GET  /api/services/:id          - Get service details
          POST /api/services/:id/start    - Start service
          POST /api/services/:id/stop     - Stop service
          POST /api/services/:id/restart  - Restart service
          GET  /api/services/:id/logs     - Get service logs
          GET  /api/services/:id/health   - Health check
          GET  /api/services/:id/feishu/status    - Feishu status
          POST /api/services/:id/feishu/config    - Configure Feishu
          POST /api/services/:id/tencent-model/config - Configure Tencent model
        """
        registry = get_registry()
        path_parts = path.strip("/").split("/")
        n_parts = len(path_parts)

        # GET /api/services - List all services
        if n_parts == 2 and self.command == "GET":
            try:
                services = registry.list_all_services()
                self._send_json(200, {
                    "services": [service_to_dict(s) for s in services],
                    "generatedAt": utc_now_iso(),
                })
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        # Routes requiring service ID - need authentication
        if n_parts >= 3:
            service_id = path_parts[2]

            # Require authentication for service detail and action endpoints
            try:
                self._require_management_auth()
            except PermissionError as exc:
                self._send_json(403, {"error": str(exc)})
                return

            # GET /api/services/:id - Get service details
            if n_parts == 3 and self.command == "GET":
                try:
                    detail = registry.get_service(service_id)
                    if detail is None:
                        self._send_json(404, {"error": f"Service not found: {service_id}"})
                        return
                    self._send_json(200, detail_to_dict(detail))
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # POST /api/services/:id/start - Start service
            if n_parts == 4 and path_parts[3] == "start" and self.command == "POST":
                try:
                    result = registry.start_service(service_id)
                    record_manager_action({
                        **self._request_meta(),
                        "scope": "services-action",
                        "serviceId": service_id,
                        "action": "start",
                        "ok": result.success,
                        "returncode": result.returncode,
                    })
                    self._send_json(200 if result.success else 400, action_result_to_dict(result))
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # POST /api/services/:id/stop - Stop service
            if n_parts == 4 and path_parts[3] == "stop" and self.command == "POST":
                try:
                    result = registry.stop_service(service_id)
                    record_manager_action({
                        **self._request_meta(),
                        "scope": "services-action",
                        "serviceId": service_id,
                        "action": "stop",
                        "ok": result.success,
                        "returncode": result.returncode,
                    })
                    self._send_json(200 if result.success else 400, action_result_to_dict(result))
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # POST /api/services/:id/restart - Restart service
            if n_parts == 4 and path_parts[3] == "restart" and self.command == "POST":
                try:
                    result = registry.restart_service(service_id)
                    record_manager_action({
                        **self._request_meta(),
                        "scope": "services-action",
                        "serviceId": service_id,
                        "action": "restart",
                        "ok": result.success,
                        "returncode": result.returncode,
                    })
                    self._send_json(200 if result.success else 400, action_result_to_dict(result))
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # GET /api/services/:id/logs - Get service logs
            if n_parts == 4 and path_parts[3] == "logs" and self.command == "GET":
                try:
                    params = urllib.parse.parse_qs(parsed.query)
                    lines = int((params.get("lines") or ["120"])[0])
                    logs = registry.get_logs(service_id, lines)
                    self._send_json(200, logs_to_dict(logs))
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # GET /api/services/:id/health - Health check
            if n_parts == 4 and path_parts[3] == "health" and self.command == "GET":
                try:
                    health = registry.check_health(service_id)
                    self._send_json(200, health_to_dict(health))
                except ValueError as exc:
                    self._send_json(404, {"error": str(exc)})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return

            # OpenClaw-specific sub-resource routes
            if n_parts >= 5:
                # GET /api/services/:id/feishu/status - Feishu status
                if path_parts[3:5] == ["feishu", "status"] and self.command == "GET":
                    self._handle_service_feishu_status(service_id, parsed.query)
                    return

                # POST /api/services/:id/feishu/config - Configure Feishu
                if path_parts[3:5] == ["feishu", "config"] and self.command == "POST":
                    self._handle_service_feishu_config(service_id)
                    return

                # POST /api/services/:id/tencent-model/config - Configure Tencent model
                if path_parts[3:5] == ["tencent-model", "config"] and self.command == "POST":
                    self._handle_service_tencent_model_config(service_id)
                    return

        self._send_json(404, {"error": "not found"})

    def _handle_service_feishu_status(self, service_id: str, query_string: str):
        """Handle GET /api/services/:id/feishu/status."""
        try:
            profile = self._extract_profile_from_service_id(service_id)
            if profile is None:
                self._send_json(400, {"error": "Feishu status only supported for OpenClaw services"})
                return
            self._send_json(200, get_feishu_qr_session_status(profile, include_output=True))
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_service_feishu_config(self, service_id: str):
        """Handle POST /api/services/:id/feishu/config."""
        payload: dict = {}
        try:
            profile = self._extract_profile_from_service_id(service_id)
            if profile is None:
                self._send_json(400, {"error": "Feishu config only supported for OpenClaw services"})
                return
            payload = self._read_json_body()
            payload["profile"] = profile
            result = apply_feishu_channel_module(payload)
            ok = result.get("status") in {"preview", "applied"}
            body = dict(result)
            if ok and result.get("status") != "preview":
                body["summary"] = openclaw_summary()
            record_manager_action({
                **self._request_meta(),
                "scope": "services-feishu-config",
                "serviceId": service_id,
                "profile": profile,
                "dryRun": bool(result.get("dryRun")),
                "restartAfterSave": bool(result.get("restartAfterSave")),
                "target": result.get("target"),
                "ok": ok,
                "status": result.get("status"),
            })
            self._send_json(200 if ok else 400, body)
        except Exception as exc:
            record_manager_action({
                **self._request_meta(),
                "scope": "services-feishu-config",
                "serviceId": service_id,
                "profile": payload.get("profile"),
                "ok": False,
                "error": str(exc),
            })
            self._send_json(400, {"status": "failed", "error": str(exc)})

    def _handle_service_tencent_model_config(self, service_id: str):
        """Handle POST /api/services/:id/tencent-model/config."""
        payload: dict = {}
        try:
            profile = self._extract_profile_from_service_id(service_id)
            if profile is None:
                self._send_json(400, {"error": "Tencent model config only supported for OpenClaw services"})
                return
            payload = self._read_json_body()
            payload["profile"] = profile
            result = apply_tencent_model_module(payload)
            ok = result.get("status") in {"preview", "applied", "applied_with_probe_failure"}
            body = dict(result)
            if ok and result.get("status") != "preview":
                body["summary"] = openclaw_summary()
            record_manager_action({
                **self._request_meta(),
                "scope": "services-tencent-model-config",
                "serviceId": service_id,
                "profile": profile,
                "primaryModel": result.get("primaryModel"),
                "dryRun": bool(result.get("dryRun")),
                "restartAfterSave": bool(result.get("restartAfterSave")),
                "probeAfterApply": bool(result.get("probeAfterApply")),
                "ok": ok,
                "status": result.get("status"),
            })
            self._send_json(200 if ok else 400, body)
        except Exception as exc:
            record_manager_action({
                **self._request_meta(),
                "scope": "services-tencent-model-config",
                "serviceId": service_id,
                "profile": payload.get("profile"),
                "ok": False,
                "error": str(exc),
            })
            self._send_json(400, {"status": "failed", "error": str(exc)})

    def _extract_profile_from_service_id(self, service_id: str) -> str | None:
        """Extract OpenClaw profile from service ID.

        OpenClaw services have IDs like 'openclaw-default' or 'openclaw-designer'.
        Returns None for non-OpenClaw services.
        """
        if not service_id.startswith("openclaw-"):
            return None
        profile = service_id.removeprefix("openclaw-")
        return normalize_profile(profile)

    def _handle_openclaw_summary(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        try:
            self._send_json(200, openclaw_summary())
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_instances(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        try:
            self._send_json(200, {"instances": list_instances(), "generatedAt": utc_now_iso()})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_instance(self, query_string: str):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        try:
            params = urllib.parse.parse_qs(query_string)
            profile = normalize_profile((params.get("profile") or ["default"])[0])
            self._send_json(200, read_instance(profile))
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_action(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        payload: dict = {}
        try:
            payload = self._read_json_body()
            result = perform_instance_action(payload)
            profile = payload.get("profile")
            action = payload.get("action")
            force_host_managed = _coerce_bool_field(payload, "forceHostManaged") if action == "ensure" else False
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-action",
                    "profile": profile,
                    "action": action,
                    "ok": int(result.get("returncode", 1)) == 0,
                    "returncode": int(result.get("returncode", 1)),
                    "forceHostManaged": force_host_managed,
                }
            )
            self._send_json(
                200 if result.get("returncode", 1) == 0 else 400,
                {
                    "performedAt": utc_now_iso(),
                    **result,
                    "summary": openclaw_summary(),
                },
            )
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-action",
                    "profile": payload.get("profile"),
                    "action": payload.get("action"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_config(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command == "GET":
            try:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                profile = normalize_profile((params.get("profile") or ["default"])[0])
                instance = read_instance(profile)
                self._send_json(200, {"profile": profile, "config": instance["config"], "paths": instance["paths"]})
            except FileNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        try:
            payload = self._read_json_body()
            result = save_config(payload)
            ok = result.get("status") == "saved"
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-save",
                    "profile": payload.get("profile"),
                    "restartAfterSave": bool(result.get("restartAfterSave")),
                    "ok": ok,
                    "status": result.get("status"),
                    "validateReturncode": int((result.get("validate") or {}).get("returncode", 0)),
                }
            )
            self._send_json(200 if ok else 400, result)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-save",
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_logs(self, query_string: str):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        try:
            params = urllib.parse.parse_qs(query_string)
            profile = normalize_profile((params.get("profile") or ["default"])[0])
            lines = int((params.get("lines") or ["120"])[0])
            service_name = service_name_for_profile(profile)
            self._send_json(
                200,
                {
                    "profile": profile,
                    "serviceName": service_name,
                    "lines": lines,
                    "logs": read_service_logs(service_name, lines=lines),
                },
            )
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_feishu_channel_module(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed"})
            return

        payload: dict = {}
        try:
            payload = self._read_json_body()
            result = apply_feishu_channel_module(payload)
            ok = result.get("status") in {"preview", "applied"}
            body = dict(result)
            if ok and result.get("status") != "preview":
                body["summary"] = openclaw_summary()
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-feishu-channel-module",
                    "profile": result.get("profile"),
                    "dryRun": bool(result.get("dryRun")),
                    "restartAfterSave": bool(result.get("restartAfterSave")),
                    "target": result.get("target"),
                    "ok": ok,
                    "status": result.get("status"),
                }
            )
            self._send_json(200 if ok else 400, body)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-feishu-channel-module",
                    "profile": payload.get("profile"),
                    "dryRun": payload.get("dryRun"),
                    "restartAfterSave": payload.get("restartAfterSave"),
                    "ok": False,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            self._send_json(400, {"status": "failed", "error": str(exc)})

    def _handle_openclaw_tencent_model_module(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed"})
            return

        payload: dict = {}
        try:
            payload = self._read_json_body()
            result = apply_tencent_model_module(payload)
            ok = result.get("status") in {"preview", "applied", "applied_with_probe_failure"}
            body = dict(result)
            if ok and result.get("status") != "preview":
                body["summary"] = openclaw_summary()
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-tencent-model-module",
                    "profile": result.get("profile"),
                    "primaryModel": result.get("primaryModel"),
                    "dryRun": bool(result.get("dryRun")),
                    "restartAfterSave": bool(result.get("restartAfterSave")),
                    "probeAfterApply": bool(result.get("probeAfterApply")),
                    "ok": ok,
                    "status": result.get("status"),
                }
            )
            self._send_json(200 if ok else 400, body)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-tencent-model-module",
                    "profile": payload.get("profile"),
                    "primaryModel": payload.get("primaryModel"),
                    "dryRun": payload.get("dryRun"),
                    "restartAfterSave": payload.get("restartAfterSave"),
                    "probeAfterApply": payload.get("probeAfterApply"),
                    "ok": False,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            self._send_json(400, {"status": "failed", "error": str(exc)})

    def _handle_openclaw_feishu_qr_status(self, query_string: str):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "GET":
            self._send_json(405, {"error": "method not allowed"})
            return
        try:
            params = urllib.parse.parse_qs(query_string)
            profile = normalize_profile((params.get("profile") or ["default"])[0])
            self._send_json(200, get_feishu_qr_session_status(profile, include_output=True))
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_feishu_qr_start(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed"})
            return
        payload: dict = {}
        try:
            payload = self._read_json_body()
            profile = normalize_profile(payload.get("profile"))
            verbose = _coerce_bool_field(payload, "verbose", default=True)
            result = start_feishu_qr_session(profile, verbose=verbose)
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-start",
                    "profile": profile,
                    "verbose": verbose,
                    "ok": True,
                    "status": result.get("status"),
                }
            )
            self._send_json(200, result)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-start",
                    "profile": payload.get("profile"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_feishu_qr_input(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed"})
            return
        payload: dict = {}
        try:
            payload = self._read_json_body()
            profile = normalize_profile(payload.get("profile"))
            input_value = payload.get("input")
            if not isinstance(input_value, str) or not input_value:
                raise ValueError("input 不能为空")
            result = send_feishu_qr_input(profile, input_value)
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-input",
                    "profile": profile,
                    "inputLength": len(input_value),
                    "ok": True,
                    "status": result.get("status"),
                }
            )
            self._send_json(200, result)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-input",
                    "profile": payload.get("profile"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_feishu_qr_stop(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method not allowed"})
            return
        payload: dict = {}
        try:
            payload = self._read_json_body()
            profile = normalize_profile(payload.get("profile"))
            result = stop_feishu_qr_session(profile)
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-stop",
                    "profile": profile,
                    "ok": True,
                    "status": result.get("status"),
                    "exitCode": result.get("exitCode"),
                }
            )
            self._send_json(200, result)
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-feishu-qr-stop",
                    "profile": payload.get("profile"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_diagnostics(self):
        try:
            self._require_management_auth()
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return
        try:
            self._send_json(200, build_diagnostics())
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_bridge_status(self, query_string: str):
        try:
            params = urllib.parse.parse_qs(query_string)
            profile = normalize_profile((params.get("profile") or ["default"])[0])
            self._require_bridge_auth(profile)
            self._send_json(200, build_bridge_status(profile))
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_openclaw_bridge_action(self):
        payload: dict = {}
        try:
            payload = self._read_json_body()
            profile = normalize_profile(payload.get("profile"))
            self._require_bridge_auth(profile)
            action = (payload.get("action") or "").strip()
            if action not in {"start", "stop", "restart", "doctor-repair"}:
                raise ValueError(f"bridge action 不支持: {action}")
            result = perform_instance_action({"profile": profile, "action": action})
            body = summarize_bridge_action_result(profile, action, result)
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-bridge-action",
                    "profile": profile,
                    "action": action,
                    "ok": body["returncode"] == 0,
                    "returncode": body["returncode"],
                }
            )
            self._send_json(200 if body["returncode"] == 0 else 400, body)
        except PermissionError as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-bridge-action",
                    "profile": payload.get("profile"),
                    "action": payload.get("action"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-bridge-action",
                    "profile": payload.get("profile"),
                    "action": payload.get("action"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            self._send_json(400, {"error": str(exc)})

    def log_message(self, format, *args):
        if args and "200" not in str(args[0]):
            super().log_message(format, *args)


def _apply_cli_overrides(args: list[str]) -> None:
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            settings.port = int(args[i + 1])
        elif arg == "--host" and i + 1 < len(args):
            settings.host = args[i + 1]
        elif arg == "--bridge-port" and i + 1 < len(args):
            settings.bridge_port = int(args[i + 1])
        elif arg == "--bridge-host" and i + 1 < len(args):
            settings.bridge_host = args[i + 1]
        elif arg == "--disable-bridge":
            settings.docker_bridge_enabled = False


def main() -> None:
    setup_logging()
    _apply_cli_overrides(sys.argv[1:])

    # Ensure management token exists for authentication
    token_path = ensure_management_token()
    logger.info("management_token_ready", path=str(token_path))

    logger.info("server_starting", host=settings.host, port=settings.port)

    server = http.server.ThreadingHTTPServer((settings.host, settings.port), ExecHandler)
    logger.info("server_started", host=settings.host, port=settings.port)

    bridge_server: http.server.ThreadingHTTPServer | None = None
    if settings.docker_bridge_enabled:
        try:
            bridge_server = http.server.ThreadingHTTPServer((settings.bridge_host, settings.bridge_port), ExecHandler)
            bridge_thread = threading.Thread(target=bridge_server.serve_forever, name="openclaw-docker-bridge", daemon=True)
            bridge_thread.start()
            logger.info("bridge_started", host=settings.bridge_host, port=settings.bridge_port)
        except OSError as exc:
            logger.warning("bridge_disabled", error=str(exc))

    def shutdown(sig, frame):
        logger.info("server_shutdown", signal=sig.name if hasattr(sig, 'name') else str(sig))
        if bridge_server is not None:
            bridge_server.server_close()
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()
