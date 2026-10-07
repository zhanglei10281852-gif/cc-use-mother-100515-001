"""事件存储：只增不改的 JSONL 日志 + 启动时哈希链校验。

- 追加写：每条事件一行，落盘后 fsync，崩溃最多损失最后一条未落盘事件。
- 重启恢复：打开存储时全量重放并校验哈希链，投影（内存状态）由事件重建，
  因此未完成的审批、待归并的冲突等都会自然恢复。
- ":memory:" 路径用于测试。
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import threading

from .events import GENESIS_HASH, canonical, make_event, verify_chain
from .domain import DomainError


class EventStore:
    """只增不改的事件日志。"""

    def __init__(self, path: "str | Path"):
        self._lock = threading.RLock()
        self._memory = str(path) == ":memory:"
        self._path = None if self._memory else Path(path)
        self._events: list[dict] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                self._load()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _load(self) -> None:
        events: list[dict] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise DomainError(
                        "store_corrupt", f"事件日志第 {lineno} 行无法解析: {exc}"
                    ) from exc
        try:
            verify_chain(events)
        except ValueError as exc:
            raise DomainError("store_corrupt", str(exc)) from exc
        self._events = events

    def events(self) -> list[dict]:
        """返回全部事件的副本（按 seq 升序）。"""
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def head_hash(self) -> str:
        with self._lock:
            return self._events[-1]["hash"] if self._events else GENESIS_HASH

    # ------------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------------

    def append(
        self,
        event: str,
        actor: str,
        payload: dict,
        ts: str,
        idem: str | None = None,
        result: dict | None = None,
    ) -> dict:
        """追加一条事件并落盘，返回完整事件记录。"""
        with self._lock:
            seq = len(self._events) + 1
            record = make_event(
                seq=seq,
                ts=ts,
                event=event,
                actor=actor,
                payload=payload,
                prev_hash=self.head_hash,
                idem=idem,
                result=result,
            )
            if self._path is not None:
                line = canonical(record)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._events.append(record)
            return record

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def verify(self) -> bool:
        """重新校验整条哈希链（供 verify 命令/接口使用）。"""
        with self._lock:
            verify_chain(self._events)
        return True
