"""Discord アダプタ（discord.py）。

監視チャンネルへの PDF 添付を検知し、その投稿からスレッドを作成して返信する。
加えて、DM でのスラッシュコマンド（/setkey ほか）で OpenRouter の API キーをユーザー
ごとに登録でき、ローカル翻訳の順番待ち中・実行中は「外部サービスを使用」ボタンを
提示して、順番待ちのスキップや実行中の切り替えを行える。

必要: Bot に「Message Content Intent」を有効化。権限: メッセージ送信・スレッド作成・
ファイル添付。スラッシュコマンドは起動時に `CommandTree.sync()` で同期する。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import discord
from discord import app_commands

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

_DISCORD_LIMIT = 1900  # メッセージ2000字制限に対する安全マージン


def _split(text: str, limit: int = _DISCORD_LIMIT) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


class _DiscordProgress(ProgressHandle):
    """`Message.edit` で同一メッセージを上書きする進捗ハンドル。"""

    def __init__(self, message: discord.Message):
        self._message = message

    async def update(self, text: str) -> None:
        try:
            await self._message.edit(content=text[:_DISCORD_LIMIT])
        except Exception:
            logger.warning("進捗メッセージの更新に失敗しました", exc_info=True)


class _SetKeyModal(discord.ui.Modal, title="APIキーを登録"):
    """API キー入力用のモーダル。入力値はチャットに表示されず、応答も ephemeral。"""

    api_key: discord.ui.TextInput = discord.ui.TextInput(
        label="OpenRouter API キー",
        placeholder="sk-or-v1-...",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    model: discord.ui.TextInput = discord.ui.TextInput(
        label="モデル（任意 / 空欄なら既定）",
        placeholder="openai/gpt-4o-mini",
        style=discord.TextStyle.short,
        required=False,
        max_length=120,
    )

    def __init__(self, keystore: UserKeyStore):
        super().__init__()
        self._keystore = keystore

    async def on_submit(self, interaction: discord.Interaction) -> None:
        model = self.model.value.strip() or None
        await self._keystore.set_key(
            "discord", str(interaction.user.id), self.api_key.value.strip(), model
        )
        await interaction.response.send_message(
            f"✅ APIキーを登録しました（`{mask_key(self.api_key.value.strip())}`）。",
            ephemeral=True,
        )


class _SwitchView(discord.ui.View):
    """進捗メッセージとは別に投稿する「外部サービスへ切替」ボタン。"""

    def __init__(self, adapter: DiscordAdapter, req: JobRequest, control: JobControl):
        super().__init__(timeout=None)
        self._adapter = adapter
        self._req = req
        self._control = control

    @discord.ui.button(
        label="外部サービスを使用",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
    )
    async def switch(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._adapter._handle_switch(interaction, self._req, self._control, button, self)


class _DiscordControl(ControlHandle):
    """切替ボタン付きメッセージのハンドル。完了時にボタンを除去する。"""

    def __init__(self, message: discord.Message):
        self._message = message

    async def close(self, note: str = "") -> None:
        try:
            if note:
                await self._message.edit(content=note[:_DISCORD_LIMIT], view=None)
            else:
                await self._message.edit(view=None)
        except Exception:
            logger.warning("切替ボタンのクローズに失敗しました", exc_info=True)


class DiscordAdapter(PlatformAdapter):
    name = "discord"

    def __init__(
        self,
        settings: Settings,
        on_job: JobCallback,
        keystore: UserKeyStore | None = None,
    ):
        self._settings = settings
        self._on_job = on_job
        self._keystore = keystore
        self._watch = settings.discord_watch_channel_set
        self._allowed = settings.discord_allowed_user_set
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)
        self._threads: dict[str, discord.abc.Messageable] = {}
        self._register()
        self._register_commands()

    def is_allowed(self, user_id: str) -> bool:
        return not self._allowed or user_id in self._allowed

    def _register(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            try:
                await self._tree.sync()
                logger.info("スラッシュコマンドを同期しました")
            except Exception:
                logger.warning("スラッシュコマンドの同期に失敗しました", exc_info=True)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._handle_message(message)

    # ------------------------------------------------------------------
    # スラッシュコマンド（DM 推奨。応答は常に ephemeral）
    # ------------------------------------------------------------------
    def _register_commands(self) -> None:
        keystore = self._keystore

        async def _guard(interaction: discord.Interaction) -> bool:
            """キー機能が無効なら案内して False を返す。"""
            if keystore is None:
                await interaction.response.send_message(
                    "⚠️ 管理者が SECRET_KEY を設定していないため、キー機能は無効です。",
                    ephemeral=True,
                )
                return False
            return True

        @self._tree.command(
            name="setkey", description="OpenRouter APIキーを登録/更新します（DM推奨）"
        )
        async def setkey(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            await interaction.response.send_modal(_SetKeyModal(keystore))

        @self._tree.command(name="keystatus", description="登録済みAPIキーの状態を表示します")
        async def keystatus(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            stored = await keystore.get_key("discord", str(interaction.user.id))
            if stored is None:
                await interaction.response.send_message(
                    "未登録です。`/setkey` で登録できます。", ephemeral=True
                )
                return
            model = stored.model or f"（既定: {self._settings.openrouter_model}）"
            await interaction.response.send_message(
                f"登録済み: `{mask_key(stored.api_key)}`\nモデル: {model}", ephemeral=True
            )

        @self._tree.command(name="forgetkey", description="登録済みAPIキーを削除します")
        async def forgetkey(interaction: discord.Interaction) -> None:
            if not await _guard(interaction):
                return
            removed = await keystore.delete_key("discord", str(interaction.user.id))
            await interaction.response.send_message(
                "🗑️ 削除しました。" if removed else "登録はありませんでした。", ephemeral=True
            )

    # ------------------------------------------------------------------
    # 翻訳の途中切替（ボタン）
    # ------------------------------------------------------------------
    async def offer_translation_switch(
        self, req: JobRequest, control: JobControl
    ) -> ControlHandle | None:
        if self._keystore is None:
            return None
        dest = await self._resolve_dest(req)
        if dest is None:
            return None
        view = _SwitchView(self, req, control)
        try:
            message = await dest.send(
                "💡 ローカル翻訳の順番待ちをスキップして、登録済みの外部サービス"
                "（OpenRouter）で今すぐ翻訳できます。",
                view=view,
            )
        except Exception:
            logger.warning("切替ボタンの投稿に失敗しました", exc_info=True)
            return None
        return _DiscordControl(message)

    async def _handle_switch(
        self,
        interaction: discord.Interaction,
        req: JobRequest,
        control: JobControl,
        button: discord.ui.Button,
        view: discord.ui.View,
    ) -> None:
        if str(interaction.user.id) != req.user_id:
            await interaction.response.send_message(
                "⚠️ この操作は投稿者のみ実行できます。", ephemeral=True
            )
            return
        if self._keystore is None:
            await interaction.response.send_message(
                "⚠️ キー機能が無効です（SECRET_KEY 未設定）。", ephemeral=True
            )
            return
        stored = await self._keystore.get_key("discord", req.user_id)
        if stored is None:
            await interaction.response.send_message(
                "⚠️ APIキーが未登録です。DMで `/setkey` を実行してから再度お試しください。",
                ephemeral=True,
            )
            return
        override = TranslationOverride(
            base_url=self._settings.openrouter_base_url,
            api_key=stored.api_key,
            model=stored.model or self._settings.openrouter_model,
        )
        if not control.request_switch(override):
            await interaction.response.send_message(
                "すでに切り替え済みか、翻訳が完了しています。", ephemeral=True
            )
            return
        button.disabled = True
        try:
            await interaction.response.edit_message(
                content="🔄 外部サービス（OpenRouter）に切り替えています…", view=view
            )
        except Exception:
            logger.warning("切替ボタンの更新に失敗しました", exc_info=True)

    # ------------------------------------------------------------------
    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        is_dm = message.guild is None
        channel_id = str(message.channel.id)
        if is_dm:
            # DM は監視チャンネル設定に関わらず処理する（アクセス制御は下で適用）。
            if not self._settings.allow_dm:
                return
        else:
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
            dest = await self._ensure_dest(message, att.filename, is_dm)
            req = JobRequest(
                id=uuid.uuid4().hex[:12],
                platform=self.name,
                channel_id=channel_id,
                thread_ref=str(dest.id),
                user_id=user_id,
                filename=att.filename,
                file_url=att.url,
                file_size=att.size,
                meta={"thread": dest, "attachment": att},
            )
            await self._on_job(req)

    async def _ensure_dest(self, message, filename, is_dm):
        # DM ではスレッドを作成できないため、その DM チャンネルへ直接返信する。
        if is_dm or isinstance(message.channel, discord.Thread):
            self._threads[str(message.channel.id)] = message.channel
            return message.channel
        thread = await message.create_thread(name=f"翻訳: {filename[:80]}")
        self._threads[str(thread.id)] = thread
        return thread

    async def _resolve_dest(self, req) -> discord.abc.Messageable | None:
        """投稿先（スレッド/DMチャンネル）を解決する。

        通常は `meta` のライブオブジェクトか作成時のキャッシュで足りるが、再起動後の
        再開ジョブはどちらも無いため、`thread_ref` の ID から API で取得し直す。
        """
        dest = req.meta.get("thread") or self._threads.get(req.thread_ref)
        if dest is not None:
            return dest
        try:
            channel_id = int(req.thread_ref)
            dest = self._client.get_channel(channel_id) or await self._client.fetch_channel(
                channel_id
            )
        except Exception:
            logger.warning("投稿先の再解決に失敗しました: %s", req.thread_ref, exc_info=True)
            return None
        self._threads[req.thread_ref] = dest
        return dest

    async def _dest_or_raise(self, req) -> discord.abc.Messageable:
        dest = await self._resolve_dest(req)
        if dest is None:
            raise RuntimeError(f"投稿先を解決できません: thread_ref={req.thread_ref}")
        return dest

    async def download(self, req, dest, *, on_progress=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        att = req.meta.get("attachment")
        # 進捗不要なら discord.py の save() が最速。進捗が要るときはチャンク受信する。
        if att is not None and on_progress is None:
            await att.save(dest)
            return dest
        # attachment url は認証不要。チャンク受信しながら進捗を通知する。
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(req.file_url) as resp:
                resp.raise_for_status()
                total = int(
                    resp.headers.get("Content-Length")
                    or (att.size if att is not None else 0)
                    or req.file_size
                    or 0
                )
                received = 0
                with dest.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
                        received += len(chunk)
                        if on_progress is not None:
                            await on_progress(received, total)
        return dest

    async def post_text(self, req, text):
        thread = await self._dest_or_raise(req)
        for chunk in _split(text):
            await thread.send(chunk)

    async def start_progress(self, req, text):
        thread = await self._dest_or_raise(req)
        message = await thread.send(text[:_DISCORD_LIMIT])
        return _DiscordProgress(message)

    async def upload_file(self, req, path, *, title, comment=""):
        thread = await self._dest_or_raise(req)
        if comment:
            await thread.send(comment)
        await thread.send(file=discord.File(str(path), filename=title))

    async def wait_ready(self) -> None:
        # `Client.wait_until_ready()` はログイン前に呼ぶと例外になるため、
        # `start()` と並行に呼ばれても安全なポーリングで待つ。
        while not self._client.is_ready():
            await asyncio.sleep(0.5)

    async def start(self):
        logger.info("Discordアダプタを起動します")
        await self._client.start(self._settings.discord_bot_token)
