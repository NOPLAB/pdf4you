"""処理パイプライン統括。

抽出 → （要約 ∥ 翻訳）を並行実行し、完了したものからスレッドへ投稿する。
要約は先に返ることが多いので先出しし、翻訳は mono → dual の順に添付する。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from ..config import Settings
from ..platforms.base import JobControl, JobRequest, PlatformAdapter, TranslationOverride
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
    # throttle は切替のたびに作り直す（新しい翻訳の最初の1回を必ず出すため）。
    throttle = ProgressThrottle()
    prefix = ""  # 切替後は表示にサービス名を添える

    async def on_progress(ev: ProgressEvent) -> None:
        if not throttle.should_emit(ev):
            return
        text = render_progress(ev)
        await progress.update(f"{prefix}\n{text}" if prefix else text)

    def start_translation(override: TranslationOverride | None) -> asyncio.Task:
        nonlocal throttle
        throttle = ProgressThrottle()
        base_url = override.base_url if override else settings.translate_base_url
        api_key = override.api_key if override else settings.translate_api_key
        model = override.model if override else settings.translate_model
        return asyncio.create_task(
            translator.translate_pdf(
                src,
                job_dir,
                lang_in=lang_in,
                lang_out=settings.lang_out,
                base_url=base_url,
                api_key=api_key,
                model=model,
                on_progress=on_progress,
            )
        )

    translate_task = start_translation(None)

    # 翻訳中は「外部サービスへ切替」ボタンを提示（対応アダプタのみ。非対応なら None）。
    control = JobControl()
    control_ui = await adapter.offer_translation_switch(req, control)

    # 4. 要約を先出し
    try:
        summary = await summary_task
        await adapter.post_text(req, f"📝 **要約**\n\n{summary}")
    except Exception:
        logger.exception("要約失敗: %s", req.id)
        await adapter.post_text(req, "⚠️ 要約の生成に失敗しました。")

    # 5. 翻訳結果（mono → dual）。翻訳完了 or 切替要求の先着で待つ（切替は一度きり）。
    try:
        pending_switch: asyncio.Future | None = control.switch_requested
        while True:
            waiters = {translate_task}
            if pending_switch is not None:
                waiters.add(pending_switch)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

            if translate_task in done:
                # 翻訳が先に終わった（成功/失敗）。切替が来ていても完了を優先する。
                result = translate_task.result()
                break

            # 切替要求（翻訳はまだ動作中）。ローカル翻訳を止めて外部サービスで再実行する。
            override = pending_switch.result()
            pending_switch = None  # 切替は一度きり
            logger.info("翻訳を %s へ切り替えます: %s", override.label, req.id)
            translate_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await translate_task
            prefix = f"🔀 外部サービス（{override.label}）で翻訳中"
            await progress.update(f"🔀 {override.label} に切り替えて翻訳し直します…")
            translate_task = start_translation(override)
    except Exception:
        logger.exception("翻訳失敗: %s", req.id)
        await progress.update("⚠️ 翻訳に失敗しました。")
        if control_ui is not None:
            await control_ui.close("⚠️ 翻訳に失敗しました。")
        return

    if control_ui is not None:
        note = "🔀 外部サービスで翻訳しました。" if control.requested else "✅ 翻訳が完了しました。"
        await control_ui.close(note)

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
