"""Хранение задач в SQLite.

Очередь живёт в памяти (asyncio.Queue), но её содержимое дублируется в базу.
Смысл: если контейнер перезапустят, задачи в статусе ``pending`` и прерванная
``running`` не пропадут — на старте ``recover_interrupted()`` вернёт их в
очередь, а пользователю уйдёт сообщение, что обработка продолжится.
"""

from __future__ import annotations

from typing import Any, Sequence

import aiosqlite

from app.jobs.models import Job, JobMode, JobStatus, Stage
from app.logging_setup import get_logger
from app.storage.db import Database, now

__all__ = ["JobsRepository"]

log = get_logger(__name__)

_COLUMNS = (
    "id, user_id, chat_id, progress_message_id, request_message_id, url, platform, "
    "video_id, mode, max_height, status, stage, title, duration_sec, error, "
    "result_path, created_at, started_at, finished_at"
)


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        chat_id=int(row["chat_id"]),
        progress_message_id=row["progress_message_id"],
        request_message_id=row["request_message_id"],
        url=row["url"],
        platform=row["platform"],
        video_id=row["video_id"],
        mode=JobMode(row["mode"]),
        max_height=int(row["max_height"]),
        status=JobStatus(row["status"]),
        stage=Stage(row["stage"]),
        title=row["title"],
        duration_sec=row["duration_sec"],
        error=row["error"],
        result_path=row["result_path"],
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class JobsRepository:
    """CRUD по задачам."""

    __slots__ = ("_db",)

    def __init__(self, database: Database) -> None:
        self._db = database

    async def create(self, job: Job) -> Job:
        job.created_at = job.created_at or now()
        job.id = await self._db.execute(
            """
            INSERT INTO jobs (
                user_id, chat_id, progress_message_id, request_message_id, url,
                platform, video_id, mode, max_height, status, stage, title,
                duration_sec, error, result_path, created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.user_id,
                job.chat_id,
                job.progress_message_id,
                job.request_message_id,
                job.url,
                job.platform,
                job.video_id,
                job.mode.value,
                job.max_height,
                job.status.value,
                job.stage.value,
                job.title,
                job.duration_sec,
                job.error,
                job.result_path,
                job.created_at,
                job.started_at,
                job.finished_at,
            ),
        )
        return job

    async def get(self, job_id: int) -> Job | None:
        row = await self._db.fetch_one(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,))
        return _row_to_job(row) if row is not None else None

    async def update(self, job: Job) -> None:
        await self._db.execute(
            """
            UPDATE jobs SET
                progress_message_id = ?, request_message_id = ?, status = ?, stage = ?,
                title = ?, duration_sec = ?, error = ?, result_path = ?,
                started_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                job.progress_message_id,
                job.request_message_id,
                job.status.value,
                job.stage.value,
                job.title,
                job.duration_sec,
                job.error,
                job.result_path,
                job.started_at,
                job.finished_at,
                job.id,
            ),
        )

    async def set_stage(self, job_id: int, stage: Stage) -> None:
        await self._db.execute("UPDATE jobs SET stage = ? WHERE id = ?", (stage.value, job_id))

    async def set_status(
        self,
        job_id: int,
        status: JobStatus,
        *,
        error: str | None = None,
        result_path: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE jobs SET status = ?, error = ?, result_path = COALESCE(?, result_path),
                            finished_at = ?
            WHERE id = ?
            """,
            (status.value, error, result_path, now() if status.is_final else None, job_id),
        )

    async def list_active(self) -> list[Job]:
        """Задачи в очереди и в работе, в порядке поступления."""
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM jobs WHERE status IN (?, ?) ORDER BY id",
            (JobStatus.PENDING.value, JobStatus.RUNNING.value),
        )
        return [_row_to_job(row) for row in rows]

    async def list_recent(self, user_id: int, limit: int = 10) -> list[Job]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [_row_to_job(row) for row in rows]

    async def count_active(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
                (JobStatus.PENDING.value, JobStatus.RUNNING.value),
                default=0,
            )
        )

    async def find_active_duplicate(self, user_id: int, cache_key_parts: Sequence[Any]) -> Job | None:
        """Ищет уже стоящую в очереди задачу на тот же ролик в том же режиме.

        Нужно, чтобы двойная отправка ссылки не порождала две одинаковые задачи.
        """
        platform, video_id, mode, max_height = cache_key_parts
        row = await self._db.fetch_one(
            f"""
            SELECT {_COLUMNS} FROM jobs
            WHERE user_id = ? AND platform = ? AND video_id = ? AND mode = ?
              AND max_height = ? AND status IN (?, ?)
            ORDER BY id LIMIT 1
            """,
            (
                user_id,
                platform,
                video_id,
                mode,
                max_height,
                JobStatus.PENDING.value,
                JobStatus.RUNNING.value,
            ),
        )
        return _row_to_job(row) if row is not None else None

    async def recover_interrupted(self) -> list[Job]:
        """Возвращает прерванные рестартом задачи и переводит их в pending."""
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM jobs WHERE status IN (?, ?) ORDER BY id",
            (JobStatus.PENDING.value, JobStatus.RUNNING.value),
        )
        jobs = [_row_to_job(row) for row in rows]
        if jobs:
            await self._db.execute(
                "UPDATE jobs SET status = ?, stage = ?, started_at = NULL "
                "WHERE status IN (?, ?)",
                (
                    JobStatus.PENDING.value,
                    Stage.QUEUED.value,
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
            log.info("jobs_recovered", count=len(jobs))
        for job in jobs:
            job.status = JobStatus.PENDING
            job.stage = Stage.QUEUED
            job.started_at = None
        return jobs

    async def fail_all_active(self, reason: str) -> int:
        """Помечает все незавершённые задачи как упавшие (используется редко)."""
        count = await self.count_active()
        if count:
            await self._db.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                (
                    JobStatus.FAILED.value,
                    reason,
                    now(),
                    JobStatus.PENDING.value,
                    JobStatus.RUNNING.value,
                ),
            )
        return count

    async def purge_old(self, ttl_days: int) -> int:
        """Удаляет историю завершённых задач старше ttl_days. Возвращает число строк."""
        threshold = now() - ttl_days * 86400
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "DELETE FROM jobs WHERE status NOT IN (?, ?) AND created_at < ?",
                (JobStatus.PENDING.value, JobStatus.RUNNING.value, threshold),
            )
            removed = cursor.rowcount or 0
        if removed:
            log.info("jobs_history_purged", count=removed)
        return removed
