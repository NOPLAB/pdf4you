"""pdf2zh-next（PDFMathTranslate-next / BabelDOC）による翻訳。

公式の Python API（`SettingsModel` + `OpenAISettings` + `do_translate_async_stream`）を
使い、1回の翻訳で mono / dual PDF を生成する。翻訳エンジンは OpenAI互換なので
`openai_base_url` を差し替えれば vLLM / Ollama / 外部いずれも利用できる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TranslateResult:
    mono_path: Path | None
    dual_path: Path | None


def _to_path(value: object) -> Path | None:
    return Path(str(value)) if value else None


async def translate_pdf(
    src: Path,
    out_dir: Path,
    *,
    lang_in: str,
    lang_out: str,
    base_url: str,
    api_key: str,
    model: str,
    make_mono: bool = True,
    make_dual: bool = True,
) -> TranslateResult:
    """`src` を翻訳し、`out_dir` に mono/dual PDF を生成してパスを返す。

    pdf2zh-next は mono/dual を同一の翻訳処理から生成するため、両方を要求しても
    翻訳（LLM呼び出し）は1回で済む。`make_mono` と `make_dual` の両方を False には
    できない（pdf2zh-next 側の制約）。
    """
    if not (make_mono or make_dual):
        raise ValueError("make_mono と make_dual の両方を False にはできません")

    # 重い依存なので遅延 import（起動を速くし、翻訳を使わない実行時の負荷を避ける）
    from pdf2zh_next import OpenAISettings, SettingsModel
    from pdf2zh_next.high_level import do_translate_async_stream

    out_dir.mkdir(parents=True, exist_ok=True)

    settings = SettingsModel(
        translate_engine_settings=OpenAISettings(
            openai_model=model,
            openai_base_url=base_url,
            openai_api_key=api_key or "dummy",
        ),
    )
    settings.translation.lang_in = lang_in
    settings.translation.lang_out = lang_out
    settings.translation.output = str(out_dir)
    settings.pdf.no_mono = not make_mono
    settings.pdf.no_dual = not make_dual

    mono_path: Path | None = None
    dual_path: Path | None = None

    async for event in do_translate_async_stream(settings, str(src)):
        etype = event.get("type")
        if etype == "finish":
            result = event["translate_result"]
            mono_path = _to_path(getattr(result, "mono_pdf_path", None))
            dual_path = _to_path(getattr(result, "dual_pdf_path", None))
            logger.info("翻訳完了: mono=%s dual=%s", mono_path, dual_path)
        elif etype == "error":
            raise RuntimeError(f"pdf2zh-next 翻訳エラー: {event.get('error')}")

    return TranslateResult(mono_path, dual_path)
