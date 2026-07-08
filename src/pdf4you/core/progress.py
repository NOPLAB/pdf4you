"""翻訳進捗の表示ユーティリティ。

pdf2zh-next（babeldoc）が流す `progress_update` イベントから、
- 全体進捗 `overall_progress`（0〜100）をメインバーに、
- 現在の工程内進捗 `stage_progress`（0〜100。LLM翻訳工程なら翻訳の進み具合）をサブバーに
整形する。加えて、Slack/Discord のメッセージ編集を叩きすぎないためのスロットリング
（一定%進む／工程が変わる & 一定秒経過したときだけ更新を許可）を提供する。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_BAR_FILLED = "▓"
_BAR_EMPTY = "░"

# babeldoc の英語ステージ名 → 表示用の短い日本語ラベル。
# 特に "Translate Paragraphs" が LLM を使う翻訳工程。未知の工程名はそのまま表示する。
STAGE_LABELS = {
    "Translate Paragraphs": "翻訳(LLM)",
    "Parse PDF and Create Intermediate Representation": "PDF解析",
    "Parse Page Layout": "レイアウト解析",
    "Parse Paragraphs": "段落解析",
    "Parse Formulas and Styles": "数式・書式解析",
    "Parse Table": "表解析",
    "Typesetting": "組版",
    "Add Fonts": "フォント追加",
    "Generate drawing instructions": "描画生成",
    "Automatic Term Extraction": "用語抽出",
    "DetectScannedFile": "スキャン判定",
    "Remove Char Descent": "文字調整",
    "Add Debug Information": "デバッグ情報",
}


@dataclass
class ProgressEvent:
    """翻訳エンジンの1回分の進捗（`progress_update` 由来）。"""

    overall: float  # 全体進捗 0〜100
    stage: str = ""  # 現在の工程名（babeldoc の英語名）
    stage_progress: float = 0.0  # 工程内進捗 0〜100
    stage_current: int = 0  # 工程内の処理済み数（翻訳工程なら段落数）
    stage_total: int = 0  # 工程内の総数
    part_index: int = 1  # 分割文書のパート番号（1始まり）
    total_parts: int = 1  # パート総数


def _bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, float(pct)))
    filled = round(pct / 100 * width)
    return _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)


def render_phase(emoji: str, label: str, pct: float, detail: str = "") -> str:
    """翻訳以外の単一工程（ダウンロード・抽出など）向けの1行バー。

    例:: ``⬇️ ダウンロード中  ▓▓▓▓▓▓░░░░   63%  (12.5/20.0 MB)``
    """
    pct = max(0.0, min(100.0, float(pct)))
    line = f"{emoji} {label}  {_bar(pct)}  {pct:3.0f}%"
    if detail:
        line += f"  {detail}"
    return line


def render_progress(ev: ProgressEvent) -> str:
    """全体バー（メイン）＋工程内バー（サブ）の複数行テキストを組み立てる。

    例::

        🌐 翻訳中  ▓▓▓▓▓▓░░░░   63%
        ┗ 翻訳(LLM)  ▓▓▓▓▓░░░░░   52%  (140/270)
    """
    overall = max(0.0, min(100.0, float(ev.overall)))
    lines = [f"🌐 翻訳中  {_bar(overall)}  {overall:3.0f}%"]
    if ev.total_parts > 1:
        lines[0] += f"  · パート {ev.part_index}/{ev.total_parts}"

    if ev.stage:
        label = STAGE_LABELS.get(ev.stage, ev.stage)
        sub = f"┗ {label}  {_bar(ev.stage_progress)}  {ev.stage_progress:3.0f}%"
        if ev.stage_total:
            sub += f"  ({ev.stage_current}/{ev.stage_total})"
        lines.append(sub)

    return "\n".join(lines)


class ProgressThrottle:
    """進捗更新の頻度を抑えるゲート。

    `min_interval` 秒以上経過し、かつ「全体 or 工程内が `min_delta` %以上進む」または
    「工程が切り替わった」ときだけ `should_emit()` が True を返す（初回は常に True）。
    Slack のレート制限やメッセージ編集のスパムを避けつつ、サブバーの動きにも追従する。
    """

    def __init__(self, *, min_delta: float = 5.0, min_interval: float = 2.0) -> None:
        self._min_delta = min_delta
        self._min_interval = min_interval
        self._last_time: float | None = None
        self._last_overall = 0.0
        self._last_stage_progress = 0.0
        self._last_stage = ""

    def should_emit(self, ev: ProgressEvent) -> bool:
        now = time.monotonic()
        if self._last_time is None:
            self._store(now, ev)
            return True
        if now - self._last_time < self._min_interval:
            return False
        stage_changed = ev.stage != self._last_stage
        overall_moved = abs(ev.overall - self._last_overall) >= self._min_delta
        stage_moved = abs(ev.stage_progress - self._last_stage_progress) >= self._min_delta
        if stage_changed or overall_moved or stage_moved:
            self._store(now, ev)
            return True
        return False

    def _store(self, now: float, ev: ProgressEvent) -> None:
        self._last_time = now
        self._last_overall = ev.overall
        self._last_stage_progress = ev.stage_progress
        self._last_stage = ev.stage
