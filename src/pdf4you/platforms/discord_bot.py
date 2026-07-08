"""Discord アダプタ（discord.py）。

監視チャンネルへの PDF 添付を検知し、その投稿からスレッドを作成して返信する。
必要: Bot に「Message Content Intent」を有効化。権限: メッセージ送信・スレッド作成・
ファイル添付。
"""

from __future__ import annotations

import logging
import uuid

import discord

from ..config import Settings
from .base import JobCallback, JobRequest, PlatformAdapter

logger = logging.getLogger(__name__)

_DISCORD_LIMIT = 1900  # メッセージ2000字制限に対する安全マージン


def _split(text: str, limit: int = _DISCORD_LIMIT) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


class DiscordAdapter(PlatformAdapter):
    name = "discord"

    def __init__(self, settings: Settings, on_job: JobCallback):
        self._settings = settings
        self._on_job = on_job
        self._watch = settings.discord_watch_channel_set
        self._allowed = settings.discord_allowed_user_set
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._threads: dict[str, discord.abc.Messageable] = {}
        self._register()

    def is_allowed(self, user_id: str) -> bool:
        return not self._allowed or user_id in self._allowed

    def _register(self) -> None:
        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._handle_message(message)

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        channel_id = str(message.channel.id)
        # スレッド内メッセージの場合は親チャンネルIDで監視判定
        parent_id = (
            str(message.channel.parent_id)
            if isinstance(message.channel, discord.Thread)
            else channel_id
        )
        if self._watch and parent_id not in self._watch and channel_id not in self._watch:
            return

        pdfs = [a for a in message.attachments if a.filename.lower().endswith(".pdf")]
        if not pdfs:
            return

        user_id = str(message.author.id)
        if not self.is_allowed(user_id):
            logger.info("許可されていないユーザーからの投稿を無視: %s", user_id)
            return

        for att in pdfs:
            if att.size and att.size > self._settings.max_pdf_bytes:
                await message.reply(
                    f"⚠️ サイズ上限（{self._settings.max_pdf_mb}MB）を超えています。"
                )
                continue
            thread = await self._ensure_thread(message, att.filename)
            req = JobRequest(
                id=uuid.uuid4().hex[:12],
                platform=self.name,
                channel_id=channel_id,
                thread_ref=str(thread.id),
                user_id=user_id,
                filename=att.filename,
                file_url=att.url,
                file_size=att.size,
                meta={"thread": thread, "attachment": att},
            )
            await self._on_job(req)

    async def _ensure_thread(self, message, filename):
        if isinstance(message.channel, discord.Thread):
            self._threads[str(message.channel.id)] = message.channel
            return message.channel
        thread = await message.create_thread(name=f"翻訳: {filename[:80]}")
        self._threads[str(thread.id)] = thread
        return thread

    def _dest(self, req):
        return req.meta.get("thread") or self._threads.get(req.thread_ref)

    async def download(self, req, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        att = req.meta.get("attachment")
        if att is not None:
            await att.save(dest)
            return dest
        # フォールバック: 添付URLから取得（attachment url は認証不要）
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(req.file_url) as resp:
                resp.raise_for_status()
                dest.write_bytes(await resp.read())
        return dest

    async def post_text(self, req, text):
        thread = self._dest(req)
        for chunk in _split(text):
            await thread.send(chunk)

    async def upload_file(self, req, path, *, title, comment=""):
        thread = self._dest(req)
        if comment:
            await thread.send(comment)
        await thread.send(file=discord.File(str(path), filename=title))

    async def start(self):
        logger.info("Discordアダプタを起動します")
        await self._client.start(self._settings.discord_bot_token)
