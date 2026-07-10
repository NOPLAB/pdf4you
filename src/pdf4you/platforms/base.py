"""プラットフォーム共通の型とアダプタインターフェース。

Slack / Discord の差異（スレッド返信・ファイル添付・ダウンロード・アクセス制御）を
`PlatformAdapter` に隠蔽し、パイプラインからはプラットフォーム非依存に扱う。
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JobRequest:
    """1件のPDF処理要求。プラットフォームアダプタが生成し、キューを流れる。"""

    id: str
    platform: str  # "slack" | "discord"
    channel_id: str
    thread_ref: str  # スレッド識別（Slack: thread_ts / Discord: thread id）
    user_id: str
    filename: str
    file_url: str = ""
    file_size: int = 0
    # アダプタが宛先解決やダウンロードに使う自由領域（同一プロセス内のライブオブジェクト等）
    meta: dict[str, Any] = field(default_factory=dict)


# PDF検知時にアダプタが呼ぶコールバック（キューへ投入する）
JobCallback = Callable[[JobRequest], Awaitable[None]]

# ダウンロード進捗コールバック: (受信済みバイト, 総バイト。総数不明なら0)
OnDownload = Callable[[int, int], Awaitable[None]]


@dataclass
class TranslationOverride:
    """翻訳を途中で別バックエンドに切り替えるための接続パラメータ。

    OpenAI 互換なので `base_url` / `api_key` / `model` の差し替えだけで切り替わる。
    """

    base_url: str
    api_key: str
    model: str
    label: str = "OpenRouter"  # 進捗表示に使う表示名


class JobControl:
    """操作UI（ボタン）→ パイプライン へ「翻訳の切り替え要求」を1回だけ受け渡す。

    ボタン押下ハンドラ（アダプタ側）が `request_switch()` を呼び、パイプラインは
    `switch_requested` フューチャを待って切り替えを実行する。二重要求は無視する。
    フューチャは実行中のイベントループ上で生成する（パイプラインから生成すること）。
    """

    def __init__(self) -> None:
        self._switch: asyncio.Future[TranslationOverride] = (
            asyncio.get_running_loop().create_future()
        )

    def request_switch(self, override: TranslationOverride) -> bool:
        """切り替えを要求する。受理したら True、既に要求済みなら False。"""
        if self._switch.done():
            return False
        self._switch.set_result(override)
        return True

    @property
    def requested(self) -> bool:
        return self._switch.done()

    @property
    def switch_requested(self) -> asyncio.Future[TranslationOverride]:
        return self._switch


class ControlHandle(abc.ABC):
    """操作UI（切替ボタン等）のハンドル。処理完了時に `close()` で無効化する。"""

    @abc.abstractmethod
    async def close(self, note: str = "") -> None:
        """ボタンを無効化/削除する。`note` があれば理由を表示してよい。"""


class ProgressHandle(abc.ABC):
    """1件の進捗メッセージを指すハンドル。`update()` で同じメッセージを上書きする。

    翻訳の進捗バーのように「1つのメッセージを編集し続ける」用途に使う。更新は
    ベストエフォート（失敗してもジョブ本体を止めない）。
    """

    @abc.abstractmethod
    async def update(self, text: str) -> None:
        """既存の進捗メッセージを `text` で上書き（編集）する。"""


class _NoUpdateProgress(ProgressHandle):
    """編集に非対応なアダプタ向けのフォールバック（更新は無視する）。"""

    async def update(self, text: str) -> None:
        return


class PlatformAdapter(abc.ABC):
    """Slack / Discord など各プラットフォームの実装が満たすインターフェース。"""

    name: str

    @abc.abstractmethod
    async def start(self) -> None:
        """接続して受信ループを開始する（起動後は動き続ける coroutine）。"""

    async def wait_ready(self) -> None:
        """投稿可能になるまで待つ（再起動時のジョブ再開が使う）。

        既定は即時リターン。接続完了を待つ必要があるアダプタ（Discord）だけ上書きする。
        """
        return

    @abc.abstractmethod
    async def download(
        self, req: JobRequest, dest: Path, *, on_progress: OnDownload | None = None
    ) -> Path:
        """添付PDFを dest に保存してパスを返す。

        `on_progress` を渡すと受信の進捗（受信済み/総バイト）を随時通知する。
        """

    @abc.abstractmethod
    async def post_text(self, req: JobRequest, text: str) -> None:
        """スレッドにテキストを投稿する。"""

    @abc.abstractmethod
    async def upload_file(
        self, req: JobRequest, path: Path, *, title: str, comment: str = ""
    ) -> None:
        """スレッドにファイルを添付する。"""

    async def start_progress(self, req: JobRequest, text: str) -> ProgressHandle:
        """進捗表示用のメッセージを1件投稿し、上書き可能なハンドルを返す。

        既定は「投稿はするが更新はできない」フォールバック。編集に対応する
        アダプタ（Slack / Discord）はこれを上書きして実メッセージのハンドルを返す。
        """
        await self.post_text(req, text)
        return _NoUpdateProgress()

    async def offer_translation_switch(
        self, req: JobRequest, control: JobControl
    ) -> ControlHandle | None:
        """「外部サービスへ切替」の操作UIを提示する（対応アダプタのみ）。

        押下時は投稿者本人か検証し、登録済みAPIキーで `TranslationOverride` を組み立てて
        `control.request_switch()` を呼ぶ。返り値のハンドルはパイプラインが処理完了時に
        `close()` する。ボタン等に非対応なアダプタは None を返す（既定）。
        """
        return None

    def is_allowed(self, user_id: str) -> bool:
        """アクセス制御。許可ユーザー未設定なら誰でも可（サブクラスで上書き）。"""
        return True
