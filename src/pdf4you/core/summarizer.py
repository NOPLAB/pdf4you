"""論文要約。OpenAI互換 API で TL;DR＋構造化した詳細の日本語要約を生成する。"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "あなたは学術論文を的確に要約する日本語アシスタントです。"
    "与えられた本文のみに基づき、推測や誇張をせず、簡潔かつ正確に要約してください。"
)

_INSTRUCTION = """次の文書を日本語で要約してください。以下のフォーマットを厳密に守ってください。

## TL;DR
（3行以内で、この論文の要点を一言で）

## 詳細
- **背景・目的**:
- **手法**:
- **主な結果**:
- **貢献・新規性**:
- **限界・今後の課題**:

文書本文:
---
{content}
---
"""


async def summarize(
    client: AsyncOpenAI,
    model: str,
    text: str,
    *,
    max_input_chars: int = 24000,
) -> str:
    """本文テキストから要約テキスト（Markdown）を返す。

    長文はコンテキスト長対策として先頭 `max_input_chars` 文字に切り詰める
    （論文はアブストラクト・序論・結論に要点が集中するため実用上有効）。
    """
    content = text.strip()[:max_input_chars]
    if not content:
        return "（本文テキストを抽出できませんでした。画像PDFの可能性があります。）"

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _INSTRUCTION.format(content=content)},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()
