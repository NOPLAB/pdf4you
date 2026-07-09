"""ジョブの受付・実行タスク管理と、ローカル翻訳の同時実行ゲート。

方針: ローカル翻訳（pdf2zh-next）は CPU/GPU を食うので同時実行を絞りたいが、
受付・ダウンロード・抽出・要約、そして外部サービス翻訳（API を叩くだけ）は
絞る必要がない。そこで「翻訳スロットだけ」を `TranslationGate` で制限し、
`JobQueue` は各ジョブを即タスク起動してライフサイクルだけを見る。

これにより、キューで沈黙させずに受付直後から進捗・切替ボタンを出せる。待機中の
ジョブは `TranslationGate.acquire` をキャンセルして待機列から離脱し（＝外部
サービスへスキップ）、ローカルの順番待ちを飛ばせる。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..platforms.base import JobCallback, JobRequest

logger = logging.getLogger(__name__)

# 待機順位（1始まり。1が「次に実行される」）を待機ジョブへ通知するコールバック。
PositionCallback = Callable[[int], Awaitable[None]]


class TranslationGate:
    """ローカル翻訳の同時実行数を制限するゲート。

    容量ぶんだけ同時にローカル翻訳を走らせ、あふれた分は FIFO で待機させる。
    待機順が変わるたびにコールバックへ通知し、待機中のジョブはキャンセルで
    待機列から離脱できる（外部サービスへのスキップに使う）。

    セマフォではなく自前実装なのは、「今何番目で待っているか」を待機ジョブへ
    通知し、離脱時に順位を繰り上げ直すため。
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._active = 0
        # 待機中のジョブ: [(future, on_position)]。先頭ほど先に実行される（FIFO）。
        self._waiters: list[tuple[asyncio.Future[None], PositionCallback | None]] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, on_position: PositionCallback | None = None) -> None:
        """ローカル翻訳スロットを1つ確保する。空きが無ければ FIFO で待つ。

        待機に入ると `on_position(順位)` を即時通知し、以後は前のジョブが
        完了/離脱して順位が繰り上がるたびに再通知する（順位は1始まり）。
        待機中にこの coroutine がキャンセルされると、待機列から安全に離脱する。
        """
        # 空きがあり、割り込み対象の待機者もいなければ即取得（FIFO 公平性を保つ）。
        if self._active < self._capacity and not self._waiters:
            self._active += 1
            return

        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((fut, on_position))
        await self._notify_positions()
        try:
            await fut
        except asyncio.CancelledError:
            # 待機中に離脱（外部サービスへスキップ等）。ただし release の admit が
            # ちょうどこの fut を選んだ直後にキャンセルが来ることがある。その場合は
            # スロットを確保済み扱いなので、離脱時にちゃんと返して次へ譲る。
            still_waiting = any(f is fut for f, _ in self._waiters)
            self._waiters = [(f, cb) for (f, cb) in self._waiters if f is not fut]
            if still_waiting:
                await self._notify_positions()
            else:
                # 既に admit（スロット確保）されていた → 返却＝次の順番待ちを繰り上げる。
                await self.release()
            raise

    async def release(self) -> None:
        """確保したスロットを1つ返し、待機先頭を繰り上げる。

        取得していないのに呼んでも安全（`active` は 0 未満にならない）。
        """
        if self._active > 0:
            self._active -= 1
        while self._waiters and self._active < self._capacity:
            fut, _cb = self._waiters.pop(0)
            if fut.cancelled():
                continue
            self._active += 1
            fut.set_result(None)
        await self._notify_positions()

    async def _notify_positions(self) -> None:
        """待機中の各ジョブへ現在の順位を通知する（ベストエフォート）。"""
        for i, (fut, cb) in enumerate(self._waiters):
            if cb is None or fut.done():
                continue
            try:
                await cb(i + 1)
            except Exception:
                logger.debug("待機順位の通知に失敗しました", exc_info=True)


class JobQueue:
    """PDF処理ジョブを受け取り、それぞれを独立タスクとして即実行する。

    ローカル翻訳の同時実行制限は `TranslationGate`（パイプライン側）が担うため、
    ここでは受付を待たせず、起動したタスクの追跡と一括停止だけを行う。
    """

    def __init__(self, worker_fn: JobCallback):
        self._worker_fn = worker_fn
        self._tasks: set[asyncio.Task] = set()

    async def enqueue(self, req: JobRequest) -> None:
        task = asyncio.create_task(self._run(req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "ジョブ受付: id=%s platform=%s running=%d",
            req.id,
            req.platform,
            len(self._tasks),
        )

    async def _run(self, req: JobRequest) -> None:
        try:
            await self._worker_fn(req)
        except Exception:
            logger.exception("ジョブ処理で例外: id=%s", req.id)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
