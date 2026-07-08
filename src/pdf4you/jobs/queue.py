"""asyncio ベースのジョブキュー。`MAX_CONCURRENCY` 個のワーカーで並行処理を制限する。"""

from __future__ import annotations

import asyncio
import logging

from ..platforms.base import JobCallback, JobRequest

logger = logging.getLogger(__name__)


class JobQueue:
    """PDF処理ジョブを溜め、N並列のワーカーで処理する。

    重い翻訳・推論が詰まらないよう、`concurrency` で同時実行数を制御する。
    """

    def __init__(self, worker_fn: JobCallback, concurrency: int = 1):
        self._queue: asyncio.Queue[JobRequest] = asyncio.Queue()
        self._worker_fn = worker_fn
        self._concurrency = max(1, concurrency)
        self._workers: list[asyncio.Task] = []

    async def enqueue(self, req: JobRequest) -> None:
        await self._queue.put(req)
        logger.info(
            "ジョブ受付: id=%s platform=%s queue=%d",
            req.id,
            req.platform,
            self._queue.qsize(),
        )

    def start(self) -> None:
        """ワーカータスクを起動する（イベントループ内で呼ぶこと）。"""
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._run(i)) for i in range(self._concurrency)]
        logger.info("ジョブワーカーを %d 個起動しました", self._concurrency)

    async def _run(self, index: int) -> None:
        while True:
            req = await self._queue.get()
            logger.info("ワーカー#%d 処理開始: %s", index, req.id)
            try:
                await self._worker_fn(req)
            except Exception:
                logger.exception("ジョブ処理で例外: id=%s", req.id)
            finally:
                self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()
