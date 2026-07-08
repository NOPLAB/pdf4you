FROM python:3.12-slim

# uv を導入
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# pdf2zh-next / pymupdf 等の実行に必要なシステムライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存を先に解決してレイヤキャッシュを効かせる
COPY pyproject.toml uv.lock* .python-version ./
RUN uv sync --no-install-project

# アプリ本体
COPY . .
RUN uv sync

# BabelDOC のレイアウト解析モデル・フォントをビルド時に取得（初回起動を高速化）
RUN uv run pdf2zh_next --warmup || true

ENV WORK_DIR=/app/work
CMD ["uv", "run", "pdf4you"]
