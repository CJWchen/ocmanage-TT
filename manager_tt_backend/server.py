from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.parse

from .actions import apply_tencent_model_module, perform_instance_action, save_config
from .config import (
    DEFAULT_TIMEOUT_MS,
    EXEC_DANGEROUS_PATTERNS,
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
    summarize_bridge_action_result,
)
from .system import read_service_logs, run_shell


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

        if path == "/cgi-bin/exec":
            self._handle_exec()
            return
        if path == "/cgi-bin/status":
            self._handle_status()
            return
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
        if path == "/api/openclaw/config/tencent-coding-plan":
            self._handle_openclaw_tencent_model_module()
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
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-OpenClaw-Bridge-Token")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def _send_json(self, code: int, data: dict):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-OpenClaw-Bridge-Token")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
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

    def _require_bridge_auth(self, profile: str) -> None:
        require_bridge_token(profile, self._bridge_token_from_headers())

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
            req = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        cmd = req.get("cmd", "")
        timeout = min(req.get("timeout", DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)

        if not isinstance(cmd, str) or not cmd.strip():
            self._send_json(400, {"error": "empty cmd"})
            return

        lowered = cmd.lower()
        for pattern in EXEC_DANGEROUS_PATTERNS:
            if pattern in lowered:
                self._send_json(403, {"error": f"command rejected: {pattern}"})
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

    def _handle_openclaw_summary(self):
        try:
            self._send_json(200, openclaw_summary())
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_instances(self):
        try:
            self._send_json(200, {"instances": list_instances(), "generatedAt": utc_now_iso()})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_instance(self, query_string: str):
        try:
            params = urllib.parse.parse_qs(query_string)
            profile = normalize_profile((params.get("profile") or ["default"])[0])
            self._send_json(200, read_instance(profile))
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def _handle_openclaw_action(self):
        payload: dict = {}
        try:
            payload = self._read_json_body()
            result = perform_instance_action(payload)
            profile = payload.get("profile")
            action = payload.get("action")
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-action",
                    "profile": profile,
                    "action": action,
                    "ok": int(result.get("returncode", 1)) == 0,
                    "returncode": int(result.get("returncode", 1)),
                    "forceHostManaged": bool(payload.get("forceHostManaged")),
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
            record_manager_action(
                {
                    **self._request_meta(),
                    "scope": "openclaw-config-save",
                    "profile": payload.get("profile"),
                    "restartAfterSave": bool(payload.get("restartAfterSave")),
                    "ok": True,
                    "validateReturncode": int((result.get("validate") or {}).get("returncode", 0)),
                }
            )
            self._send_json(200, result)
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

    def _handle_openclaw_tencent_model_module(self):
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

    def _handle_openclaw_diagnostics(self):
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
    _apply_cli_overrides(sys.argv[1:])

    server = http.server.ThreadingHTTPServer((settings.host, settings.port), ExecHandler)
    print(f"[launcher] Server running on http://{settings.host}:{settings.port}")

    bridge_server: http.server.ThreadingHTTPServer | None = None
    if settings.docker_bridge_enabled:
        try:
            bridge_server = http.server.ThreadingHTTPServer((settings.bridge_host, settings.bridge_port), ExecHandler)
            bridge_thread = threading.Thread(target=bridge_server.serve_forever, name="openclaw-docker-bridge", daemon=True)
            bridge_thread.start()
            print(f"[launcher] Docker bridge running on http://{settings.bridge_host}:{settings.bridge_port}")
        except OSError as exc:
            print(f"[launcher] Docker bridge disabled: {exc}", file=sys.stderr)

    def shutdown(sig, frame):
        print("\n[launcher] Shutting down...")
        if bridge_server is not None:
            bridge_server.server_close()
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()
