"""プラットフォーム共通の型とアダプタインターフェース。

Slack / Discord の差異（スレッド返信・ファイル添付・ダウンロード・アクセス制御）を
`PlatformAdapter` に隠蔽し、パイプラインからはプラットフォーム非依存に扱う。
"""

from __future__ import annotations

import abc
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

    def is_allowed(self, user_id: str) -> bool:
        """アクセス制御。許可ユーザー未設定なら誰でも可（サブクラスで上書き）。"""
        return True
