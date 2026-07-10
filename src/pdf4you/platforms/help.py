"""ヘルプテキストの生成（プラットフォーム共通）。

GitHub風Markdownで組み立てる。Slack は投稿時に mrkdwn へ変換され、Discord はそのまま
表示される。Discord のメッセージ上限（2000字）に収まる長さを保つこと。
"""

from __future__ import annotations

from ..config import Settings

_OPENROUTER_GUIDE = """\
**外部サービス（OpenRouter）のセットアップ**
自分の API キーを登録すると、ローカル翻訳の順番待ちをスキップして OpenRouter で今すぐ翻訳できます。
1. <https://openrouter.ai/> でアカウントを作成（Google アカウント等でサインアップ可）
2. **Keys**（<https://openrouter.ai/settings/keys>）→ **Create Key** でキーを発行し、\
表示された `sk-or-v1-...` をコピー（あとから再表示できないので注意）
3. 有料モデルを使う場合は **Credits**（<https://openrouter.ai/settings/credits>）で残高をチャージ
4. Bot に `/setkey` を実行し、モーダルにキーを貼り付けて保存（モデル欄は空欄なら既定 `{model}`）
5. PDF 投稿後にスレッドへ出る **「外部サービスを使用」** ボタンを押すと OpenRouter に切り替わる\
（操作できるのは投稿者本人のみ）"""

_KEYS_DISABLED_NOTE = (
    "⚠️ 現在は管理者が SECRET_KEY を設定していないため、"
    "キー機能（`/setkey`・切替ボタン）は無効です。"
)


def build_help_text(settings: Settings, *, help_command: str = "/help") -> str:
    """使い方・コマンド一覧・OpenRouter 手順をまとめたヘルプを返す。"""
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
        "- `/setkey` — OpenRouter API キーを登録/更新（DM 推奨）",
        "- `/keystatus` — 登録状態をマスク表示で確認",
        "- `/forgetkey` — 登録済みキーを削除",
        f"- `{help_command}` — このヘルプを表示",
        "",
        _OPENROUTER_GUIDE.format(model=settings.openrouter_model),
    ]
    if not settings.keys_enabled:
        lines += ["", _KEYS_DISABLED_NOTE]
    return "\n".join(lines)
