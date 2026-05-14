"""结构化日志模块 - 基于 structlog 的 JSON 格式日志配置

提供:
- request_id、method、path、status_code、duration 的请求日志
- service_id、action、result 的服务操作日志
- error_code、stack trace 的错误日志
- 同时输出到文件和 stdout
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog
from structlog.types import Processor


# Context variables for request tracking
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """获取当前请求 ID"""
    return request_id_ctx.get()


def set_request_id(request_id: str | None = None) -> str:
    """设置请求 ID，如果未提供则生成新的"""
    rid = request_id or str(uuid.uuid4())[:8]
    request_id_ctx.set(rid)
    return rid


def clear_request_id() -> None:
    """清除请求 ID"""
    request_id_ctx.set(None)


def add_request_id(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor: 添加 request_id 到日志"""
    rid = get_request_id()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def rename_event_to_message(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor: 将 'event' 重命名为 'message'"""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def get_log_processors() -> list[Processor]:
    """获取 structlog 处理器链"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_request_id,
        structlog.processors.format_exc_info,
        rename_event_to_message,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]


def get_file_log_processors() -> list[Processor]:
    """获取文件日志处理器链 (更详细)"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_request_id,
        structlog.processors.format_exc_info,
        rename_event_to_message,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]


def setup_logging(
    log_dir: Path | str | None = None,
    log_file: str = "manager-tt.log",
    log_level: str = "INFO",
    enable_file_logging: bool = True,
) -> None:
    """配置 structlog 日志系统

    Args:
        log_dir: 日志目录，默认为 ~/.config/manager-tt/logs
        log_file: 日志文件名
        log_level: 日志级别
        enable_file_logging: 是否启用文件日志
    """
    # 确定 log 目录
    if log_dir is None:
        from ..config import MANAGER_CONFIG_DIR

        log_dir = MANAGER_CONFIG_DIR / "logs"
    elif isinstance(log_dir, str):
        log_dir = Path(log_dir)

    log_dir.mkdir(parents=True, exist_ok=True)

    # 配置 structlog
    structlog.configure(
        processors=get_log_processors(),
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 配置标准库 logging
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 清除现有 handlers
    root_logger.handlers.clear()

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_formatter = logging.Formatter("%(message)s")
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 文件输出
    if enable_file_logging:
        file_path = log_dir / log_file
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter("%(message)s")
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    # 设置第三方库日志级别
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取 structlog logger 实例

    Args:
        name: logger 名称，默认为调用模块名
    """
    return structlog.get_logger(name)


# 请求日志工具函数
def log_request_start(
    logger: structlog.stdlib.BoundLogger,
    method: str,
    path: str,
    remote_addr: str | None = None,
    **extra: Any,
) -> None:
    """记录请求开始"""
    logger.info(
        "request_started",
        method=method,
        path=path,
        remote_addr=remote_addr,
        **extra,
    )


def log_request_end(
    logger: structlog.stdlib.BoundLogger,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    remote_addr: str | None = None,
    **extra: Any,
) -> None:
    """记录请求结束"""
    level = "error" if status_code >= 500 else "warning" if status_code >= 400 else "info"
    getattr(logger, level)(
        "request_completed",
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=round(duration_ms, 2),
        remote_addr=remote_addr,
        **extra,
    )


def log_service_action(
    logger: structlog.stdlib.BoundLogger,
    service_id: str,
    action: str,
    result: str,
    **extra: Any,
) -> None:
    """记录服务操作"""
    logger.info(
        "service_action",
        service_id=service_id,
        action=action,
        result=result,
        **extra,
    )


def log_error(
    logger: structlog.stdlib.BoundLogger,
    error_code: str,
    message: str,
    exc_info: bool = True,
    **extra: Any,
) -> None:
    """记录错误"""
    logger.error(
        "error_occurred",
        error_code=error_code,
        message=message,
        exc_info=exc_info,
        **extra,
    )


class RequestLogger:
    """请求日志上下文管理器"""

    def __init__(
        self,
        logger: structlog.stdlib.BoundLogger,
        method: str,
        path: str,
        remote_addr: str | None = None,
    ):
        self.logger = logger
        self.method = method
        self.path = path
        self.remote_addr = remote_addr
        self.start_time: float = 0
        self.status_code: int = 0

    def __enter__(self) -> "RequestLogger":
        set_request_id()
        self.start_time = time.monotonic()
        log_request_start(self.logger, self.method, self.path, self.remote_addr)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        duration_ms = (time.monotonic() - self.start_time) * 1000
        if exc_type is not None:
            self.status_code = 500
            log_error(
                self.logger,
                error_code=exc_type.__name__ if exc_type else "unknown",
                message=str(exc_val) if exc_val else "Unknown error",
                method=self.method,
                path=self.path,
            )
        log_request_end(
            self.logger,
            self.method,
            self.path,
            self.status_code,
            duration_ms,
            self.remote_addr,
        )
        clear_request_id()

    def set_status(self, status_code: int) -> None:
        """设置响应状态码"""
        self.status_code = status_code
