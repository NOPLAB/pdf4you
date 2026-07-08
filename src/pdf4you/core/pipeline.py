"""処理パイプライン統括。

抽出 → （要約 ∥ 翻訳）を並行実行し、完了したものからスレッドへ投稿する。
要約は先に返ることが多いので先出しし、翻訳は mono → dual の順に添付する。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import Settings
from ..platforms.base import JobRequest, PlatformAdapter
from . import extractor, summarizer, translator
from .llm_client import make_client

logger = logging.getLogger(__name__)


async def process_job(
    req: JobRequest,
    adapter: PlatformAdapter,
    settings: Settings,
    job_dir: Path,
) -> None:
    await adapter.post_text(
        req, f"📥 受け付けました。翻訳・要約を開始します（`{req.filename}`）。"
    )

    # 1. ダウンロード
    src = job_dir / req.filename
    try:
        await adapter.download(req, src)
    except Exception:
        logger.exception("ダウンロード失敗: %s", req.id)
        await adapter.post_text(req, "⚠️ ファイルの取得に失敗しました。")
        return

    # 2. テキスト抽出（同期処理なのでスレッドに逃がす）＆ 入力言語の解決
    text = await asyncio.to_thread(extractor.extract_text, src)
    lang_in = extractor.resolve_lang_in(settings.lang_in, text)

    # 3. 要約と翻訳を並行起動
    summary_client = make_client(settings.summary_base_url, settings.summary_api_key)
    summary_task = asyncio.create_task(
        summarizer.summarize(summary_client, settings.summary_model, text)
    )
    translate_task = asyncio.create_task(
        translator.translate_pdf(
            src,
            job_dir,
            lang_in=lang_in,
            lang_out=settings.lang_out,
            base_url=settings.translate_base_url,
            api_key=settings.translate_api_key,
            model=settings.translate_model,
        )
    )

    # 4. 要約を先出し
    try:
        summary = await summary_task
        await adapter.post_text(req, f"📝 **要約**\n\n{summary}")
    except Exception:
        logger.exception("要約失敗: %s", req.id)
        await adapter.post_text(req, "⚠️ 要約の生成に失敗しました。")

    # 5. 翻訳結果（mono → dual）
    try:
        result = await translate_task
    except Exception:
        logger.exception("翻訳失敗: %s", req.id)
        await adapter.post_text(req, "⚠️ 翻訳に失敗しました。")
        return

    stem = Path(req.filename).stem
    posted = False
    if result.mono_path and result.mono_path.exists():
        await adapter.upload_file(
            req, result.mono_path, title=f"{stem}-mono.pdf", comment="🌐 訳文 (mono)"
        )
        posted = True
    if result.dual_path and result.dual_path.exists():
        await adapter.upload_file(
            req, result.dual_path, title=f"{stem}-dual.pdf", comment="📑 対訳 (dual)"
        )
        posted = True

    await adapter.post_text(
        req, "✅ 完了しました。" if posted else "⚠️ 翻訳PDFを生成できませんでした。"
    )
