"""処理パイプライン統括。

抽出 → （要約 ∥ 翻訳）を並行実行し、完了したものからスレッドへ投稿する。
要約は先に返ることが多いので先出しし、翻訳は mono → dual の順に添付する。

翻訳は「ローカル翻訳（同時実行を `TranslationGate` で制限）」が基本だが、順番待ちの
間に投稿者が「外部サービスを使用」ボタンを押すと、待機列から離脱して外部サービスで
即座に翻訳できる（ローカルの順番待ちをスキップする）。ローカル翻訳が走り出した後でも
同じボタンで外部サービスへ切り替えられる（実行中の翻訳を止めてやり直す）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings
from ..platforms.base import JobControl, JobRequest, PlatformAdapter, TranslationOverride
from . import extractor, summarizer, translator
from .llm_client import make_client
from .progress import ProgressEvent, ProgressThrottle, render_phase, render_progress

if TYPE_CHECKING:
    from ..jobs.queue import TranslationGate

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
    gate: TranslationGate,
) -> None:
    # 受付〜完了までを1つのメッセージで表現する（順番待ち・翻訳中はここを上書きする）。
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
        await progress.update(render_phase("⬇️", "ダウンロード中", pct, _fmt_bytes(received, total)))

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

    # 3. 要約は翻訳と独立して並行に走らせ、準備でき次第スレッドへ先出しする。
    #    （ローカル翻訳の順番待ち中でも要約だけは早く届けられる。）
    summary_client = make_client(settings.summary_base_url, settings.summary_api_key)
    summary_task = asyncio.create_task(
        summarizer.summarize(summary_client, settings.summary_model, text)
    )

    async def deliver_summary() -> None:
        try:
            summary = await summary_task
            await adapter.post_text(req, f"📝 **要約**\n\n{summary}")
        except Exception:
            logger.exception("要約失敗: %s", req.id)
            await adapter.post_text(req, "⚠️ 要約の生成に失敗しました。")

    summary_deliver = asyncio.create_task(deliver_summary())

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

    # 4. 「外部サービスへ切替」ボタンを翻訳開始前に提示する（対応アダプタのみ。
    #    順番待ち中でも押せるようにするため、スロット確保より先に出す）。
    control = JobControl()
    control_ui = await adapter.offer_translation_switch(req, control)
    switch_fut = control.switch_requested

    # 5. ローカル翻訳スロットの確保を待つ（順番待ち表示）。待っている間に外部サービス
    #    への切替が要求されたら、待機列から離脱して外部サービスで翻訳する。
    slot_held = False

    async def on_wait(position: int) -> None:
        if control_ui is not None:
            await progress.update(
                f"⏳ ローカル翻訳の順番待ちです（{position} 番目）。\n"
                "「外部サービスを使用」ボタンを押すと、待たずに翻訳を始められます。"
            )
        else:
            await progress.update(f"⏳ ローカル翻訳の順番待ちです（{position} 番目）。")

    acquire_task = asyncio.create_task(gate.acquire(on_position=on_wait))
    await asyncio.wait({acquire_task, switch_fut}, return_when=asyncio.FIRST_COMPLETED)

    if switch_fut.done():
        # 順番待ちをスキップして外部サービスで翻訳する（ボタンが押された）。
        if acquire_task.done() and not acquire_task.cancelled():
            # ちょうどスロットを取得していたら返し、次の順番待ちに譲る。
            await gate.release()
        else:
            acquire_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await acquire_task
        override = switch_fut.result()
        logger.info("順番待ちをスキップし %s で翻訳します: %s", override.label, req.id)
        prefix = f"🔀 外部サービス（{override.label}）で翻訳中"
        await progress.update(f"🔀 {override.label} で翻訳します…")
        translate_task = start_translation(override)
    else:
        # 順番が来てローカル翻訳スロットを確保できた。切替は実行フェーズで受け付ける。
        acquire_task.result()  # 取得確定（例外なし）
        slot_held = True
        await progress.update("🌐 ローカル翻訳を開始します…")
        translate_task = start_translation(None)

    # 6. 翻訳結果（mono → dual）。ローカル実行中はさらに外部切替を受け付ける
    #    （翻訳完了 or 切替要求の先着で待つ。切替は一度きり）。
    try:
        # ローカル翻訳中で、かつ切替がまだ消費されていなければ実行中切替を待てる。
        pending_switch: asyncio.Future | None = (
            switch_fut if (slot_held and not switch_fut.done()) else None
        )
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
            # ローカルから外部へ移るのでスロットを返し、次の順番待ちを繰り上げる。
            if slot_held:
                await gate.release()
                slot_held = False
            prefix = f"🔀 外部サービス（{override.label}）で翻訳中"
            await progress.update(f"🔀 {override.label} に切り替えて翻訳し直します…")
            translate_task = start_translation(override)
    except Exception:
        logger.exception("翻訳失敗: %s", req.id)
        await progress.update("⚠️ 翻訳に失敗しました。")
        if control_ui is not None:
            await control_ui.close("⚠️ 翻訳に失敗しました。")
        with contextlib.suppress(Exception):
            await summary_deliver
        return
    finally:
        # 成功・例外いずれでもローカルスロットは必ず返す（実行中切替で解放済みなら何もしない）。
        if slot_held:
            await gate.release()
            slot_held = False

    if control_ui is not None:
        note = "🔀 外部サービスで翻訳しました。" if control.requested else "✅ 翻訳が完了しました。"
        await control_ui.close(note)

    # 要約の先出しが未完なら待ち合わせる（この時点ではほぼ完了しているはず）。
    with contextlib.suppress(Exception):
        await summary_deliver

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

    await progress.update("✅ 完了しました。" if posted else "⚠️ 翻訳PDFを生成できませんでした。")
