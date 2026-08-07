"""Кэш готовых результатов по ID видео.

Хранится две вещи:

* **файл** в ``paths.cache_dir`` — чтобы можно было отдать результат в любой
  форме (в Telegram, ссылкой, повторно склеить);
* **telegram file_id** — если файл уже уходил в Telegram, повторная отправка
  делается по file_id вообще без диска и без трафика, мгновенно.

Поэтому запись кэша остаётся полезной даже после того, как файл удалён по
лимиту размера: file_id живёт на серверах Telegram практически вечно
(``cache.keep_file_ids``).

Ключ — ``platform:video_id:mode[:height]``. Для аудио и субтитров качество
и громкость роли не играют, поэтому в ключ не входят.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.config import CacheSettings
from app.jobs.models import Job, JobMode
from app.logging_setup import get_logger
from app.storage.db import Database, now
from app.utils.tempdirs import safe_rmtree

__all__ = ["CacheEntry", "ResultCache"]

log = get_logger(__name__)


@dataclass(slots=True)
class CacheEntry:
    key: str
    platform: str
    video_id: str
    mode: JobMode
    max_height: int
    title: str | None
    file_path: Path | None
    file_size: int
    telegram_file_id: str | None
    duration_sec: float | None
    created_at: float
    last_used_at: float
    hits: int

    @property
    def file_exists(self) -> bool:
        return self.file_path is not None and self.file_path.is_file()

    @property
    def usable(self) -> bool:
        """Из записи можно что-то отдать: либо файл на диске, либо file_id."""
        return self.file_exists or bool(self.telegram_file_id)

    def age_sec(self) -> float:
        return max(0.0, now() - self.created_at)


def _row_to_entry(row: aiosqlite.Row) -> CacheEntry:
    raw_path = row["file_path"]
    return CacheEntry(
        key=row["key"],
        platform=row["platform"],
        video_id=row["video_id"],
        mode=JobMode(row["mode"]),
        max_height=int(row["max_height"]),
        title=row["title"],
        file_path=Path(raw_path) if raw_path else None,
        file_size=int(row["file_size"] or 0),
        telegram_file_id=row["telegram_file_id"],
        duration_sec=row["duration_sec"],
        created_at=float(row["created_at"]),
        last_used_at=float(row["last_used_at"]),
        hits=int(row["hits"] or 0),
    )


class ResultCache:
    """Кэш результатов: поиск, запись, уборка по TTL и по объёму."""

    __slots__ = ("_db", "_settings", "_cache_dir")

    def __init__(self, database: Database, settings: CacheSettings, cache_dir: Path) -> None:
        self._db = database
        self._settings = settings
        self._cache_dir = Path(cache_dir)

    @property
    def keeps_file_ids(self) -> bool:
        """Сохраняются ли file_id при удалении файлов с диска."""
        return self._settings.keep_file_ids

    # -- чтение ----------------------------------------------------------- #

    async def get(self, job: Job) -> CacheEntry | None:
        """Возвращает пригодную запись кэша для задачи или None.

        Протухшие, битые и «пустые» записи (файл удалён, file_id нет)
        удаляются прямо здесь, чтобы не накапливать мусор.
        """
        if not self._settings.enabled:
            return None

        row = await self._db.fetch_one(
            "SELECT * FROM cache WHERE key = ?",
            (job.cache_key,),
        )
        if row is None:
            return None

        entry = _row_to_entry(row)

        if entry.age_sec() > self._settings.ttl_seconds:
            log.info("cache_expired", key=entry.key, age_h=round(entry.age_sec() / 3600, 1))
            await self.drop(entry.key, delete_file=True)
            return None

        if entry.file_path is not None and not entry.file_exists:
            # Файл удалили (лимит объёма, ручная уборка) — file_id может остаться.
            if entry.telegram_file_id and self._settings.keep_file_ids:
                entry.file_path = None
                await self._db.execute(
                    "UPDATE cache SET file_path = NULL, file_size = 0 WHERE key = ?",
                    (entry.key,),
                )
            else:
                await self.drop(entry.key, delete_file=False)
                return None

        if not entry.usable:
            await self.drop(entry.key, delete_file=False)
            return None

        await self._db.execute(
            "UPDATE cache SET last_used_at = ?, hits = hits + 1 WHERE key = ?",
            (now(), entry.key),
        )
        entry.hits += 1
        log.info("cache_hit", key=entry.key, hits=entry.hits, has_file=entry.file_exists)
        return entry

    # -- запись ----------------------------------------------------------- #

    async def store(
        self,
        job: Job,
        source_file: Path,
        *,
        telegram_file_id: str | None = None,
        move: bool = True,
    ) -> CacheEntry | None:
        """Кладёт готовый файл в кэш. Возвращает созданную запись.

        ``move=True`` переносит файл из рабочего каталога задачи (быстро, если
        том один), иначе копирует. Если кэш выключен — файл не трогается,
        возвращается None.
        """
        if not self._settings.enabled:
            return None
        if not source_file.is_file():
            log.warning("cache_store_missing_file", path=str(source_file))
            return None

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_dir / self._file_name(job, source_file.suffix)

        try:
            if move:
                await self._move(source_file, target)
            else:
                await self._copy(source_file, target)
        except OSError as exc:
            log.warning("cache_store_failed", path=str(source_file), error=str(exc))
            return None

        size = target.stat().st_size
        timestamp = now()

        await self._db.execute(
            """
            INSERT INTO cache (
                key, platform, video_id, mode, max_height, title, file_path,
                file_size, telegram_file_id, duration_sec, created_at, last_used_at, hits
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(key) DO UPDATE SET
                title            = excluded.title,
                file_path        = excluded.file_path,
                file_size        = excluded.file_size,
                telegram_file_id = COALESCE(excluded.telegram_file_id, cache.telegram_file_id),
                duration_sec     = excluded.duration_sec,
                created_at       = excluded.created_at,
                last_used_at     = excluded.last_used_at
            """,
            (
                job.cache_key,
                job.platform,
                job.video_id,
                job.mode.value,
                job.max_height if job.mode.needs_video else 0,
                job.title,
                str(target),
                size,
                telegram_file_id,
                job.duration_sec,
                timestamp,
                timestamp,
            ),
        )

        log.info("cache_stored", key=job.cache_key, size_mb=round(size / 1024**2, 1))
        await self.enforce_size_limit()

        return CacheEntry(
            key=job.cache_key,
            platform=job.platform,
            video_id=job.video_id,
            mode=job.mode,
            max_height=job.max_height,
            title=job.title,
            file_path=target,
            file_size=size,
            telegram_file_id=telegram_file_id,
            duration_sec=job.duration_sec,
            created_at=timestamp,
            last_used_at=timestamp,
            hits=0,
        )

    async def store_reference(
        self,
        job: Job,
        *,
        telegram_file_id: str,
        size_bytes: int = 0,
    ) -> CacheEntry | None:
        """Пишет в кэш только ``file_id``, не копируя файл на диск.

        Это и есть освобождение места «сразу после отправки»: результат уже
        лежит на серверах Telegram, повторная выдача делается по ``file_id``
        мгновенно и вообще без диска, а локальный файл остаётся во временном
        каталоге задачи и удаляется вместе с ним через несколько секунд.

        Используется, когда ``cache.keep_files_after_send: false``.
        """
        if not self._settings.enabled or not telegram_file_id:
            return None

        timestamp = now()
        await self._db.execute(
            """
            INSERT INTO cache (
                key, platform, video_id, mode, max_height, title, file_path,
                file_size, telegram_file_id, duration_sec, created_at, last_used_at, hits
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, 0)
            ON CONFLICT(key) DO UPDATE SET
                title            = excluded.title,
                telegram_file_id = excluded.telegram_file_id,
                duration_sec     = excluded.duration_sec,
                created_at       = excluded.created_at,
                last_used_at     = excluded.last_used_at
            """,
            (
                job.cache_key,
                job.platform,
                job.video_id,
                job.mode.value,
                job.max_height if job.mode.needs_video else 0,
                job.title,
                telegram_file_id,
                job.duration_sec,
                timestamp,
                timestamp,
            ),
        )

        log.info(
            "cache_stored_reference",
            key=job.cache_key,
            freed_mb=round(size_bytes / 1024**2, 1),
        )
        return CacheEntry(
            key=job.cache_key,
            platform=job.platform,
            video_id=job.video_id,
            mode=job.mode,
            max_height=job.max_height,
            title=job.title,
            file_path=None,
            file_size=0,
            telegram_file_id=telegram_file_id,
            duration_sec=job.duration_sec,
            created_at=timestamp,
            last_used_at=timestamp,
            hits=0,
        )

    async def release_file(self, key: str) -> int:
        """Удаляет файл записи с диска, сохраняя ``file_id``.

        Возвращает освобождённый объём в байтах. Если ``file_id`` не записан,
        файл не трогается — иначе результат был бы потерян безвозвратно.
        """
        row = await self._db.fetch_one(
            "SELECT file_path, file_size, telegram_file_id FROM cache WHERE key = ?",
            (key,),
        )
        if row is None or not row["file_path"]:
            return 0
        if not row["telegram_file_id"]:
            log.debug("cache_release_skipped_no_file_id", key=key)
            return 0

        freed = int(row["file_size"] or 0)
        safe_rmtree(Path(row["file_path"]))
        await self._db.execute(
            "UPDATE cache SET file_path = NULL, file_size = 0 WHERE key = ?", (key,)
        )
        log.info("cache_file_released", key=key, freed_mb=round(freed / 1024**2, 1))
        return freed

    async def purge_all_files(self) -> tuple[int, int]:
        """Удаляет все файлы кэша, сохраняя записи с ``file_id``.

        Аварийная мера для команды /cleanup. Возвращает ``(файлов, байт)``.
        """
        rows = await self._db.fetch_all(
            "SELECT key, file_path, file_size, telegram_file_id FROM cache "
            "WHERE file_path IS NOT NULL"
        )
        count = 0
        freed = 0
        for row in rows:
            safe_rmtree(Path(row["file_path"]))
            count += 1
            freed += int(row["file_size"] or 0)
            if self._settings.keep_file_ids and row["telegram_file_id"]:
                await self._db.execute(
                    "UPDATE cache SET file_path = NULL, file_size = 0 WHERE key = ?",
                    (row["key"],),
                )
            else:
                await self._db.execute("DELETE FROM cache WHERE key = ?", (row["key"],))
        if count:
            log.info("cache_purged", files=count, freed_mb=round(freed / 1024**2, 1))
        return count, freed

    async def remember_file_id(self, key: str, telegram_file_id: str) -> None:
        """Запоминает file_id после успешной отправки в Telegram."""
        if not self._settings.enabled or not telegram_file_id:
            return
        await self._db.execute(
            "UPDATE cache SET telegram_file_id = ? WHERE key = ?",
            (telegram_file_id, key),
        )

    async def drop(self, key: str, *, delete_file: bool) -> None:
        row = await self._db.fetch_one("SELECT file_path FROM cache WHERE key = ?", (key,))
        if row is not None and delete_file and row["file_path"]:
            safe_rmtree(Path(row["file_path"]))
        await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))

    # -- уборка ------------------------------------------------------------ #

    async def cleanup_expired(self) -> int:
        """Удаляет протухшие записи. Возвращает количество удалённых файлов."""
        if not self._settings.enabled:
            return 0
        threshold = now() - self._settings.ttl_seconds
        rows = await self._db.fetch_all(
            "SELECT key, file_path, telegram_file_id FROM cache WHERE created_at < ?",
            (threshold,),
        )
        removed = 0
        for row in rows:
            if row["file_path"]:
                safe_rmtree(Path(row["file_path"]))
                removed += 1
            if self._settings.keep_file_ids and row["telegram_file_id"]:
                # Файл удаляем, но file_id оставляем — отдавать по нему можно вечно.
                await self._db.execute(
                    "UPDATE cache SET file_path = NULL, file_size = 0, created_at = ? "
                    "WHERE key = ?",
                    (now(), row["key"]),
                )
            else:
                await self._db.execute("DELETE FROM cache WHERE key = ?", (row["key"],))
        if removed:
            log.info("cache_expired_cleanup", files=removed)
        return removed

    async def enforce_size_limit(self) -> int:
        """Удаляет самые давно не используемые файлы, пока кэш не влезет в лимит."""
        limit = self._settings.max_size_bytes
        if not self._settings.enabled or limit <= 0:
            return 0

        total = int(
            await self._db.fetch_value(
                "SELECT COALESCE(SUM(file_size), 0) FROM cache WHERE file_path IS NOT NULL",
                default=0,
            )
        )
        if total <= limit:
            return 0

        rows = await self._db.fetch_all(
            "SELECT key, file_path, file_size, telegram_file_id FROM cache "
            "WHERE file_path IS NOT NULL ORDER BY last_used_at ASC"
        )
        removed = 0
        for row in rows:
            if total <= limit:
                break
            safe_rmtree(Path(row["file_path"]))
            total -= int(row["file_size"] or 0)
            removed += 1
            if self._settings.keep_file_ids and row["telegram_file_id"]:
                await self._db.execute(
                    "UPDATE cache SET file_path = NULL, file_size = 0 WHERE key = ?",
                    (row["key"],),
                )
            else:
                await self._db.execute("DELETE FROM cache WHERE key = ?", (row["key"],))

        if removed:
            log.info(
                "cache_size_enforced",
                removed=removed,
                total_gb=round(total / 1024**3, 2),
                limit_gb=round(limit / 1024**3, 2),
            )
        return removed

    async def stats(self) -> dict[str, float | int]:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS bytes, "
            "COALESCE(SUM(hits), 0) AS hits FROM cache"
        )
        files = int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM cache WHERE file_path IS NOT NULL", default=0
            )
        )
        return {
            "entries": int(row["n"]) if row else 0,
            "files": files,
            "bytes": int(row["bytes"]) if row else 0,
            "hits": int(row["hits"]) if row else 0,
        }

    # -- вспомогательное --------------------------------------------------- #

    @staticmethod
    def _file_name(job: Job, suffix: str) -> str:
        safe_platform = job.platform.replace("/", "_")
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in job.video_id
        )[:64]
        height = job.max_height if job.mode.needs_video else 0
        return f"{safe_platform}_{safe_id}_{job.mode.value}_{height}{suffix or '.bin'}"

    @staticmethod
    async def _move(source: Path, target: Path) -> None:
        def _do() -> None:
            if target.exists():
                target.unlink()
            try:
                source.replace(target)  # быстрый путь: один том
            except OSError:
                shutil.move(str(source), str(target))

        await asyncio.to_thread(_do)

    @staticmethod
    async def _copy(source: Path, target: Path) -> None:
        await asyncio.to_thread(shutil.copy2, str(source), str(target))
