"""ユーザー別 API キーの保管庫（aiosqlite + Fernet 暗号化）。

DM のスラッシュコマンドで登録されたキーを、プラットフォーム（discord/slack）と
ユーザーIDごとに保存する。キーは秘匿情報なので Fernet で暗号化して格納し、平文は
DB に残さない。暗号鍵（Fernet 鍵）は `Settings.secret_key` から供給する。
鍵が無い場合はそもそも `UserKeyStore` を生成しない（呼び出し側でガードする）。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_keys (
    platform    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    api_key_enc BLOB NOT NULL,
    model       TEXT,
    base_url    TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (platform, user_id)
);
"""


@dataclass(frozen=True)
class StoredKey:
    """保存済みのキー情報（復号済み）。"""

    api_key: str
    model: str | None
    base_url: str | None = None


def mask_key(api_key: str) -> str:
    """`/keystatus` 等での表示用にキーをマスクする（先頭数文字＋末尾4文字）。"""
    if len(api_key) <= 10:
        return "•" * len(api_key)
    return f"{api_key[:6]}…{api_key[-4:]}"


class UserKeyStore:
    """プラットフォーム×ユーザーIDごとに暗号化した API キーを保存する。

    `Fernet(secret_key)` で暗号化する。`secret_key` が不正な場合は生成時に
    例外を送出するので、起動時に検知できる。
    """

    def __init__(self, db_path: Path, secret_key: str):
        self._db_path = Path(db_path)
        # 不正な鍵はここで ValueError になる（起動時に気づける）。
        self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        self._ready = False

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self._db_path)
        if not self._ready:
            await db.executescript(_SCHEMA)
            # 旧スキーマ（base_url カラムなし）からの移行。
            async with db.execute("PRAGMA table_info(user_keys)") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if "base_url" not in columns:
                await db.execute("ALTER TABLE user_keys ADD COLUMN base_url TEXT")
            await db.commit()
            self._ready = True
        return db

    async def init(self) -> None:
        """テーブルを作成しておく（起動時に一度呼ぶと以後の初回遅延がない）。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await self._connect()
        await db.close()

    async def set_key(
        self,
        platform: str,
        user_id: str,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """キー（と任意のモデル・Base URL）を暗号化して保存/更新する。"""
        enc = self._fernet.encrypt(api_key.encode())
        now = dt.datetime.now(dt.UTC).isoformat()
        db = await self._connect()
        try:
            await db.execute(
                "INSERT INTO user_keys "
                "(platform, user_id, api_key_enc, model, base_url, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, user_id) DO UPDATE SET "
                "api_key_enc=excluded.api_key_enc, model=excluded.model, "
                "base_url=excluded.base_url, updated_at=excluded.updated_at",
                (platform, user_id, enc, model or None, base_url or None, now),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_key(self, platform: str, user_id: str) -> StoredKey | None:
        """復号したキー情報を返す。未登録なら None。"""
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT api_key_enc, model, base_url FROM user_keys WHERE platform=? AND user_id=?",
                (platform, user_id),
            ) as cur:
                row = await cur.fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        try:
            api_key = self._fernet.decrypt(row[0]).decode()
        except InvalidToken:
            # SECRET_KEY を変更した等で復号できない場合。壊れたレコードは無視する。
            logger.warning(
                "キーの復号に失敗しました（SECRET_KEY 変更の可能性）: %s/%s", platform, user_id
            )
            return None
        return StoredKey(api_key=api_key, model=row[1], base_url=row[2])

    async def delete_key(self, platform: str, user_id: str) -> bool:
        """キーを削除する。削除したら True、元々無ければ False。"""
        db = await self._connect()
        try:
            cur = await db.execute(
                "DELETE FROM user_keys WHERE platform=? AND user_id=?",
                (platform, user_id),
            )
            await db.commit()
            return cur.rowcount > 0
        finally:
            await db.close()

    async def has_key(self, platform: str, user_id: str) -> bool:
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT 1 FROM user_keys WHERE platform=? AND user_id=?",
                (platform, user_id),
            ) as cur:
                return await cur.fetchone() is not None
        finally:
            await db.close()
