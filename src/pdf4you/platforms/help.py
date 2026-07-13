"""ヘルプテキストの生成（プラットフォーム共通）。

GitHub風Markdownで組み立てる。Slack は投稿時に mrkdwn へ変換され、Discord はそのまま
表示される。Discord のメッセージ上限（2000字）に収まる長さを保つこと。
"""

from __future__ import annotations

from ..config import Settings

_EXTERNAL_GUIDE = """\
**外部サービスのセットアップ**
自分の API キーを登録すると、ローカル翻訳の順番待ちをスキップして外部サービス\
（OpenAI 互換 API）で今すぐ翻訳できます。
1. 使いたいサービスで API キーを発行する
2. Bot に `/setkey` を実行し、モーダルにキーを貼り付けて保存\
（モデル欄・Base URL 欄は空欄なら既定 `{model}` / `{base_url}`）
3. PDF 投稿後にスレッドへ出る **「外部サービスを使用」** ボタンを押すと切り替わる\
（操作できるのは投稿者本人のみ）"""

_OPENROUTER_STEPS = """\
既定の OpenRouter を使う場合: <https://openrouter.ai/> でアカウント作成 → \
**Keys**（<https://openrouter.ai/settings/keys>）でキーを発行（`sk-or-v1-...` は再表示不可）\
→ 有料モデルは **Credits**（<https://openrouter.ai/settings/credits>）で残高をチャージ"""

_KEYS_DISABLED_NOTE = (
    "⚠️ 現在は管理者が SECRET_KEY を設定していないため、"
    "キー機能（`/setkey`・切替ボタン）は無効です。"
)


def build_help_text(settings: Settings, *, help_command: str = "/help") -> str:
    """使い方・コマンド一覧・外部サービスの設定手順をまとめたヘルプを返す。"""
    lines = [
        "**📄 pdf4you — PDF 翻訳・要約 Bot**",
        "",
        "PDF を投稿すると、要約（TL;DR＋詳細）と翻訳 PDF"
        "（mono: 訳文のみ / dual: 原文対訳）をスレッドに返信します。",
        "",
        "**使い方**",
        "- 監視チャンネルに PDF を添付して投稿する",
    ]
    if settings.allow_dm:
        lines.append("- Bot への DM に PDF を送っても翻訳できます")
    lines += [
        f"- サイズ上限: {settings.max_pdf_mb}MB / 翻訳先言語: `{settings.lang_out}`",
        "",
        "**コマンド**",
        "- `/setkey` — 外部サービスの API キー・モデル・Base URL を登録/更新（DM 推奨）",
        "- `/keystatus` — 登録状態をマスク表示で確認",
        "- `/forgetkey` — 登録済みキーを削除",
        f"- `{help_command}` — このヘルプを表示",
        "",
        _EXTERNAL_GUIDE.format(model=settings.external_model, base_url=settings.external_base_url),
    ]
    if "openrouter" in settings.external_base_url.lower():
        lines += ["", _OPENROUTER_STEPS]
    if not settings.keys_enabled:
        lines += ["", _KEYS_DISABLED_NOTE]
    return "\n".join(lines)
