"""服务层错误类型。

所有错误都携带稳定的 ``code`` 和中文说明，方便接口层直接透传。
"""

from __future__ import annotations

from typing import Any, Mapping


class ServiceError(Exception):
    """服务层错误基类。"""

    code = "service_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details = dict(details or {})


class PermissionDenied(ServiceError):
    """操作者角色不允许执行该动作。"""

    code = "permission_denied"


class NotFound(ServiceError):
    """引用的对象不存在。"""

    code = "not_found"


class Conflict(ServiceError):
    """与既有记录冲突（如同版本摘要内容变化）。"""

    code = "conflict"


class CapacityExceeded(Conflict):
    """治疗师或场地并发容量不足。"""

    code = "capacity_exceeded"


class StateError(ServiceError):
    """当前状态不允许该动作（如授权已撤回、无待解除暂停）。"""

    code = "state_error"


class ContractViolation(ServiceError):
    """事件不满足领域契约，拒绝入账。"""

    code = "contract_violation"
