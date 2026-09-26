"""追加式事件账：写入前校验契约，按事件标识幂等，可从磁盘恢复。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event


class LedgerError(Exception):
    """事件未通过契约校验或账本文件不可用。"""


class EventLedger:
    """只追加的事件账。

    相同 event_id 的重复提交幂等返回 False，不产生新记录；
    指定路径时事件以 JSONL 追加落盘，可用 load 恢复。
    """

    def __init__(self, schema: Mapping[str, Any], path: str | Path | None = None) -> None:
        self._schema = schema
        self._path = Path(path) if path is not None else None
        self._events: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def append(self, event: Mapping[str, Any]) -> bool:
        """校验并追加事件；重复 event_id 返回 False。"""
        issues = validate_event(event, self._schema)
        if issues:
            detail = "；".join(f"{issue.field}:{issue.message}" for issue in issues)
            raise LedgerError(f"事件未通过契约校验：{detail}")
        record = dict(event)
        with self._lock:
            if record["event_id"] in self._ids:
                return False
            self._events.append(record)
            self._ids.add(record["event_id"])
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True

    def events(self) -> list[dict[str, Any]]:
        """按追加顺序返回事件副本。"""
        return list(self._events)

    @classmethod
    def load(cls, schema: Mapping[str, Any], path: str | Path) -> "EventLedger":
        """从 JSONL 文件恢复账本；重复事件标识只保留首条。"""
        ledger = cls(schema, path)
        source = Path(path)
        if not source.exists():
            return ledger
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            issues = validate_event(event, schema)
            if issues:
                detail = "；".join(f"{issue.field}:{issue.message}" for issue in issues)
                raise LedgerError(f"账本事件未通过契约校验：{detail}")
            if event["event_id"] not in ledger._ids:
                ledger._events.append(event)
                ledger._ids.add(event["event_id"])
        return ledger
