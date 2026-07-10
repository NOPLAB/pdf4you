"""処理中ジョブの永続化（aiosqlite）。再起動時の翻訳再開に使う。

受け付けたジョブを SQLite に記録し、処理が終わったら（成功・失敗を問わず）削除する。
プロセスが途中で落ちるとレコードが残るため、次回起動時に残っているレコードを
`JobRequest` に復元して再投入する＝翻訳を最初からやり直す（pdf2zh-next は途中
再開できないため、「再開」はジョブ単位のやり直しを意味する）。

要約は投稿済みかどうかを `summary_posted` に記録し、再開時の二重投稿を防ぐ。
`JobRequest.meta` のライブオブジェクト（Discord の thread 等）は永続化できないので、
復元後はアダプタ側が ID から再解決する。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..platforms.base import JobRequest

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    channel_id     TEXT NOT NULL,
    thread_ref     TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    filename       TEXT NOT NULL,
    file_url       TEXT NOT NULL,
    file_size      INTEGER NOT NULL,
    summary_posted INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PersistedJob:
    """SQLite から読み出した中断ジョブ。"""

    id: str
    platform: str
    channel_id: str
    thread_ref: str
    user_id: str
    filename: str
    file_url: str
    file_size: int
    summary_posted: bool

    def to_request(self) -> JobRequest:
        """再開用の `JobRequest` に復元する。

        `meta` に再開フラグと要約投稿済みフラグを載せ、パイプラインが
        「ダウンロード済みファイルの再利用」「要約のスキップ」を判断できるようにする。
        """
        return JobRequest(
            id=self.id,
            platform=self.platform,
            channel_id=self.channel_id,
            thread_ref=self.thread_ref,
            user_id=self.user_id,
            filename=self.filename,
            file_url=self.file_url,
            file_size=self.file_size,
            meta={"resumed": True, "summary_posted": self.summary_posted},
        )


class JobStateStore:
    """処理中ジョブを SQLite に記録する保管庫。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._ready = False

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self._db_path)
        if not self._ready:
            await db.executescript(_SCHEMA)
            await db.commit()
            self._ready = True
        return db

    async def init(self) -> None:
        """テーブルを作成しておく（起動時に一度呼ぶ）。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await self._connect()
        await db.close()

    async def add(self, req: JobRequest) -> None:
        """受け付けたジョブを記録する（同じ id なら上書き）。"""
        now = dt.datetime.now(dt.UTC).isoformat()
        db = await self._connect()
        try:
            await db.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, platform, channel_id, thread_ref, user_id, filename, "
                " file_url, file_size, summary_posted, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    req.id,
                    req.platform,
                    req.channel_id,
                    req.thread_ref,
                    req.user_id,
                    req.filename,
                    req.file_url,
                    req.file_size,
                    now,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def mark_summary_posted(self, job_id: str) -> None:
        """要約を投稿済みとして記録する（再開時の二重投稿防止）。"""
        db = await self._connect()
        try:
            await db.execute("UPDATE jobs SET summary_posted=1 WHERE id=?", (job_id,))
            await db.commit()
        finally:
            await db.close()

    async def remove(self, job_id: str) -> None:
        """処理が終わったジョブの記録を消す（成功・失敗を問わず呼ぶ）。"""
        db = await self._connect()
        try:
            await db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            await db.commit()
        finally:
            await db.close()

    async def list_pending(self) -> list[PersistedJob]:
        """残っている（＝前回中断された）ジョブを受付順に返す。"""
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT id, platform, channel_id, thread_ref, user_id, filename, "
                "file_url, file_size, summary_posted FROM jobs ORDER BY created_at"
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()
        return [
            PersistedJob(
                id=r[0],
                platform=r[1],
                channel_id=r[2],
                thread_ref=r[3],
                user_id=r[4],
                filename=r[5],
                file_url=r[6],
                file_size=r[7],
                summary_posted=bool(r[8]),
            )
            for r in rows
        ]
