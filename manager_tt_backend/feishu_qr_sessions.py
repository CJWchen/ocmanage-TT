from __future__ import annotations

import fcntl
import os
import pty
import re
import shlex
import struct
import subprocess
import threading
import termios
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import HOME, OPENCLAW_BIN, config_path_for_profile, normalize_profile, utc_now_iso
from .feishu_runtime import (
    build_runtime_openclaw_command,
    ensure_feishu_plugin_available,
    ensure_feishu_runtime,
    read_feishu_runtime_context,
)

_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_OUTPUT_CHARS = 120000
_FEISHU_QR_TERM_COLUMNS = 120
_FEISHU_QR_TERM_ROWS = 40


def sanitize_terminal_output(text: str) -> str:
    value = _ANSI_OSC_RE.sub("", text or "")
    value = _ANSI_CSI_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x1b", "")
    return "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def build_feishu_qr_command(profile: str, *, verbose: bool = True, runtime: dict | None = None) -> list[str]:
    command = ["channels", "login", "--channel", "feishu"]
    if verbose:
        command.append("--verbose")
    if runtime and runtime.get("runtimeMode") == "docker":
        return build_runtime_openclaw_command(profile, command, runtime)

    args = [str(OPENCLAW_BIN)]
    if profile != "default":
        args.extend(["--profile", profile])
    args.extend(command)
    return args


def set_pty_window_size(fd: int, *, columns: int = _FEISHU_QR_TERM_COLUMNS, rows: int = _FEISHU_QR_TERM_ROWS) -> None:
    if columns <= 0 or rows <= 0:
        raise ValueError("PTY size must be positive")
    winsize = struct.pack("HHHH", rows, columns, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def build_feishu_qr_env(*, home: Path = HOME, columns: int = _FEISHU_QR_TERM_COLUMNS, rows: int = _FEISHU_QR_TERM_ROWS) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["COLUMNS"] = str(columns)
    env["LINES"] = str(rows)
    env.setdefault("TERM", "xterm-256color")
    return env


def normalize_feishu_qr_input(text: str) -> str:
    # Clack prompts submit on carriage return; browsers often send plain LF.
    return text.replace("\r\n", "\r").replace("\n", "\r")


@dataclass
class FeishuQrSession:
    profile: str
    command: list[str]
    process: subprocess.Popen[bytes]
    master_fd: int
    started_at: str
    output_chunks: deque[str] = field(default_factory=deque)
    output_chars: int = 0
    updated_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    post_sync_status: str | None = None
    post_sync_result: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_active(self) -> bool:
        return self.process.poll() is None and self.exit_code is None

    def append_output(self, chunk: str) -> None:
        cleaned = sanitize_terminal_output(chunk)
        if not cleaned:
            return
        with self.lock:
            self.output_chunks.append(cleaned)
            self.output_chars += len(cleaned)
            self.updated_at = utc_now_iso()
            while self.output_chars > _MAX_OUTPUT_CHARS and self.output_chunks:
                removed = self.output_chunks.popleft()
                self.output_chars -= len(removed)

    def mark_exited(self, exit_code: int | None) -> None:
        with self.lock:
            self.exit_code = exit_code
            self.ended_at = utc_now_iso()
            self.updated_at = self.ended_at

    def write_input(self, text: str) -> None:
        if not self.is_active():
            raise RuntimeError(f"{self.profile} 的 Feishu QR 会话已结束")
        data = normalize_feishu_qr_input(text).encode("utf-8")
        if not data:
            raise ValueError("input 不能为空")
        os.write(self.master_fd, data)

    def stop(self) -> None:
        if not self.is_active():
            return
        try:
            os.write(self.master_fd, b"\x03")
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def snapshot(self, *, include_output: bool = True) -> dict:
        with self.lock:
            polled_exit_code = self.process.poll()
            exit_code = self.exit_code if self.exit_code is not None else polled_exit_code
            active = polled_exit_code is None and exit_code is None
            return {
                "profile": self.profile,
                "active": active,
                "status": "running" if active else ("exited" if exit_code is not None else "idle"),
                "command": shlex.join(self.command),
                "pid": self.process.pid,
                "startedAt": self.started_at,
                "updatedAt": self.updated_at or self.started_at,
                "endedAt": self.ended_at,
                "exitCode": exit_code,
                "postSyncStatus": self.post_sync_status,
                "postSyncResult": self.post_sync_result,
                "output": "".join(self.output_chunks) if include_output else "",
            }


class FeishuQrSessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, FeishuQrSession] = {}

    def _reader_loop(self, session: FeishuQrSession) -> None:
        try:
            while True:
                chunk = os.read(session.master_fd, 4096)
                if not chunk:
                    break
                session.append_output(chunk.decode("utf-8", errors="replace"))
        except OSError:
            pass
        finally:
            exit_code = session.process.wait()
            session.mark_exited(exit_code)
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    def _ensure_post_exit_sync(self, session: FeishuQrSession) -> None:
        with session.lock:
            if session.exit_code != 0 or session.post_sync_status in {"running", "done", "failed"}:
                return
            session.post_sync_status = "running"
            session.updated_at = utc_now_iso()

        session.append_output("\n[manager-tt] Feishu QR 已结束，正在同步运行态...\n")
        try:
            result = ensure_feishu_runtime(session.profile, restart_gateway=True)
            runtime = result.get("feishuRuntime") if isinstance(result.get("feishuRuntime"), dict) else {}
            lines = []
            for action in result.get("actions") or []:
                lines.append(f"[manager-tt] {action}")
            if runtime.get("ready"):
                lines.append("[manager-tt] Feishu 插件已加载，channel 已启动并 ready。")
            elif runtime.get("status"):
                lines.append(f"[manager-tt] Feishu 运行态状态: {runtime['status']}")
            for issue in (runtime.get("issues") or [])[:3]:
                lines.append(f"[manager-tt] {issue}")
            if lines:
                session.append_output("\n" + "\n".join(lines) + "\n")
            with session.lock:
                session.post_sync_status = "done"
                session.post_sync_result = result
                session.updated_at = utc_now_iso()
        except Exception as exc:
            session.append_output(f"\n[manager-tt] Feishu 运行态同步失败: {exc}\n")
            with session.lock:
                session.post_sync_status = "failed"
                session.post_sync_result = {
                    "performedAt": utc_now_iso(),
                    "profile": session.profile,
                    "status": "failed",
                    "error": str(exc),
                }
                session.updated_at = utc_now_iso()

    def start(self, profile: str, *, verbose: bool = True) -> dict:
        normalized_profile = normalize_profile(profile)
        path = config_path_for_profile(normalized_profile)
        if not path.exists():
            raise FileNotFoundError(f"实例配置不存在: {path}")
        if not OPENCLAW_BIN.exists():
            raise FileNotFoundError(f"缺少可执行文件: {OPENCLAW_BIN}")
        runtime_context = read_feishu_runtime_context(normalized_profile)
        ensure_feishu_plugin_available(normalized_profile, runtime=runtime_context)

        with self._lock:
            existing = self._sessions.get(normalized_profile)
            if existing and existing.is_active():
                raise RuntimeError(f"{normalized_profile} 已有正在运行的 Feishu QR 会话")
            if existing and not existing.is_active():
                self._sessions.pop(normalized_profile, None)

            master_fd, slave_fd = pty.openpty()
            command = build_feishu_qr_command(normalized_profile, verbose=verbose, runtime=runtime_context)
            try:
                set_pty_window_size(slave_fd)
                process = subprocess.Popen(
                    command,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(HOME),
                    env=build_feishu_qr_env(),
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception:
                os.close(master_fd)
                os.close(slave_fd)
                raise
            os.close(slave_fd)
            session = FeishuQrSession(
                profile=normalized_profile,
                command=command,
                process=process,
                master_fd=master_fd,
                started_at=utc_now_iso(),
            )
            self._sessions[normalized_profile] = session
            thread = threading.Thread(target=self._reader_loop, args=(session,), daemon=True)
            thread.start()
            return session.snapshot(include_output=True)

    def status(self, profile: str, *, include_output: bool = True) -> dict:
        normalized_profile = normalize_profile(profile)
        with self._lock:
            session = self._sessions.get(normalized_profile)
        if not session:
            return {
                "profile": normalized_profile,
                "active": False,
                "status": "idle",
                "command": shlex.join(build_feishu_qr_command(normalized_profile)),
                "pid": None,
                "startedAt": None,
                "updatedAt": None,
                "endedAt": None,
                "exitCode": None,
                "output": "" if include_output else "",
            }
        snapshot = session.snapshot(include_output=include_output)
        if snapshot.get("exitCode") == 0 and snapshot.get("postSyncStatus") not in {"done", "failed"}:
            self._ensure_post_exit_sync(session)
        snapshot = session.snapshot(include_output=include_output)
        if not include_output:
            snapshot.pop("output", None)
        return snapshot

    def stop(self, profile: str) -> dict:
        normalized_profile = normalize_profile(profile)
        with self._lock:
            session = self._sessions.get(normalized_profile)
        if not session:
            return self.status(normalized_profile, include_output=True)
        session.stop()
        return session.snapshot(include_output=True)

    def send_input(self, profile: str, text: str) -> dict:
        normalized_profile = normalize_profile(profile)
        with self._lock:
            session = self._sessions.get(normalized_profile)
        if not session:
            raise RuntimeError(f"{normalized_profile} 没有正在运行的 Feishu QR 会话")
        session.write_input(text)
        return session.snapshot(include_output=True)

    def ensure_profile_unlocked(self, profile: str, *, scope: str = "修改配置") -> None:
        status = self.status(profile, include_output=False)
        if status.get("active"):
            raise RuntimeError(f"{normalize_profile(profile)} 正在进行 Feishu QR 会话，会话结束前不能{scope}")


_MANAGER = FeishuQrSessionManager()


def start_feishu_qr_session(profile: str, *, verbose: bool = True) -> dict:
    return _MANAGER.start(profile, verbose=verbose)


def get_feishu_qr_session_status(profile: str, *, include_output: bool = True) -> dict:
    return _MANAGER.status(profile, include_output=include_output)


def get_feishu_qr_session_summary(profile: str) -> dict:
    return _MANAGER.status(profile, include_output=False)


def stop_feishu_qr_session(profile: str) -> dict:
    return _MANAGER.stop(profile)


def send_feishu_qr_input(profile: str, text: str) -> dict:
    return _MANAGER.send_input(profile, text)


def ensure_feishu_qr_session_unlocked(profile: str, *, scope: str = "修改配置") -> None:
    _MANAGER.ensure_profile_unlocked(profile, scope=scope)
