# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Slack / Discord に投稿された PDF を検知し、**pdf2zh-next** で翻訳（mono/dual）しつつ **要約**（TL;DR＋詳細）を生成してスレッドへ返信する Bot。翻訳・要約とも **OpenAI 互換 API** を叩く形に抽象化されており、バックエンド（vLLM / Ollama / OpenRouter 等）は `base_url` / `api_key` / `model` の差し替えだけで切り替わる。

コード・コメント・ドキュメント・コミットメッセージはすべて**日本語**で書かれている。踏襲すること。

## コマンド

```bash
uv sync                          # 依存インストール（.venv 構築）
uv run pdf4you                   # Bot 起動（.env に設定されたプラットフォームだけ起動）
uv run ruff check .              # Lint（select = E, F, I, UP, B / line-length 100 / py312）
uv run ruff format .             # フォーマット
uv run pytest                    # テスト（※ 現状テストは未整備。pytest は dev 依存のみ）
uv run pdf2zh_next --warmup      # 翻訳レイアウトモデルの事前取得（初回起動の高速化）
docker compose up -d --build     # Docker 常時稼働（ビルド時に warmup を自動実行）
```

`.env`（`.env.example` からコピー）に Slack / Discord いずれかのトークンを設定するとそのアダプタが起動する。両方設定すれば両方同時に動く。どちらも未設定なら起動時に `SystemExit`。

## アーキテクチャ

**単一プロセス・全 asyncio**。`__main__.run()` が Slack/Discord アダプタとジョブワーカーを `asyncio.gather` で並行起動する。3 層構成：

- **`platforms/`** — プラットフォーム抽象化。`base.PlatformAdapter` が「ダウンロード／スレッド投稿／ファイル添付／進捗メッセージ編集／切替ボタン提示／アクセス制御」を抽象メソッドで定義し、`slack_bot.py`（Bolt Socket Mode）と `discord_bot.py`（discord.py）が実装する。パイプラインはプラットフォーム非依存。
- **`jobs/`** — `queue.JobQueue`（`asyncio.Queue` ＋ `MAX_CONCURRENCY` 個のワーカー）、`store.FileStore`（ジョブ単位の作業ディレクトリと保持期間クリーンアップ）、`keystore.UserKeyStore`（ユーザー別 API キーを Fernet 暗号化して aiosqlite に保存）。
- **`core/`** — `pipeline.process_job` が統括。`extractor`（PyMuPDF 抽出＋言語判定）→ `summarizer`（要約）∥ `translator`（pdf2zh-next）を並行実行し、完了順にスレッドへ投稿する。`progress` は進捗バー整形とスロットリング、`llm_client` は OpenAI 互換クライアント生成。

**データフロー**: アダプタが PDF を検知 → `JobRequest` を生成 → `queue.enqueue` → ワーカーが `process_job(req, adapter, settings, job_dir)` を実行。`JobRequest.meta` は同一プロセス内のライブオブジェクト受け渡しに使う自由領域（Discord は `thread` / `attachment` オブジェクトをここで渡す）。

## 押さえるべき非自明な設計

- **OpenAI 互換抽象化が全体の要**。翻訳（pdf2zh-next の `OpenAISettings`）も要約も同じ「`base_url`/`api_key`/`model`」の三点で表現される。バックエンドを増やす作業＝新しいエンドポイント設定を渡すだけ。

- **翻訳の外部切替**（`pipeline.py` 4〜5節が中核）。翻訳開始と同時に「外部サービスを使用」ボタンを提示。押下時は `JobControl.request_switch()` が `asyncio.Future` を解決し、パイプラインが `asyncio.wait(FIRST_COMPLETED)` で「翻訳完了」と「切替要求」を競わせる。切替が勝てば実行中の翻訳タスクを `cancel()` して OpenRouter で**最初からやり直す**。切替は **1 ジョブ 1 回**、ローカルが先に完走したら完了優先。要約は切替の影響を受けない。ボタンは**投稿者本人のみ**操作可（`req.user_id` で検証）。

- **`SECRET_KEY`（Fernet 鍵）でキー機能全体がゲートされる**。`Settings.keys_enabled` が False なら `keystore` は `None` になり、`/setkey`・`/keystatus`・`/forgetkey`・切替ボタンがすべて無効化される。キー機能に触るコードは必ず `keystore is None` を確認する。API キーは平文でログにも DB にも残さない（保存前に Fernet 暗号化、表示は `mask_key`）。

- **`LANG_IN=auto` は Bot 側で解決する**。pdf2zh-next は言語オート判定を持たないため、`extractor.resolve_lang_in` が抽出テキストから langdetect で判定し pdf2zh-next 用コードに正規化してから渡す。

- **mono/dual は 1 回の翻訳で両方生成される**（pdf2zh-next の仕様）。LLM 呼び出しは 1 回。`make_mono` と `make_dual` の両方 False は不可。

- **重い SDK は遅延 import**。Slack/Discord SDK（`__main__` 内）と pdf2zh-next（`translator.translate_pdf` 内）は使う時だけ import して起動を速く保つ。

- **進捗はスロットリングされる**。`ProgressThrottle` が「一定秒経過＋一定 % 進行 or 工程切替」のときだけ更新を許可し、Slack のレート制限・メッセージ編集スパムを避ける。翻訳を切り替えるたびに throttle を作り直す（新翻訳の初回を必ず出すため）。同期の抽出処理は `asyncio.to_thread` に逃がし、別スレッドからの進捗は `run_coroutine_threadsafe` でループへ委譲する。

- **DM 翻訳**（`ALLOW_DM=true`）。監視チャンネル設定を無視するがアクセス制御（`ALLOWED_USERS`）は適用。Discord は DM 内でスレッドを作れないので直接返信、Slack は IM 購読とスコープが別途必要。

- **スラッシュコマンドの登録差**: Discord は起動時に `CommandTree.sync()` で自動同期。**Slack はアプリ設定側で手動宣言が必須**（コードから登録できない）— README のセットアップ手順参照。

## 補足

- Python は pdf2zh-next の推奨に合わせ **3.12**（`.python-version`）。
- 設定のカンマ区切り一覧（watch channels / allowed users）は文字列で受け、`*_set` プロパティで集合に変換する（pydantic の complex-type パースと空文字問題を避けるため）。
- 詳細な要件・仕様の背景は `docs/requirements.md` にある。
