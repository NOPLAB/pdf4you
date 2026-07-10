"""エントリポイント。Slack / Discord アダプタとジョブワーカーを単一プロセスで並行起動する。"""

from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .core.pipeline import process_job
from .jobs.jobdb import JobStateStore
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

    # 処理中ジョブの永続化（途中でプロセスが落ちても再起動時に翻訳を再開できるように）。
    jobstate = JobStateStore(settings.jobstate_db)
    await jobstate.init()

    adapters: dict[str, PlatformAdapter] = {}

    # ローカル翻訳の同時実行を絞るゲート（外部サービス翻訳はこの制限を受けない）。
    gate = TranslationGate(settings.max_concurrency)

    async def worker(req: JobRequest) -> None:
        adapter = adapters.get(req.platform)
        if adapter is None:
            log.error("未知のプラットフォーム: %s", req.platform)
            await jobstate.remove(req.id)
            return
        job_dir = store.job_dir(req.id)
        try:
            await process_job(req, adapter, settings, job_dir, gate, jobstate)
        finally:
            # 成功・失敗を問わず処理が「終わった」ジョブは記録を消す。
            # 再開の対象は、途中終了でここまで到達しなかったジョブだけ。
            await jobstate.remove(req.id)

    queue = JobQueue(worker)

    async def enqueue(req: JobRequest) -> None:
        # キュー投入より先に永続化する（直後に落ちても再開できるように）。
        # 記録に失敗してもジョブ自体は流す（再開できないだけ）。
        try:
            await jobstate.add(req)
        except Exception:
            log.exception("ジョブの永続化に失敗しました: %s", req.id)
        await queue.enqueue(req)

    # トークンが設定されている方だけ起動（重いSDKは遅延import）
    if settings.slack_enabled:
        from .platforms.slack_bot import SlackAdapter

        adapters["slack"] = SlackAdapter(settings, enqueue, keystore)
    if settings.discord_enabled:
        from .platforms.discord_bot import DiscordAdapter

        adapters["discord"] = DiscordAdapter(settings, enqueue, keystore)

    if not adapters:
        raise SystemExit(
            "Slack / Discord いずれのトークンも設定されていません。.env を確認してください。"
        )

    async def resume_pending() -> None:
        """前回の実行で中断されたジョブを再投入する（翻訳の再開）。"""
        try:
            pending = await jobstate.list_pending()
        except Exception:
            log.exception("中断ジョブの読み込みに失敗しました")
            return
        if not pending:
            return
        log.info("中断されたジョブを %d 件再開します", len(pending))
        for job in pending:
            adapter = adapters.get(job.platform)
            if adapter is None:
                log.warning(
                    "アダプタが無効のため再開できません: id=%s platform=%s", job.id, job.platform
                )
                await jobstate.remove(job.id)
                continue
            # 投稿できる状態になるまで待つ（Discord は接続完了待ちが必要）。
            await adapter.wait_ready()
            # DB には記録済みなので enqueue（再記録）ではなく直接キューへ入れる。
            await queue.enqueue(job.to_request())

    log.info("pdf4you 起動: %s", ", ".join(adapters))
    await asyncio.gather(*(a.start() for a in adapters.values()), resume_pending())


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
