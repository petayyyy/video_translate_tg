"""Одноразовые ссылки для файлов, которые не влезли в лимит Telegram.

Как это устроено:

1. Бот кладёт файл в ``paths.files_dir/<token>/<имя файла>``.
2. В базу пишется токен со сроком жизни.
3. Пользователю уходит ссылка ``<public_base_url>/dl/<token>/<имя файла>``.
4. nginx перед отдачей делает ``auth_request`` к боту (``/_auth``). Бот
   проверяет токен и, если ссылка одноразовая, сразу помечает её
   использованной. Сам nginx одноразовость реализовать не умеет — отсюда
   и нужен этот маленький HTTP-эндпоинт.
5. Фоновая уборка удаляет и запись, и каталог с файлом по истечении TTL.

Токен — 32 символа из ``secrets.token_urlsafe``, то есть 192 бита энтропии:
перебрать его по прямой ссылке нереально.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import LinksSettings
from app.logging_setup import get_logger
from app.storage.db import Database, now
from app.utils.tempdirs import safe_rmtree

__all__ = ["DownloadLink", "LinkStore", "sanitize_filename"]

log = get_logger(__name__)

_UNSAFE_CHARS = re.compile(r"[^\w.\-() ]+", re.UNICODE)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")


def sanitize_filename(name: str, *, fallback: str = "video", max_length: int = 100) -> str:
    """Приводит название ролика к безопасному имени файла.

    Убираются разделители путей, управляющие символы и всё, что ломает
    HTTP-заголовки; длина ограничивается, расширение сохраняется.
    """
    name = (name or "").strip().replace("/", "-").replace("\\", "-")
    suffix = ""
    if "." in name:
        head, _, tail = name.rpartition(".")
        if head and 1 <= len(tail) <= 5 and tail.isalnum():
            name, suffix = head, "." + tail.lower()
    cleaned = _UNSAFE_CHARS.sub("_", name)
    # Схлопываем подряд идущие подчёркивания вместе с пробелами между ними:
    # 'Лекция 3-4_ _Физика_ _' → 'Лекция 3-4_Физика'. Без этого вычищенные
    # кавычки и двоеточия оставляют за собой рваные '_ _'.
    cleaned = re.sub(r"[\s_]*_[\s_]*", "_", cleaned).strip(" ._-")
    # Имя из одних разделителей ('///...' → '---') технически допустимо, но
    # бесполезно: если не осталось ни одной буквы или цифры, берём запасное.
    if not any(character.isalnum() for character in cleaned):
        cleaned = fallback
    limit = max(1, max_length - len(suffix))
    return cleaned[:limit] + suffix


@dataclass(slots=True)
class DownloadLink:
    token: str
    url: str
    file_path: Path
    file_name: str
    size_bytes: int
    expires_at: float
    one_time: bool

    @property
    def ttl_hours(self) -> float:
        return max(0.0, (self.expires_at - now()) / 3600.0)


class LinkStore:
    """Создание, проверка и уборка одноразовых ссылок."""

    __slots__ = ("_db", "_settings", "_files_dir")

    def __init__(self, database: Database, settings: LinksSettings, files_dir: Path) -> None:
        self._db = database
        self._settings = settings
        self._files_dir = Path(files_dir)

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # -- создание ---------------------------------------------------------- #

    async def publish(
        self,
        source_file: Path,
        *,
        display_name: str,
        user_id: int | None = None,
        job_id: int | None = None,
        move: bool = False,
    ) -> DownloadLink:
        """Выкладывает файл под одноразовым токеном и возвращает ссылку.

        ``move=False`` копирует файл (исходник обычно нужен ещё и для кэша);
        ``move=True`` переносит, если исходник больше не понадобится.
        """
        if not self._settings.enabled:
            raise RuntimeError("Раздача ссылками выключена в конфиге (links.enabled: false)")
        if not source_file.is_file():
            raise FileNotFoundError(f"Нет файла для публикации: {source_file}")

        token = secrets.token_urlsafe(24)
        file_name = sanitize_filename(display_name or source_file.name) or source_file.name
        if not Path(file_name).suffix:
            file_name += source_file.suffix

        target_dir = self._files_dir / token
        target = target_dir / file_name

        def _place() -> int:
            target_dir.mkdir(parents=True, exist_ok=True)
            if move:
                try:
                    source_file.replace(target)
                except OSError:
                    shutil.move(str(source_file), str(target))
            else:
                try:
                    # Жёсткая ссылка не занимает места, если том один и тот же.
                    target.hardlink_to(source_file)
                except (OSError, AttributeError):
                    shutil.copy2(str(source_file), str(target))
            return target.stat().st_size

        size = await asyncio.to_thread(_place)
        expires_at = now() + self._settings.ttl_seconds

        await self._db.execute(
            """
            INSERT INTO links (
                token, file_path, file_name, size_bytes, user_id, job_id,
                one_time, downloads, created_at, expires_at, used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
            """,
            (
                token,
                str(target),
                file_name,
                size,
                user_id,
                job_id,
                1 if self._settings.one_time else 0,
                now(),
                expires_at,
            ),
        )

        url = f"{self._settings.public_base_url}/dl/{token}/{quote(file_name)}"
        log.info(
            "link_published",
            token=token[:8] + "…",
            size_mb=round(size / 1024**2, 1),
            one_time=self._settings.one_time,
            ttl_h=round(self._settings.ttl_hours, 1),
        )
        return DownloadLink(
            token=token,
            url=url,
            file_path=target,
            file_name=file_name,
            size_bytes=size,
            expires_at=expires_at,
            one_time=self._settings.one_time,
        )

    # -- проверка (вызывается из /_auth) ------------------------------------ #

    async def authorize(self, token: str) -> tuple[bool, str]:
        """Проверяет токен для nginx auth_request.

        Возвращает ``(разрешено, причина)``.

        Одноразовость реализована как «один сеанс скачивания», а не «один
        HTTP-запрос»: браузеры и менеджеры загрузок тянут большой файл
        несколькими Range-запросами, и каждый из них приходит сюда отдельно.
        Поэтому после первого обращения ссылка продолжает работать ещё
        ``links.one_time_grace_min`` минут, а затем закрывается навсегда.
        """
        if not token or not _TOKEN_RE.match(token):
            return False, "bad_token_format"

        row = await self._db.fetch_one(
            "SELECT token, file_path, one_time, downloads, expires_at, used_at "
            "FROM links WHERE token = ?",
            (token,),
        )
        if row is None:
            return False, "unknown_token"

        if now() > float(row["expires_at"]):
            await self.revoke(token, delete_file=True)
            return False, "expired"

        if int(row["one_time"]) == 1 and row["used_at"] is not None:
            grace = self._settings.one_time_grace_min * 60.0
            if now() - float(row["used_at"]) > grace:
                return False, "already_used"

        if not Path(row["file_path"]).is_file():
            await self.revoke(token, delete_file=False)
            return False, "file_missing"

        await self._db.execute(
            "UPDATE links SET downloads = downloads + 1, used_at = COALESCE(used_at, ?) "
            "WHERE token = ?",
            (now(), token),
        )
        return True, "ok"

    async def revoke(self, token: str, *, delete_file: bool = True) -> None:
        row = await self._db.fetch_one("SELECT file_path FROM links WHERE token = ?", (token,))
        if row is not None and delete_file:
            safe_rmtree(Path(row["file_path"]).parent)
        await self._db.execute("DELETE FROM links WHERE token = ?", (token,))

    # -- уборка ------------------------------------------------------------ #

    async def cleanup_expired(self) -> int:
        """Удаляет протухшие и уже скачанные ссылки вместе с файлами.

        Две категории:

        * истёк ``ttl_hours`` — файл никто не забрал, держать больше незачем;
        * ``delete_after_download`` и сеанс скачивания завершён (прошло окно
          ``one_time_grace_min`` после первого обращения) — файл забрали,
          место можно вернуть немедленно, не дожидаясь TTL. На небольшом
          диске это разница между часом и половиной суток.
        """
        moment = now()
        rows = await self._db.fetch_all(
            "SELECT token, file_path FROM links WHERE expires_at < ?", (moment,)
        )

        downloaded: list[Any] = []
        if self._settings.delete_after_download and self._settings.one_time:
            grace = self._settings.one_time_grace_min * 60.0
            downloaded = await self._db.fetch_all(
                "SELECT token, file_path FROM links "
                "WHERE one_time = 1 AND used_at IS NOT NULL AND used_at < ?",
                (moment - grace,),
            )
            if downloaded:
                log.info("links_freed_after_download", count=len(downloaded))

        for row in [*rows, *downloaded]:
            safe_rmtree(Path(row["file_path"]).parent)
            await self._db.execute("DELETE FROM links WHERE token = ?", (row["token"],))

        known = {
            str(known_row["token"])
            for known_row in await self._db.fetch_all("SELECT token FROM links")
        }
        removed_orphans = await asyncio.to_thread(self._sweep_orphan_dirs, known)

        total = len(rows) + len(downloaded)
        if total or removed_orphans:
            log.info(
                "links_cleanup",
                expired=len(rows),
                downloaded=len(downloaded),
                orphans=removed_orphans,
            )
        return total

    def _sweep_orphan_dirs(self, known_tokens: set[str]) -> int:
        """Удаляет каталоги в files_dir, которым не соответствует токен в базе.

        Такое остаётся, если процесс убили между созданием каталога и записью
        в базу. Синхронная функция: вызывается через ``to_thread``, поэтому
        список живых токенов передаётся снаружи, а не читается здесь.
        """
        if not self._files_dir.is_dir():
            return 0
        removed = 0
        threshold = now() - 3600  # не трогаем свежие: их могли только что создать
        for entry in self._files_dir.iterdir():
            if not entry.is_dir() or not _TOKEN_RE.match(entry.name):
                continue
            if entry.name in known_tokens:
                continue
            try:
                if entry.stat().st_mtime > threshold:
                    continue
            except OSError:
                continue
            removed += 1
            safe_rmtree(entry)
        return removed

    async def stats(self) -> dict[str, int]:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes FROM links "
            "WHERE expires_at > ?",
            (now(),),
        )
        return {
            "active": int(row["n"]) if row else 0,
            "bytes": int(row["bytes"]) if row else 0,
        }
