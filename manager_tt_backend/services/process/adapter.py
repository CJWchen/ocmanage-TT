from __future__ import annotations

import os
import re
import shlex
import signal
from pathlib import Path
from typing import Any

from ...core import (
    HealthCheckResult,
    LogResult,
    ServiceAdapter,
    ServiceInfo,
    ServiceState,
    ServiceStatus,
)
from ...system import list_port_owners, run_shell


class ProcessAdapter(ServiceAdapter):
    """进程服务适配器

    管理通过启动脚本启动的进程。
    通过端口探测状态，日志通过文件路径读取。
    """

    def __init__(
        self,
        name: str,
        *,
        display_name: str | None = None,
        start_script: str | Path,
        stop_script: str | Path | None = None,
        pid_file: str | Path | None = None,
        port: int | None = None,
        log_file: str | Path | None = None,
        profile: str | None = None,
        working_dir: str | Path | None = None,
        env: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self._name = name
        self._display_name = display_name or name
        self._start_script = Path(start_script)
        self._stop_script = Path(stop_script) if stop_script else None
        self._pid_file = Path(pid_file) if pid_file else None
        self._port = port
        self._log_file = Path(log_file) if log_file else None
        self._profile = profile
        self._working_dir = Path(working_dir) if working_dir else None
        self._env = env or {}
        self._extra = extra or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def service_type(self) -> str:
        return "process"

    def _read_pid(self) -> int | None:
        if not self._pid_file or not self._pid_file.exists():
            return None
        try:
            content = self._pid_file.read_text(encoding="utf-8").strip()
            return int(content) if content.isdigit() else None
        except (OSError, ValueError):
            return None

    def _is_pid_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _find_pid_by_script(self) -> int | None:
        script_name = self._start_script.name
        result = run_shell(
            f"pgrep -f {shlex.quote(script_name)} || true",
            timeout_ms=10000,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                if self._is_pid_running(pid):
                    return pid
            except ValueError:
                continue
        return None

    def _probe_port(self) -> bool:
        if not self._port:
            return False
        owners = list_port_owners(self._port)
        return bool(owners)

    def get_status(self) -> ServiceStatus:
        pid = self._read_pid()
        if pid is None:
            pid = self._find_pid_by_script()

        running = False
        if pid and self._is_pid_running(pid):
            running = True
        elif self._port:
            running = self._probe_port()

        if running:
            state = ServiceState.RUNNING
        else:
            state = ServiceState.STOPPED

        return ServiceStatus(
            state=state,
            running=running,
            pid=pid if running else None,
            extra={
                "port": self._port,
                "script": str(self._start_script),
                **self._extra,
            },
        )

    def _run_script(self, script: Path, timeout_ms: int = 60000) -> bool:
        if not script.exists():
            return False
        cmd = str(script)
        cwd = str(self._working_dir) if self._working_dir else None

        env_str = ""
        if self._env:
            env_str = " ".join(
                f"{k}={shlex.quote(v)}" for k, v in self._env.items()
            ) + " "

        result = run_shell(f"{env_str}{cmd}", timeout_ms=timeout_ms, cwd=Path(cwd) if cwd else None)
        return result.returncode == 0

    def start(self) -> bool:
        status = self.get_status()
        if status.running:
            return True
        return self._run_script(self._start_script, timeout_ms=60000)

    def stop(self) -> bool:
        if self._stop_script and self._stop_script.exists():
            return self._run_script(self._stop_script, timeout_ms=30000)

        pid = self._read_pid()
        if pid is None:
            pid = self._find_pid_by_script()

        if pid is None:
            return True

        try:
            os.kill(pid, signal.SIGTERM)
            import time
            for _ in range(30):
                if not self._is_pid_running(pid):
                    break
                time.sleep(0.5)

            if self._is_pid_running(pid):
                os.kill(pid, signal.SIGKILL)
                time.sleep(1)

            return not self._is_pid_running(pid)
        except OSError:
            return True

    def restart(self) -> bool:
        if not self.stop():
            return False
        import time
        time.sleep(1)
        return self.start()

    def get_logs(self, lines: int = 100) -> LogResult:
        if not self._log_file or not self._log_file.exists():
            return LogResult(logs="", lines=0, truncated=False)

        safe_lines = max(1, min(lines, 2000))
        result = run_shell(
            f"tail -n {safe_lines} {shlex.quote(str(self._log_file))}",
            timeout_ms=15000,
        )
        logs = result.stdout or ""
        log_lines = logs.count("\n") if logs else 0
        truncated = log_lines >= safe_lines
        return LogResult(logs=logs, lines=log_lines, truncated=truncated)

    def health_check(self) -> HealthCheckResult:
        status = self.get_status()
        details: dict[str, Any] = {
            "port": self._port,
            "script": str(self._start_script),
        }

        if status.running:
            if self._port:
                owners = list_port_owners(self._port)
                details["port_listening"] = bool(owners)
                if owners:
                    return HealthCheckResult(
                        healthy=True,
                        message=f"进程运行中，端口 {self._port} 正在监听",
                        details=details,
                    )
                return HealthCheckResult(
                    healthy=False,
                    message=f"进程运行中但端口 {self._port} 未监听",
                    details=details,
                )
            return HealthCheckResult(
                healthy=True,
                message="进程运行中",
                details=details,
            )

        return HealthCheckResult(
            healthy=False,
            message="进程未运行",
            details=details,
        )

    def get_info(self) -> ServiceInfo:
        status = self.get_status()
        return ServiceInfo(
            name=self._name,
            display_name=self._display_name,
            service_type=self.service_type,
            profile=self._profile,
            port=self._port,
            config_path=str(self._start_script) if self._start_script.exists() else None,
            state_dir=str(self._pid_file.parent) if self._pid_file else None,
            workspace_dir=str(self._working_dir) if self._working_dir else None,
            status=status,
            extra={
                "start_script": str(self._start_script),
                "stop_script": str(self._stop_script) if self._stop_script else None,
                "pid_file": str(self._pid_file) if self._pid_file else None,
                "log_file": str(self._log_file) if self._log_file else None,
                **self._extra,
            },
        )
