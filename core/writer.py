"""异步落盘：JSONL 追加与 JSON 原子写入。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import StorageConfig


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), default=str)
    os.replace(temp, path)


def append_line(path: Path, line: str, compress: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compress:
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
        return
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{line}\n")


def serialize_record(record: Any) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


class Writer:
    """单消费者写队列，保证同一文件的写入顺序。"""

    def __init__(
        self,
        root: Path,
        config: StorageConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._root = Path(root)
        self._config = config
        self._logger = logger or logging.getLogger("astrbot")
        self._queue: asyncio.Queue[tuple[str, str, Any] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._closed = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._closed = True
        self._queue.put_nowait(None)
        try:
            await self._task
        finally:
            self._task = None

    def path_for(self, relative: str) -> Path:
        return self._root / relative

    def append(self, relative: str, record: Any) -> None:
        self._enqueue(("line", relative, record))

    def write_json(self, relative: str, data: Any) -> None:
        self._enqueue(("json", relative, data))

    async def flush(self) -> None:
        await self._queue.join()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _enqueue(self, item: tuple[str, str, Any]) -> None:
        if self._closed:
            self._write_now(item)
            return
        self._queue.put_nowait(item)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await asyncio.to_thread(self._write_now, item)
            finally:
                self._queue.task_done()

    def _write_now(self, item: tuple[str, str, Any]) -> None:
        kind, relative, payload = item
        path = self._root / relative
        try:
            if kind == "line":
                append_line(path, serialize_record(payload), self._config.compress_jsonl)
            else:
                write_json_atomic(path, payload)
        except Exception as exc:
            self._logger.error("[llm_backup] 写入 %s 失败: %s", path, exc)
