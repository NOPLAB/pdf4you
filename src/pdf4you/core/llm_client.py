"""OpenAI互換 API クライアントの生成。

翻訳（pdf2zh-next 側で使用）と要約でエンドポイント/モデルを分けられるよう、
`base_url` / `api_key` を差し替えてクライアントを作る薄いヘルパ。
vLLM / Ollama / 外部いずれも同じインターフェースで扱える。
"""

from __future__ import annotations

from openai import AsyncOpenAI


def make_client(base_url: str, api_key: str) -> AsyncOpenAI:
    # ローカルLLM（Ollama等）は api_key 任意。空ならダミー値を入れる。
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "dummy")
