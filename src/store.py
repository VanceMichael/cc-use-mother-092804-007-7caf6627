"""仅追加 JSONL 事件存储。

事件按序号递增写入，并通过 prev_hash 形成哈希链：任何对历史事件的改写
都会在回放时被发现。读取全部事件即可重建台账状态。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterator

from .models import Event, EVENT_TYPES


def _digest(event: dict) -> str:
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: Event) -> Event:
        if event.type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型: {event.type}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        seq = 0
        prev_hash = ""
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    seq = max(seq, raw["seq"])
                    prev_hash = raw["hash"]
        seq += 1
        stored = event.to_dict()
        stored["seq"] = seq
        if stored["ts"] is None:
            import time

            stored["ts"] = time.time()
        stored["prev_hash"] = prev_hash
        stored["hash"] = _digest({k: stored[k] for k in ("id", "seq", "ts", "type", "actor", "payload", "prev_hash")})
        # 原子追加：先写临时文件再并入，避免半行污染日志。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return Event.from_dict(stored)

    def replay(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        prev_hash = ""
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                raw = json.loads(line)
                expected_prev = raw.get("prev_hash", "")
                if expected_prev != prev_hash:
                    raise ValueError(f"事件链断裂（第{line_no}行）：历史事件可能被改写")
                body = {k: raw[k] for k in ("id", "seq", "ts", "type", "actor", "payload", "prev_hash")}
                if _digest(body) != raw["hash"]:
                    raise ValueError(f"事件校验失败（第{line_no}行）：事件内容与哈希不符")
                prev_hash = raw["hash"]
                yield Event.from_dict(raw)

    def exists(self) -> bool:
        return self.path.exists()
