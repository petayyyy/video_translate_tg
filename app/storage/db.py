"""Соединение с SQLite, схема и миграции.

Одно соединение на процесс: нагрузка — единицы запросов в минуту, а вот
конкурентная запись в SQLite из нескольких соединений на сетевом или
контейнерном томе — источник «database is locked». Запись сериализуется
явной блокировкой, чтение идёт без неё (WAL это позволяет).

Версия схемы хранится в ``PRAGMA user_version``; миграции — упорядоченный
список функций, применяемых по возрастанию версии.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

import aiosqlite

from app.logging_setup import get_logger

__all__ = ["Database", "now"]

log = get_logger(__name__)

SCHEMA_VERSION = 1


def now() -> float:
    """Текущее время в секундах Unix. Единая точка — удобно подменять в тестах."""
    return time.time()


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    progress_message_id INTEGER,
    request_message_id  INTEGER,
    url                 TEXT    NOT NULL,
    platform            TEXT    NOT NULL,
    video_id            TEXT    NOT NULL,
    mode                TEXT    NOT NULL,
    max_height          INTEGER NOT NULL,
    status              TEXT    NOT NULL,
    stage               TEXT    NOT NULL,
    title               TEXT,
    duration_sec        REAL,
    error               TEXT,
    result_path         TEXT,
    created_at          REAL    NOT NULL,
    started_at          REAL,
    finished_at         REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs (status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_user    ON jobs (user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at);

CREATE TABLE IF NOT EXISTS cache (
    key              TEXT PRIMARY KEY,
    platform         TEXT    NOT NULL,
    video_id         TEXT    NOT NULL,
    mode             TEXT    NOT NULL,
    max_height       INTEGER NOT NULL DEFAULT 0,
    title            TEXT,
    file_path        TEXT,
    file_size        INTEGER NOT NULL DEFAULT 0,
    telegram_file_id TEXT,
    duration_sec     REAL,
    created_at       REAL    NOT NULL,
    last_used_at     REAL    NOT NULL,
    hits             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cache_created ON cache (created_at);
CREATE INDEX IF NOT EXISTS idx_cache_used    ON cache (last_used_at);
CREATE INDEX IF NOT EXISTS idx_cache_video   ON cache (platform, video_id);

CREATE TABLE IF NOT EXISTS links (
    token      TEXT PRIMARY KEY,
    file_path  TEXT    NOT NULL,
    file_name  TEXT    NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    user_id    INTEGER,
    job_id     INTEGER,
    one_time   INTEGER NOT NULL DEFAULT 1,
    downloads  INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL,
    used_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_links_expires ON links (expires_at);
"""


def _migration_1(connection: aiosqlite.Connection) -> str:
    return _SCHEMA_V1


_MIGRATIONS: tuple[Callable[[aiosqlite.Connection], str], ...] = (_migration_1,)


class Database:
    """Асинхронная обёртка над aiosqlite с сериализованной записью."""

    __slots__ = ("_path", "_connection", "_write_lock", "_closed")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    # -- жизненный цикл --------------------------------------------------- #

    async def connect(self) -> None:
        """Открывает соединение, включает WAL и применяет миграции."""
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(self._path), timeout=30.0)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=30000")
        await connection.commit()
        self._connection = connection
        await self._migrate()
        log.info("database_ready", path=str(self._path), schema=SCHEMA_VERSION)

    async def close(self) -> None:
        if self._connection is None:
            return
        self._closed = True
        try:
            await self._connection.commit()
        except Exception:  # соединение могло уже развалиться
            log.debug("db_final_commit_failed", exc_info=True)
        await self._connection.close()
        self._connection = None
        log.info("database_closed")

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("База не открыта: сначала вызови Database.connect()")
        return self._connection

    async def _migrate(self) -> None:
        connection = self.connection
        async with connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        current = int(row[0]) if row else 0

        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"База {self._path} создана более новой версией бота "
                f"(схема {current} > {SCHEMA_VERSION}). Откати образ или удали базу."
            )

        for version in range(current + 1, SCHEMA_VERSION + 1):
            script = _MIGRATIONS[version - 1](connection)
            log.info("db_migrate", to_version=version)
            await connection.executescript(script)
            await connection.execute(f"PRAGMA user_version={version}")
            await connection.commit()

    # -- примитивы -------------------------------------------------------- #

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.connection.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.connection.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполняет запрос на запись. Возвращает lastrowid."""
        async with self._write_lock:
            cursor = await self.connection.execute(sql, params)
            await self.connection.commit()
            return int(cursor.lastrowid or 0)

    async def execute_many(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        async with self._write_lock:
            await self.connection.executemany(sql, list(params))
            await self.connection.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция для нескольких связанных изменений.

        При исключении внутри блока изменения откатываются.
        """
        async with self._write_lock:
            connection = self.connection
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def vacuum(self) -> None:
        """Сжимает файл базы. Вызывается редко, из фоновой уборки."""
        async with self._write_lock:
            await self.connection.execute("VACUUM")
            await self.connection.commit()
