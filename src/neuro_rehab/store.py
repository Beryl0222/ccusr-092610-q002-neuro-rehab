"""仅追加的事件存储。

事件是唯一的真相来源：写入前按领域契约校验，持久化为 JSONL，
服务重启后通过重放恢复全部状态。存储不提供任何修改或删除入口，
保证既往结论不可被追改。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import ContractViolation

Validator = Callable[[Mapping[str, Any]], list]


class EventStore:
    """线程安全的仅追加事件日志。"""

    def __init__(self, path: str | Path | None = None, validator: Validator | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._validator = validator
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        if self._path is not None and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """校验并追加事件，返回已入账的事件副本。"""
        if self._validator is not None:
            issues = self._validator(event)
            if issues:
                raise ContractViolation(
                    "事件不满足领域契约，拒绝入账",
                    details={"issues": [vars(issue) if not isinstance(issue, Mapping) else dict(issue) for issue in issues]},
                )
        record = dict(event)
        with self._lock:
            self._events.append(record)
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return dict(record)

    def replay(self) -> list[dict[str, Any]]:
        """按入账顺序返回全部事件副本。"""
        with self._lock:
            return [dict(event) for event in self._events]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
