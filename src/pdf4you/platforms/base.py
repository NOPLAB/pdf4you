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


class PlatformAdapter(abc.ABC):
    """Slack / Discord など各プラットフォームの実装が満たすインターフェース。"""

    name: str

    @abc.abstractmethod
    async def start(self) -> None:
        """接続して受信ループを開始する（起動後は動き続ける coroutine）。"""

    @abc.abstractmethod
    async def download(self, req: JobRequest, dest: Path) -> Path:
        """添付PDFを dest に保存してパスを返す。"""

    @abc.abstractmethod
    async def post_text(self, req: JobRequest, text: str) -> None:
        """スレッドにテキストを投稿する。"""

    @abc.abstractmethod
    async def upload_file(
        self, req: JobRequest, path: Path, *, title: str, comment: str = ""
    ) -> None:
        """スレッドにファイルを添付する。"""

    def is_allowed(self, user_id: str) -> bool:
        """アクセス制御。許可ユーザー未設定なら誰でも可（サブクラスで上書き）。"""
        return True
