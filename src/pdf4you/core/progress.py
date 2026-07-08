"""翻訳進捗の表示ユーティリティ。

pdf2zh-next（babeldoc）が流す `progress_update` イベントの `overall_progress`（0〜100）を
テキストの進捗バーに整形する。加えて、Slack/Discord のメッセージ編集を叩きすぎないための
スロットリング（一定%進む & 一定秒経過したときだけ更新を許可）を提供する。
"""

from __future__ import annotations

import time

_BAR_FILLED = "▓"
_BAR_EMPTY = "░"


def render_bar(
    overall: float,
    *,
    stage: str = "",
    part_index: int = 0,
    total_parts: int = 0,
    width: int = 10,
) -> str:
    """全体進捗（0〜100）を `🌐 翻訳中  ▓▓▓▓▓▓░░░░  63%` 形式の文字列にする。

    `stage`（工程名）があれば2行目に `工程: {stage} ({part_index}/{total_parts})` を添える。
    """
    pct = max(0.0, min(100.0, float(overall)))
    filled = round(pct / 100 * width)
    bar = _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)
    head = f"🌐 翻訳中  {bar}  {pct:3.0f}%"
    if stage and total_parts:
        return f"{head}\n工程: {stage} ({part_index}/{total_parts})"
    if stage:
        return f"{head}\n工程: {stage}"
    return head


class ProgressThrottle:
    """進捗更新の頻度を抑えるゲート。

    `min_interval` 秒以上経過し、かつ前回から `min_delta` %以上進んだときだけ
    `should_emit()` が True を返す（初回は常に True）。Slack のレート制限や
    メッセージ編集のスパムを避けるために使う。
    """

    def __init__(self, *, min_delta: float = 5.0, min_interval: float = 2.0) -> None:
        self._min_delta = min_delta
        self._min_interval = min_interval
        self._last_value: float | None = None
        self._last_time: float | None = None

    def should_emit(self, value: float) -> bool:
        now = time.monotonic()
        if self._last_time is None:
            self._last_value = value
            self._last_time = now
            return True
        if now - self._last_time < self._min_interval:
            return False
        if self._last_value is not None and value - self._last_value < self._min_delta:
            return False
        self._last_value = value
        self._last_time = now
        return True
