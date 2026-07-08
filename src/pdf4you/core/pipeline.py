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
from .progress import ProgressEvent, ProgressThrottle, render_phase, render_progress

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


def _fmt_bytes(received: int, total: int) -> str:
    """受信量をサイズに応じて MB / KB で見やすく整形する。"""
    if total >= _MB:
        return f"{received / _MB:.1f}/{total / _MB:.1f} MB"
    return f"{received / 1024:.0f}/{total / 1024:.0f} KB"


async def process_job(
    req: JobRequest,
    adapter: PlatformAdapter,
    settings: Settings,
    job_dir: Path,
) -> None:
    # 受付〜完了までを1つのメッセージで表現する（翻訳中はバーで上書きする）。
    progress = await adapter.start_progress(
        req, f"📥 受け付けました。翻訳・要約を開始します（`{req.filename}`）。"
    )

    # 1. ダウンロード（バイト数の進捗バー）
    src = job_dir / req.filename
    dl_throttle = ProgressThrottle()

    async def on_download(received: int, total: int) -> None:
        if total <= 0:
            return
        pct = received * 100 / total
        # 最後（受信完了）は必ず出して 100% で締める。
        if received < total and not dl_throttle.should_emit(ProgressEvent(overall=pct)):
            return
        await progress.update(
            render_phase("⬇️", "ダウンロード中", pct, _fmt_bytes(received, total))
        )

    try:
        await adapter.download(req, src, on_progress=on_download)
    except Exception:
        logger.exception("ダウンロード失敗: %s", req.id)
        await progress.update("⚠️ ファイルの取得に失敗しました。")
        return

    # 2. テキスト抽出（ページ数の進捗バー。同期処理なのでスレッドに逃がす）
    loop = asyncio.get_running_loop()
    ex_throttle = ProgressThrottle()

    def on_page(current: int, total: int) -> None:
        # 別スレッドから呼ばれる。間引いたうえでメッセージ更新をループへ委譲する。
        pct = current * 100 / total if total else 100.0
        # 最終ページは必ず出す（半端な%で止まって見えるのを防ぐ）。
        if current < total and not ex_throttle.should_emit(ProgressEvent(overall=pct)):
            return
        text = render_phase("📄", "テキスト抽出中", pct, f"{current}/{total} ページ")
        asyncio.run_coroutine_threadsafe(progress.update(text), loop)

    text = await asyncio.to_thread(extractor.extract_text, src, on_page=on_page)
    lang_in = extractor.resolve_lang_in(settings.lang_in, text)

    # 3. 要約と翻訳を並行起動
    summary_client = make_client(settings.summary_base_url, settings.summary_api_key)
    summary_task = asyncio.create_task(
        summarizer.summarize(summary_client, settings.summary_model, text)
    )

    # 翻訳の進捗で進捗メッセージを上書きする（レート制限/スパム回避のため間引く）。
    throttle = ProgressThrottle()

    async def on_progress(ev: ProgressEvent) -> None:
        if not throttle.should_emit(ev):
            return
        await progress.update(render_progress(ev))

    translate_task = asyncio.create_task(
        translator.translate_pdf(
            src,
            job_dir,
            lang_in=lang_in,
            lang_out=settings.lang_out,
            base_url=settings.translate_base_url,
            api_key=settings.translate_api_key,
            model=settings.translate_model,
            on_progress=on_progress,
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
        await progress.update("⚠️ 翻訳に失敗しました。")
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

    await progress.update(
        "✅ 完了しました。" if posted else "⚠️ 翻訳PDFを生成できませんでした。"
    )
