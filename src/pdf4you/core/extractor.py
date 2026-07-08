"""PDFテキスト抽出と入力言語の判定。

pdf2zh-next は `lang_in=auto` を持たないため、`LANG_IN=auto` の場合はここで
抽出テキストから言語を判定し、pdf2zh-next 用の言語コードに正規化する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

# langdetect の一部コードを pdf2zh-next / BabelDOC 想定のコードに寄せる
_LANG_MAP = {"zh-cn": "zh", "zh-tw": "zh"}


def extract_text(pdf_path: Path, max_chars: int | None = None) -> str:
    """PDF全文（またはmax_charsまで）のプレーンテキストを返す。"""
    doc = pymupdf.open(pdf_path)
    try:
        parts: list[str] = []
        total = 0
        for page in doc:
            t = page.get_text("text")
            parts.append(t)
            total += len(t)
            if max_chars and total >= max_chars:
                break
    finally:
        doc.close()
    text = "\n".join(parts)
    return text[:max_chars] if max_chars else text


def normalize_lang(code: str) -> str:
    code = code.lower()
    if code in _LANG_MAP:
        return _LANG_MAP[code]
    return code.split("-")[0]


def detect_language(text: str, default: str = "en") -> str:
    """先頭付近のテキストから言語コード（ISO 639-1系）を推定する。"""
    sample = text.strip()[:2000]
    if not sample:
        return default
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0  # 判定の再現性を確保
        return detect(sample)
    except Exception:
        logger.warning("言語判定に失敗しました。%s にフォールバックします", default)
        return default


def resolve_lang_in(configured: str, text: str) -> str:
    """設定値が `auto` なら判定、そうでなければそのまま返す（pdf2zh-next用に正規化）。"""
    if configured.strip().lower() != "auto":
        return configured
    detected = normalize_lang(detect_language(text))
    logger.info("入力言語をオート判定しました: %s", detected)
    return detected
