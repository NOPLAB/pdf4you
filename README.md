# pdf4you

特定のチャンネル（Slack / Discord）に投稿されたPDFを検知し、**pdf2zh-next** で翻訳（mono/dual）
しつつ **論文要約**（TL;DR＋詳細）を生成して、元投稿のスレッドに返信するBotです。

- 翻訳・要約とも **OpenAI互換API** を叩く抽象化。バックエンドは vLLM / Ollama / 外部いずれも
  `base_url` / `api_key` / `model` の差し替えのみで対応。
- 翻訳方向は **原文言語オート判定 → 日本語**（`LANG_IN=auto` / `LANG_OUT=ja`）。
- 要約と mono 翻訳を **並行実行**し、続けて dual を生成。**mono / dual を両方**スレッドへ投稿。
- Slack / Discord を **単一プロセス**で並行起動。
- **DM 翻訳**: 監視チャンネルだけでなく、Bot への **DM に PDF を送っても翻訳**できる（`ALLOW_DM`）。
- **ユーザー別 API キー**: DM のスラッシュコマンド `/setkey` で外部サービス（OpenRouter 等の
  OpenAI 互換 API）のキー・モデル・Base URL を各自登録。翻訳中に **「外部サービスを使用」**
  ボタンで外部サービスへ切替可能。

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
   / `commands`（プライベートch等を使う場合は `groups:history`、**DM利用なら `im:history`** も）を追加。
4. **Event Subscriptions** で bot events に `message.channels`（必要に応じ `message.groups`、
   **DM利用なら `message.im`**）を購読。
5. **Interactivity & Shortcuts** を ON（Socket Mode なので Request URL 不要）。
6. **Slash Commands** に `/setkey` `/keystatus` `/forgetkey` `/pdfhelp` を登録（同上、Request URL 不要）。
   ※ ヘルプは Slack 組み込みの `/help` を上書きできないため `/pdfhelp` という名前にしている。
7. **App Home** → Messages Tab を有効化し「Allow users to send Slash commands and messages
   from the messages tab」を ON（Bot への DM を受け取るため）。
8. ワークスペースにインストールし、Bot User OAuth Token → `SLACK_BOT_TOKEN`（`xoxb-...`）。
9. Bot を対象チャンネルに招待する（DM だけで使うなら招待は不要）。

## Discord のセットアップ

1. [Discord Developer Portal](https://discord.com/developers/applications) で Application → Bot を作成。
   Bot Token → `DISCORD_BOT_TOKEN`。
2. **Privileged Gateway Intents** の **MESSAGE CONTENT INTENT** を有効化。
3. OAuth2 URL Generator で scope=`bot` と **`applications.commands`**、権限は
   Send Messages / Create Public Threads / Send Messages in Threads / Attach Files / Read Message History。
4. 生成された URL でサーバーに招待する。
5. スラッシュコマンド（`/setkey` ほか）は起動時に自動同期される（Bot の DM でも利用可）。
   反映まで数分かかる場合がある。
6. Bot への DM で PDF を送っても翻訳できる（DM するには Bot と同じサーバーに所属しているか、
   アプリがユーザーインストール対応であること）。

## ヘルプコマンド

使い方・コマンド一覧・外部サービスのセットアップ手順を Bot 自身が案内します。

- Discord: `/help`
- Slack: `/pdfhelp`（組み込みの `/help` と衝突するため別名）

応答は実行した本人にのみ表示されます（ephemeral）。

## ユーザー別 API キーと外部サービス切替

ローカル推論（vLLM/Ollama）に加えて、ユーザーが自分の API キーで **外部サービス**
（OpenAI 互換 API なら何でも可）を使って翻訳を実行できます。既定の切替先は `.env` の
`EXTERNAL_BASE_URL` / `EXTERNAL_MODEL` で設定します（初期値は OpenRouter）。

1. `SECRET_KEY` を `.env` に設定（未設定ならこの機能は無効）。
   生成: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`
2. 各ユーザーが使いたい外部サービスの **API キー**を用意する。
   既定の OpenRouter を使う場合:
   1. [openrouter.ai](https://openrouter.ai/) でアカウントを作成（Google アカウント等でサインアップ可）。
   2. [Keys](https://openrouter.ai/settings/keys) → **Create Key** でキーを発行し、
      表示された `sk-or-v1-...` をコピーする（あとから再表示できないので注意）。
   3. 有料モデルを使う場合は [Credits](https://openrouter.ai/settings/credits) で残高をチャージする。
3. 各ユーザーが Bot の **DM でスラッシュコマンド**を実行:
   - `/setkey` — モーダルに API キー（と任意でモデル名・Base URL）を入力して登録。
     空欄の項目は `.env` の既定値が使われる。OpenRouter 以外のサービスを使いたい場合は
     Base URL 欄にそのサービスのエンドポイントを入力する。
     入力値はチャットに残らず、応答は本人だけに見える（ephemeral）。
   - `/keystatus` — 登録状態をマスク表示で確認。
   - `/forgetkey` — 登録済みキーを削除。
4. PDF を投稿して翻訳が始まると、スレッドに **「外部サービスを使用」**
   ボタンが出る。押すと、実行中のローカル翻訳を停止し、登録済みキーで外部サービスに切り替えて
   最初から翻訳し直す（要約は切替の影響を受けない）。ボタンは **投稿者本人のみ**操作できる。

キーは **Fernet で暗号化**して SQLite（`USERKEY_DB`）に保存され、平文はログにも DB にも残りません。

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
