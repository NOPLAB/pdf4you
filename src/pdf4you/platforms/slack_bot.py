"""Slack アダプタ（Bolt / Socket Mode）。

監視チャンネルへの PDF 添付を検知して JobRequest を発行し、スレッド（thread_ts）へ
テキスト・ファイルを投稿する。加えて、スラッシュコマンド（/setkey ほか）で OpenRouter
の API キーをユーザーごとに登録でき、翻訳中は「ローカル翻訳をキャンセルして外部
サービスを使用」ボタン（Block Kit）を提示する。

必要スコープ: `files:read`, `chat:write`, `commands`, `channels:history`（対象チャンネル
種別に応じて groups/im/mpim も）。スラッシュコマンドはアプリ設定側で `/setkey`
`/keystatus` `/forgetkey` を宣言し、Interactivity を有効化しておくこと（Socket Mode
なので Request URL は不要）。
"""

from __future__ import annotations

import logging
import re
import uuid

import aiohttp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from ..config import Settings
from ..jobs.keystore import UserKeyStore, mask_key
from .base import (
    ControlHandle,
    JobCallback,
    JobControl,
    JobRequest,
    PlatformAdapter,
    ProgressHandle,
    TranslationOverride,
)

logger = logging.getLogger(__name__)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*$", re.MULTILINE)

_SWITCH_ACTION = "switch_translation"
_SETKEY_MODAL = "setkey_modal"


def _to_mrkdwn(text: str) -> str:
    """GitHub風Markdown を Slack の mrkdwn に変換する。

    Slack は `##` 見出しや `**bold**` を解釈しないため、
    見出し行は太字に、`**bold**` は `*bold*` に変換する。
    """
    text = _HEADING_RE.sub(r"*\1*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    return text


def _setkey_modal_view(default_model: str) -> dict:
    return {
        "type": "modal",
        "callback_id": _SETKEY_MODAL,
        "title": {"type": "plain_text", "text": "APIキーを登録"},
        "submit": {"type": "plain_text", "text": "保存"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "blocks": [
            {
                "type": "input",
                "block_id": "api_key",
                "label": {"type": "plain_text", "text": "OpenRouter API キー"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "sk-or-v1-..."},
                },
            },
            {
                "type": "input",
                "block_id": "model",
                "optional": True,
                "label": {"type": "plain_text", "text": "モデル（任意 / 空欄なら既定）"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "placeholder": {
                        "type": "plain_text",
                        "text": default_model or "openai/gpt-4o-mini",
                    },
                },
            },
        ],
    }


class _SlackProgress(ProgressHandle):
    """`chat.update` で同一メッセージ（channel + ts）を上書きする進捗ハンドル。"""

    def __init__(self, client, channel: str, ts: str):
        self._client = client
        self._channel = channel
        self._ts = ts

    async def update(self, text: str) -> None:
        try:
            await self._client.chat_update(
                channel=self._channel, ts=self._ts, text=_to_mrkdwn(text)
            )
        except Exception:
            logger.warning("進捗メッセージの更新に失敗しました", exc_info=True)


class _SlackControl(ControlHandle):
    """切替ボタン付きメッセージのハンドル。完了時にボタンを除去する。"""

    def __init__(self, adapter: SlackAdapter, token: str, channel: str, ts: str):
        self._adapter = adapter
        self._token = token
        self._channel = channel
        self._ts = ts

    async def close(self, note: str = "") -> None:
        self._adapter._controls.pop(self._token, None)
        try:
            await self._adapter._app.client.chat_update(
                channel=self._channel,
                ts=self._ts,
                text=note or "（外部サービス切替の受付を終了しました）",
                blocks=[],  # ボタンを除去
            )
        except Exception:
            logger.warning("切替ボタンのクローズに失敗しました", exc_info=True)


class SlackAdapter(PlatformAdapter):
    name = "slack"

    def __init__(
        self,
        settings: Settings,
        on_job: JobCallback,
        keystore: UserKeyStore | None = None,
    ):
        self._settings = settings
        self._on_job = on_job
        self._keystore = keystore
        self._app = AsyncApp(token=settings.slack_bot_token)
        self._handler = AsyncSocketModeHandler(self._app, settings.slack_app_token)
        self._watch = settings.slack_watch_channel_set
        self._allowed = settings.slack_allowed_user_set
        # token(=job id) -> (req, control)。切替ボタン押下時に控えから引き当てる。
        self._controls: dict[str, tuple[JobRequest, JobControl]] = {}
        self._register()
        self._register_commands()

    def is_allowed(self, user_id: str) -> bool:
        return not self._allowed or user_id in self._allowed

    def _register(self) -> None:
        @self._app.event("message")
        async def _on_message(event: dict) -> None:
            await self._handle_message(event)

    # ------------------------------------------------------------------
    # スラッシュコマンド / モーダル / 切替ボタン
    # ------------------------------------------------------------------
    def _register_commands(self) -> None:
        keystore = self._keystore

        @self._app.command("/setkey")
        async def _cmd_setkey(ack, body, client) -> None:
            if keystore is None:
                await ack("⚠️ 管理者が SECRET_KEY を設定していないため、キー機能は無効です。")
                return
            await ack()
            await client.views_open(
                trigger_id=body["trigger_id"],
                view=_setkey_modal_view(self._settings.openrouter_model),
            )

        @self._app.view(_SETKEY_MODAL)
        async def _on_setkey_submit(ack, body, view, client) -> None:
            await ack()
            if keystore is None:
                return
            user = body["user"]["id"]
            values = view["state"]["values"]
            api_key = (values["api_key"]["value"]["value"] or "").strip()
            model = (values["model"]["value"]["value"] or "").strip() or None
            if not api_key:
                return
            await keystore.set_key("slack", user, api_key, model)
            try:
                # モーダルには確認トーストが無いので DM で結果を通知する。
                await client.chat_postMessage(
                    channel=user,
                    text=f"✅ APIキーを登録しました（`{mask_key(api_key)}`）。",
                )
            except Exception:
                logger.warning("登録結果の通知に失敗しました", exc_info=True)

        @self._app.command("/keystatus")
        async def _cmd_keystatus(ack, body) -> None:
            if keystore is None:
                await ack("⚠️ キー機能は無効です（SECRET_KEY 未設定）。")
                return
            stored = await keystore.get_key("slack", body["user_id"])
            if stored is None:
                await ack("未登録です。`/setkey` で登録できます。")
                return
            model = stored.model or f"（既定: {self._settings.openrouter_model}）"
            await ack(f"登録済み: `{mask_key(stored.api_key)}`\nモデル: {model}")

        @self._app.command("/forgetkey")
        async def _cmd_forgetkey(ack, body) -> None:
            if keystore is None:
                await ack("⚠️ キー機能は無効です（SECRET_KEY 未設定）。")
                return
            removed = await keystore.delete_key("slack", body["user_id"])
            await ack("🗑️ 削除しました。" if removed else "登録はありませんでした。")

        @self._app.action(_SWITCH_ACTION)
        async def _on_switch(ack, body, client) -> None:
            await ack()
            await self._handle_switch(body, client)

    async def offer_translation_switch(
        self, req: JobRequest, control: JobControl
    ) -> ControlHandle | None:
        if self._keystore is None:
            return None
        token = req.id
        self._controls[token] = (req, control)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "💡 ローカル翻訳の代わりに、登録済みの外部サービス"
                    "（OpenRouter）へ切り替えられます。",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": _SWITCH_ACTION,
                        "text": {
                            "type": "plain_text",
                            "text": "外部サービスを使用",
                        },
                        "style": "primary",
                        "value": token,
                    }
                ],
            },
        ]
        try:
            resp = await self._app.client.chat_postMessage(
                channel=req.channel_id,
                thread_ts=req.thread_ref,
                text="外部サービスへ切り替えられます",
                blocks=blocks,
            )
        except Exception:
            logger.warning("切替ボタンの投稿に失敗しました", exc_info=True)
            self._controls.pop(token, None)
            return None
        return _SlackControl(self, token, req.channel_id, resp["ts"])

    async def _handle_switch(self, body: dict, client) -> None:
        actions = body.get("actions") or []
        token = actions[0].get("value") if actions else None
        user = body.get("user", {}).get("id", "")
        channel = body.get("channel", {}).get("id", "")
        msg_ts = body.get("container", {}).get("message_ts") or body.get("message", {}).get(
            "ts"
        )
        thread_ts = body.get("message", {}).get("thread_ts")

        async def ephem(text: str) -> None:
            try:
                await client.chat_postEphemeral(
                    channel=channel, user=user, thread_ts=thread_ts, text=text
                )
            except Exception:
                logger.warning("ephemeral 通知に失敗しました", exc_info=True)

        entry = self._controls.get(token) if token else None
        if entry is None:
            await ephem("この操作は既に終了しています。")
            return
        req, control = entry
        if user != req.user_id:
            await ephem("⚠️ この操作は投稿者のみ実行できます。")
            return
        if self._keystore is None:
            await ephem("⚠️ キー機能が無効です（SECRET_KEY 未設定）。")
            return
        stored = await self._keystore.get_key("slack", req.user_id)
        if stored is None:
            await ephem("⚠️ APIキーが未登録です。`/setkey` で登録してから再度お試しください。")
            return
        override = TranslationOverride(
            base_url=self._settings.openrouter_base_url,
            api_key=stored.api_key,
            model=stored.model or self._settings.openrouter_model,
        )
        if not control.request_switch(override):
            await ephem("すでに切り替え済みか、翻訳が完了しています。")
            return
        if msg_ts:
            try:
                await client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    text="🔄 外部サービス（OpenRouter）に切り替えています…",
                    blocks=[],
                )
            except Exception:
                logger.warning("切替メッセージの更新に失敗しました", exc_info=True)

    # ------------------------------------------------------------------
    async def _handle_message(self, event: dict) -> None:
        channel_type = event.get("channel_type")
        logger.debug(
            "message受信: channel_type=%s subtype=%s user=%s files=%d bot_id=%s",
            channel_type,
            event.get("subtype"),
            event.get("user"),
            len(event.get("files") or []),
            event.get("bot_id"),
        )
        # bot自身/編集・削除などは無視。ファイル共有と通常メッセージのみ通す。
        if event.get("bot_id"):
            logger.debug("無視: bot自身のメッセージ")
            return
        if event.get("subtype") not in (None, "file_share"):
            logger.debug("無視: 対象外の subtype=%s", event.get("subtype"))
            return

        channel = event.get("channel", "")
        is_dm = channel_type == "im"
        if is_dm:
            # DM(IM) は監視チャンネル設定に関わらず処理する（アクセス制御は下で適用）。
            if not self._settings.allow_dm:
                logger.debug("無視: DM だが ALLOW_DM=false")
                return
        elif self._watch and channel not in self._watch:
            logger.debug("無視: 監視対象外チャンネル %s", channel)
            return

        pdfs = [
            f
            for f in (event.get("files") or [])
            if f.get("filetype") == "pdf" or f.get("name", "").lower().endswith(".pdf")
        ]
        if not pdfs:
            logger.debug("無視: PDF 添付なし（channel_type=%s）", channel_type)
            return

        user = event.get("user", "")
        if not self.is_allowed(user):
            logger.info("許可されていないユーザーからの投稿を無視: %s", user)
            return

        logger.info(
            "PDF受理: user=%s channel=%s is_dm=%s files=%d", user, channel, is_dm, len(pdfs)
        )

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

    async def download(self, req, dest, *, on_progress=None):
        headers = {"Authorization": f"Bearer {self._settings.slack_bot_token}"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(req.file_url, headers=headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or req.file_size or 0)
                received = 0
                with dest.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
                        received += len(chunk)
                        if on_progress is not None:
                            await on_progress(received, total)
        return dest

    async def post_text(self, req, text):
        # Slack の text は長文可（~40000字）。ここでは分割しない。
        # 要約は GitHub風Markdown なので Slack mrkdwn に変換して投稿する。
        await self._app.client.chat_postMessage(
            channel=req.channel_id, thread_ts=req.thread_ref, text=_to_mrkdwn(text)
        )

    async def start_progress(self, req, text):
        resp = await self._app.client.chat_postMessage(
            channel=req.channel_id, thread_ts=req.thread_ref, text=_to_mrkdwn(text)
        )
        return _SlackProgress(self._app.client, req.channel_id, resp["ts"])

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
