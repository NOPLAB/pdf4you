"""Slack アダプタ（Bolt / Socket Mode）。

監視チャンネルへの PDF 添付を検知して JobRequest を発行し、スレッド（thread_ts）へ
テキスト・ファイルを投稿する。必要スコープ: `files:read`, `chat:write`,
`channels:history`（対象チャンネル種別に応じて groups/im/mpim も）。
"""

from __future__ import annotations

import logging
import uuid

import aiohttp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from ..config import Settings
from .base import JobCallback, JobRequest, PlatformAdapter

logger = logging.getLogger(__name__)


class SlackAdapter(PlatformAdapter):
    name = "slack"

    def __init__(self, settings: Settings, on_job: JobCallback):
        self._settings = settings
        self._on_job = on_job
        self._app = AsyncApp(token=settings.slack_bot_token)
        self._handler = AsyncSocketModeHandler(self._app, settings.slack_app_token)
        self._watch = settings.slack_watch_channel_set
        self._allowed = settings.slack_allowed_user_set
        self._register()

    def is_allowed(self, user_id: str) -> bool:
        return not self._allowed or user_id in self._allowed

    def _register(self) -> None:
        @self._app.event("message")
        async def _on_message(event: dict) -> None:
            await self._handle_message(event)

    async def _handle_message(self, event: dict) -> None:
        # bot自身/編集・削除などは無視。ファイル共有と通常メッセージのみ通す。
        if event.get("bot_id"):
            return
        if event.get("subtype") not in (None, "file_share"):
            return

        channel = event.get("channel", "")
        if self._watch and channel not in self._watch:
            return

        pdfs = [
            f
            for f in (event.get("files") or [])
            if f.get("filetype") == "pdf" or f.get("name", "").lower().endswith(".pdf")
        ]
        if not pdfs:
            return

        user = event.get("user", "")
        if not self.is_allowed(user):
            logger.info("許可されていないユーザーからの投稿を無視: %s", user)
            return

        thread_ts = event.get("thread_ts") or event.get("ts", "")
        for f in pdfs:
            size = f.get("size", 0)
            if size and size > self._settings.max_pdf_bytes:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"⚠️ サイズ上限（{self._settings.max_pdf_mb}MB）を超えています。",
                )
                continue
            req = JobRequest(
                id=uuid.uuid4().hex[:12],
                platform=self.name,
                channel_id=channel,
                thread_ref=thread_ts,
                user_id=user,
                filename=f.get("name", "document.pdf"),
                file_url=f.get("url_private_download") or f.get("url_private", ""),
                file_size=size,
            )
            await self._on_job(req)

    async def download(self, req, dest):
        headers = {"Authorization": f"Bearer {self._settings.slack_bot_token}"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(req.file_url, headers=headers) as resp:
                resp.raise_for_status()
                dest.write_bytes(await resp.read())
        return dest

    async def post_text(self, req, text):
        # Slack の text は長文可（~40000字）。ここでは分割しない。
        await self._app.client.chat_postMessage(
            channel=req.channel_id, thread_ts=req.thread_ref, text=text
        )

    async def upload_file(self, req, path, *, title, comment=""):
        await self._app.client.files_upload_v2(
            channel=req.channel_id,
            thread_ts=req.thread_ref,
            file=str(path),
            filename=title,
            initial_comment=comment or None,
        )

    async def start(self):
        logger.info("Slackアダプタを起動します（Socket Mode）")
        await self._handler.start_async()
