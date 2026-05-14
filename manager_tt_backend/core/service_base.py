from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ServiceState(Enum):
    """服务状态枚举"""
    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    STARTING = "starting"
    STOPPING = "stopping"


@dataclass
class ServiceStatus:
    """服务状态数据类"""
    state: ServiceState
    running: bool = False
    pid: int | None = None
    started_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceInfo:
    """服务信息数据类"""
    name: str
    display_name: str
    service_type: str
    profile: str | None = None
    port: int | None = None
    config_path: str | None = None
    state_dir: str | None = None
    workspace_dir: str | None = None
    runtime_mode: str | None = None
    status: ServiceStatus | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthCheckResult:
    """健康检查结果"""
    healthy: bool
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogResult:
    """日志查询结果"""
    logs: str
    lines: int = 0
    truncated: bool = False


class ServiceAdapter(ABC):
    """服务适配器抽象基类

    定义服务管理的标准接口，支持不同类型的服务实现
    (如 systemd 服务、Docker 容器、自定义进程等)。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """返回服务名称"""
        pass

    @property
    @abstractmethod
    def service_type(self) -> str:
        """返回服务类型标识 (如 'openclaw', 'nginx' 等)"""
        pass

    @abstractmethod
    def get_status(self) -> ServiceStatus:
        """获取服务当前状态

        Returns:
            ServiceStatus: 服务状态对象
        """
        pass

    @abstractmethod
    def start(self) -> bool:
        """启动服务

        Returns:
            bool: 启动是否成功
        """
        pass

    @abstractmethod
    def stop(self) -> bool:
        """停止服务

        Returns:
            bool: 停止是否成功
        """
        pass

    @abstractmethod
    def restart(self) -> bool:
        """重启服务

        Returns:
            bool: 重启是否成功
        """
        pass

    @abstractmethod
    def get_logs(self, lines: int = 100) -> LogResult:
        """获取服务日志

        Args:
            lines: 要获取的日志行数

        Returns:
            LogResult: 日志结果对象
        """
        pass

    @abstractmethod
    def health_check(self) -> HealthCheckResult:
        """执行健康检查

        Returns:
            HealthCheckResult: 健康检查结果
        """
        pass

    def get_info(self) -> ServiceInfo:
        """获取服务完整信息

        Returns:
            ServiceInfo: 服务信息对象
        """
        return ServiceInfo(
            name=self.name,
            display_name=self.name,
            service_type=self.service_type,
            status=self.get_status(),
        )

    def is_running(self) -> bool:
        """检查服务是否正在运行

        Returns:
            bool: 服务是否运行中
        """
        return self.get_status().running

    def wait_for_status(
        self,
        target_state: ServiceState,
        timeout_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
    ) -> bool:
        """等待服务达到目标状态

        Args:
            target_state: 目标状态
            timeout_seconds: 超时秒数
            poll_interval_seconds: 轮询间隔秒数

        Returns:
            bool: 是否在超时前达到目标状态
        """
        import time
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            if self.get_status().state == target_state:
                return True
            time.sleep(poll_interval_seconds)
        return False
