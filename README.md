# pdf4you

特定のチャンネル（Slack / Discord）に投稿されたPDFを検知し、**pdf2zh-next** で翻訳（mono/dual）
しつつ **論文要約**（TL;DR＋詳細）を生成して、元投稿のスレッドに返信するBotです。

- 翻訳・要約とも **OpenAI互換API** を叩く抽象化。バックエンドは vLLM / Ollama / 外部いずれも
  `base_url` / `api_key` / `model` の差し替えのみで対応。
- 翻訳方向は **原文言語オート判定 → 日本語**（`LANG_IN=auto` / `LANG_OUT=ja`）。
- 要約と mono 翻訳を **並行実行**し、続けて dual を生成。**mono / dual を両方**スレッドへ投稿。
- Slack / Discord を **単一プロセス**で並行起動。

詳細な要件は [docs/requirements.md](docs/requirements.md) を参照。

## セットアップ

```bash
uv sync
cp .env.example .env   # 各種トークン・エンドポイントを設定
```

## 起動

```bash
uv run pdf4you
```

`.env` に Slack / Discord いずれかのトークンを設定すると、そのプラットフォームのアダプタが起動します。
両方設定すれば両方同時に動きます。

## 構成

```
src/pdf4you/
├─ config.py         設定ロード（pydantic-settings）
├─ __main__.py       エントリポイント（Slack/Discord/ワーカーを並行起動）
├─ core/             抽出・要約・翻訳・パイプライン
├─ jobs/             ジョブキュー・状態管理
└─ platforms/        Slack / Discord アダプタ（共通インターフェース）
```

## Slack のセットアップ

1. [api.slack.com/apps](https://api.slack.com/apps) でアプリを作成（From scratch）。
2. **Socket Mode** を有効化し、App-Level Token を発行（scope: `connections:write`）→ `SLACK_APP_TOKEN`（`xapp-...`）。
3. **OAuth & Permissions** の Bot Token Scopes に `chat:write` / `files:read` / `channels:history`
   （プライベートch等を使う場合は `groups:history` なども）を追加。
4. **Event Subscriptions** で bot events に `message.channels`（必要に応じ `message.groups` 等）を購読。
5. ワークスペースにインストールし、Bot User OAuth Token → `SLACK_BOT_TOKEN`（`xoxb-...`）。
6. Bot を対象チャンネルに招待する。

## Discord のセットアップ

1. [Discord Developer Portal](https://discord.com/developers/applications) で Application → Bot を作成。
   Bot Token → `DISCORD_BOT_TOKEN`。
2. **Privileged Gateway Intents** の **MESSAGE CONTENT INTENT** を有効化。
3. OAuth2 URL Generator で scope=`bot`、権限は
   Send Messages / Create Public Threads / Send Messages in Threads / Attach Files / Read Message History。
4. 生成された URL でサーバーに招待する。

## 翻訳モデルの事前取得（推奨）

pdf2zh-next は初回にレイアウト解析モデル等をダウンロードします。オフライン運用や
初回の待ち時間短縮のため、事前に warmup しておけます（Docker ビルドでは自動実行）。

```bash
uv run pdf2zh_next --warmup
```

## Docker で常時稼働

```bash
cp .env.example .env   # トークン・エンドポイントを設定
docker compose up -d --build
```

## 補足

- **入力言語**: pdf2zh-next は言語オート判定を持たないため、`LANG_IN=auto` の場合は
  抽出テキストから Bot 側で言語を判定して pdf2zh-next に渡します。既知の言語は明示指定
  （例 `LANG_IN=en`）も可能です。
- **Python**: pdf2zh-next の推奨に合わせ **3.12** を使用します（`.python-version`）。
