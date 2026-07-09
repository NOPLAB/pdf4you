"""エントリポイント。Slack / Discord アダプタとジョブワーカーを単一プロセスで並行起動する。"""

from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .core.pipeline import process_job
from .jobs.keystore import UserKeyStore
from .jobs.queue import JobQueue, TranslationGate
from .jobs.store import FileStore
from .platforms.base import JobRequest, PlatformAdapter


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level)
    log = logging.getLogger("pdf4you")

    store = FileStore(settings.work_dir, settings.file_retention_days)
    store.cleanup_old()

    # ユーザーAPIキー保管庫（SECRET_KEY 未設定なら機能無効）。
    keystore: UserKeyStore | None = None
    if settings.keys_enabled:
        try:
            keystore = UserKeyStore(settings.userkey_db, settings.secret_key)
            await keystore.init()
            log.info("ユーザーAPIキー機能を有効化しました（DB: %s）", settings.userkey_db)
        except Exception:
            log.exception("SECRET_KEY が不正のためキー機能を無効化します")
            keystore = None
    else:
        log.warning("SECRET_KEY 未設定のため、キー登録・翻訳の外部切替は無効です。")

    adapters: dict[str, PlatformAdapter] = {}

    # ローカル翻訳の同時実行を絞るゲート（外部サービス翻訳はこの制限を受けない）。
    gate = TranslationGate(settings.max_concurrency)

    async def worker(req: JobRequest) -> None:
        adapter = adapters.get(req.platform)
        if adapter is None:
            log.error("未知のプラットフォーム: %s", req.platform)
            return
        job_dir = store.job_dir(req.id)
        await process_job(req, adapter, settings, job_dir, gate)

    queue = JobQueue(worker)

    # トークンが設定されている方だけ起動（重いSDKは遅延import）
    if settings.slack_enabled:
        from .platforms.slack_bot import SlackAdapter

        adapters["slack"] = SlackAdapter(settings, queue.enqueue, keystore)
    if settings.discord_enabled:
        from .platforms.discord_bot import DiscordAdapter

        adapters["discord"] = DiscordAdapter(settings, queue.enqueue, keystore)

    if not adapters:
        raise SystemExit(
            "Slack / Discord いずれのトークンも設定されていません。.env を確認してください。"
        )

    log.info("pdf4you 起動: %s", ", ".join(adapters))
    await asyncio.gather(*(a.start() for a in adapters.values()))


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
