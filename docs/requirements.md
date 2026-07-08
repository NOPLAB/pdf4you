# pdf4you 要件定義

PDF翻訳＋論文要約 Bot。特定チャンネルに投稿されたPDFを検知し、翻訳（pdf2zh）と要約を行ってスレッドに返信する。

## 1. 確定した選定

| 項目 | 決定 |
|---|---|
| プラットフォーム | Slack と Discord の両方（共通コア＋アダプタ構成） |
| 翻訳エンジン | pdf2zh-next（PDFMathTranslate-next / BabelDOC）の OpenAI互換 translator |
| 要約 | LLM（OpenAI互換API経由） |
| 推論基盤 | vLLM と Ollama の両方を切替可能（どちらもOpenAI互換で抽象化） |
| PDF出力 | mono と dual を両方自動生成・投稿（要約と並行して翻訳、mono→dualの順） |
| 要約形式 | TL;DR（数行）＋ 構造化した詳細の両方 |
| 翻訳方向 | 原文言語オート判定 → 日本語出力（LANG_IN=auto / LANG_OUT=ja） |
| 使用モデル | 翻訳用・要約用を別々に設定可（同一値にもできる） |
| 対象PDF | 論文以外も区別せず一律に要約・翻訳 |
| アクセス制御 | ALLOWED_USERS 未設定なら誰でも可、設定すると特定ユーザー限定 |
| 実行環境 | VPS 常時稼働 / Docker |
| 言語・ツール | Python + uv |

翻訳・要約とも「OpenAI互換HTTPエンドポイントを叩く」ことに抽象化される。
バックエンドが vLLM / Ollama / 外部商用APIのいずれでも、`base_url` / `api_key` / `model`
の設定差し替えのみで対応する。

## 2. 主要フロー（インタラクティブ処理）

```
1. 監視チャンネルにPDFが投稿される
2. Bot が添付を検知 → アクセス制御を確認 → スレッドに「受付ました」＋進捗表示
3. ジョブをキューに投入（同時実行を制御）
4. ワーカーが並行処理:
   - PDFからテキスト抽出 → 要約（TL;DR＋詳細）を生成
   - pdf2zh-next で翻訳 → mono PDF を生成
5. 続けて dual PDF を生成（monoの翻訳結果を再利用しレンダリングのみ）
6. スレッドへ投稿（完了したものから順次）:
   - 要約（TL;DR＋詳細）
   - mono PDF
   - dual PDF
```

### 翻訳・投稿方針
- 要約生成と mono 翻訳を並行実行し、完了次第スレッドへ投稿する。
- 続けて dual を生成（BabelDOC の翻訳結果を再利用しレンダリングのみ）して投稿する。
- mono / dual を常に両方出力する。ボタン等のインタラクションは用いない。

## 3. アーキテクチャ / モジュール構成

```
pdf4you/
├─ pyproject.toml            # uv 管理
├─ .env.example              # 設定テンプレート
├─ docker-compose.yml        # Bot本体（＋任意でOllama/Redis）
├─ src/pdf4you/
│  ├─ core/
│  │  ├─ pipeline.py         # 抽出→要約→翻訳の統括
│  │  ├─ translator.py       # pdf2zh-next 呼び出し（OpenAI互換設定 / TOML）
│  │  ├─ summarizer.py       # OpenAI互換で要約（TL;DR＋詳細）
│  │  ├─ extractor.py        # PDFテキスト抽出（pymupdf 等）
│  │  └─ llm_client.py       # OpenAI互換クライアント（vLLM/Ollama/外部）
│  ├─ jobs/
│  │  ├─ queue.py            # ジョブキュー・同時実行制御
│  │  └─ store.py            # ジョブ/ファイル状態（SQLite）
│  ├─ platforms/
│  │  ├─ base.py             # 抽象アダプタ（投稿/添付/スレッド返信/アクセス制御）
│  │  ├─ slack_bot.py        # Slack Bolt (Socket Mode)
│  │  └─ discord_bot.py      # discord.py
│  └─ config.py              # 設定ロード
└─ docs/requirements.md
```

### プラットフォーム抽象化
「スレッドに返信」「ファイル添付」を共通インターフェースに切り、Slack/Discord 実装を
差し込む。監視チャンネル判定とアクセス制御（ALLOWED_USERS）も共通層で行う。
インタラクション（ボタン等）は用いず、mono/dual を順次投稿する。

## 4. 技術スタック案

- uv: パッケージ／仮想環境管理
- pdf2zh-next（PDFMathTranslate-next / BabelDOC）: レイアウト保持翻訳、OpenAI translator、TOML設定、mono/dual出力、Python API
- PyMuPDF: 要約用のテキスト抽出
- openai SDK（`base_url` 差し替えで vLLM/Ollama/外部に対応）
- slack-bolt（Socket Mode）＋ discord.py
- SQLite（aiosqlite）: ジョブ・ファイルの状態管理
- ジョブキュー: 単一VPSなら asyncio.Queue＋ワーカー（軽量）／将来スケールなら RQ＋Redis

## 5. 非機能・運用要件

- 同時実行制御: 翻訳・推論は重いため、キューで直列〜N並列に制限（GPU占有を考慮）
- タイムアウト／リトライ: 大きなPDFや推論失敗時の再試行とユーザー通知
- 進捗フィードバック: 「要約中→翻訳中→完了」をスレッドで逐次更新
- ファイル管理: 一時ファイルの保存先と保持期間（処理後に自動削除 or N日保持）
- 秘匿情報: トークン類は .env／Docker secrets。リポジトリに含めない
- ログ: ジョブ単位の構造化ログ

## 6. 設定項目（環境変数の想定）

```
# プラットフォーム
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=            # Socket Mode
SLACK_WATCH_CHANNELS=
DISCORD_BOT_TOKEN=
DISCORD_WATCH_CHANNELS=

# アクセス制御（未設定なら誰でも可 / 設定すると限定）
SLACK_ALLOWED_USERS=
DISCORD_ALLOWED_USERS=

# 翻訳（pdf2zh-next / OpenAI互換）
TRANSLATE_BASE_URL=        # 例: http://vllm:8000/v1
TRANSLATE_API_KEY=
TRANSLATE_MODEL=
LANG_IN=auto
LANG_OUT=ja

# 要約（OpenAI互換 / 翻訳とは別モデル可）
SUMMARY_BASE_URL=          # 例: http://ollama:11434/v1
SUMMARY_API_KEY=
SUMMARY_MODEL=

# 動作
MAX_CONCURRENCY=1
MAX_PDF_MB=50              # 上限は要調整（TBD）
FILE_RETENTION_DAYS=7
```

## 7. 確定した仕様（旧TBD）

1. 対象言語: 原文言語オート判定 → 日本語出力（LANG_IN=auto / LANG_OUT=ja）
2. 使用モデル: 翻訳用・要約用を別々に設定可（同一値も可）
3. 非論文PDF: 区別せず一律に要約・翻訳
4. 利用範囲: ALLOWED_USERS 未設定なら誰でも可、設定すると特定ユーザー限定
5. dual戦略: ボタン廃止。要約と並行して mono を生成し、続けて dual を生成、mono/dual を両方投稿
6. 使用モデル名（具体）: vLLM/Ollama で動かす実モデルは実装時に .env で指定

## 8. 残TBD

1. ファイルサイズ・ページ数の上限、超過時の挙動（暫定 MAX_PDF_MB=50）
