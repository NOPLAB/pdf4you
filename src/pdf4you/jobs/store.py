"""ジョブごとの作業ディレクトリ管理と、保持期間を過ぎた生成物のクリーンアップ。"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class FileStore:
    """`work_dir` 配下にジョブ単位のサブディレクトリを切り、古いものを削除する。"""

    def __init__(self, work_dir: Path, retention_days: int):
        self.work_dir = Path(work_dir)
        self.retention_days = retention_days
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        """ジョブ用の作業ディレクトリを作成して返す。"""
        d = self.work_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup_old(self) -> int:
        """保持期間を超えたジョブディレクトリを削除し、削除件数を返す。"""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for child in self.work_dir.iterdir():
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("古い作業ディレクトリを %d 件削除しました", removed)
        return removed
