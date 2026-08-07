"""Отдача результата пользователю: файлом в Telegram или ссылкой через nginx.

Порядок выбора:

1. Файл влезает в ``telegram.max_upload_bytes`` → отправляем в Telegram.
   При включённом ``use_file_uri`` файл не загружается по HTTP, а передаётся
   локальному Bot API строкой ``file:///путь``: сервер читает его с диска сам.
   Для двухгигабайтного файла это разница между секундами и минутами.
   Если сборка ``telegram-bot-api`` такое не принимает, происходит
   автоматический откат на обычную multipart-загрузку, и режим ``file://``
   больше не используется до перезапуска — чтобы не спотыкаться каждый раз.
2. Файл больше лимита → кладём под одноразовым токеном и присылаем ссылку.
3. Раздача ссылками выключена → честно объясняем, что файл не помещается.

После успешной отправки ``file_id`` запоминается в кэше: повторная выдача
того же ролика делается по нему мгновенно и без диска.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError
from aiogram.types import FSInputFile, Message

from app.config import Settings
from app.jobs.models import Job, JobMode
from app.logging_setup import get_logger
from app.pipeline.runner import PipelineResult
from app.storage.cache import CacheEntry
from app.storage.links import LinkStore, sanitize_filename
from app.utils.retry import retry_async

__all__ = ["Delivery", "DeliveryOutcome"]

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]] | None


@dataclass(slots=True)
class DeliveryOutcome:
    """Чем закончилась отдача результата."""

    sent_to_telegram: bool
    telegram_file_id: str | None
    link_url: str | None
    note: str | None = None


def _escape(text: str) -> str:
    """Экранирование для ТЕКСТА внутри тега."""
    return html.escape(text, quote=False)


def _escape_attr(text: str) -> str:
    """Экранирование для значения АТРИБУТА (href): кавычки тоже."""
    return html.escape(text, quote=True)


class Delivery:
    """Отправка готового файла пользователю."""

    __slots__ = ("_bot", "_settings", "_links", "_file_uri_enabled")

    def __init__(self, bot: Bot, settings: Settings, links: LinkStore) -> None:
        self._bot = bot
        self._settings = settings
        self._links = links
        self._file_uri_enabled = settings.telegram.use_file_uri

    # -- отправка нового результата ------------------------------------------ #

    async def send_result(
        self,
        job: Job,
        result: PipelineResult,
        *,
        progress: ProgressCallback = None,
    ) -> DeliveryOutcome:
        """Отдаёт готовый файл: в Telegram либо ссылкой."""
        size = result.size_bytes or result.path.stat().st_size
        limit = self._settings.telegram.max_upload_bytes

        if size > limit:
            return await self._send_link(job, result, size)

        if progress is not None:
            await progress(f"загружаю {size / 1024**2:.0f} МБ в Telegram")

        try:
            message = await self._send_file(job, result, size)
        except TelegramEntityTooLarge:
            log.warning("telegram_too_large", job_id=job.id, size=size)
            return await self._send_link(
                job,
                result,
                size,
                note=(
                    "Telegram отказался принимать файл такого размера, "
                    "поэтому вот прямая ссылка."
                ),
            )

        file_id = _extract_file_id(message)
        log.info(
            "delivered_telegram",
            job_id=job.id,
            size_mb=round(size / 1024**2, 1),
            kind=result.kind,
            has_file_id=bool(file_id),
        )
        return DeliveryOutcome(sent_to_telegram=True, telegram_file_id=file_id, link_url=None)

    # -- отправка из кэша ------------------------------------------------------ #

    async def send_cached(self, job: Job, entry: CacheEntry) -> DeliveryOutcome:
        """Отдаёт результат из кэша: по file_id, если он есть, иначе файлом."""
        caption = self._caption(job, entry.title or job.display_title, cached=True)

        if entry.telegram_file_id:
            try:
                message = await self._send_by_file_id(job, entry, caption)
            except (TelegramBadRequest, TelegramNetworkError) as exc:
                log.warning("cached_file_id_failed", job_id=job.id, error=str(exc))
            else:
                log.info("delivered_cached_file_id", job_id=job.id, key=entry.key)
                return DeliveryOutcome(
                    sent_to_telegram=True,
                    telegram_file_id=_extract_file_id(message) or entry.telegram_file_id,
                    link_url=None,
                )

        if not entry.file_exists or entry.file_path is None:
            raise FileNotFoundError("В кэше не осталось ни файла, ни рабочего file_id")

        pseudo = PipelineResult(
            path=entry.file_path,
            kind=_kind_for_mode(entry.mode),
            suggested_name=entry.file_path.name,
            title=entry.title or job.display_title,
            duration_sec=entry.duration_sec,
            size_bytes=entry.file_size or entry.file_path.stat().st_size,
        )
        return await self.send_result(job, pseudo)

    # -- внутреннее ------------------------------------------------------------ #

    def _caption(self, job: Job, title: str, *, cached: bool = False) -> str:
        parts = [f"<b>{_escape(title)}</b>"]
        details = [job.mode.title]
        if job.mode.needs_video:
            details.append(f"до {job.max_height}p")
        if cached:
            details.append("из кэша")
        parts.append("<i>" + _escape(" · ".join(details)) + "</i>")
        caption = "\n".join(parts)
        return caption[:1020]

    def _file_argument(self, path: Path, filename: str) -> FSInputFile | str:
        """Возвращает то, что передаётся в send_*: путь для локального сервера или файл."""
        if self._file_uri_enabled:
            return f"file://{path}"
        return FSInputFile(path, filename=filename)

    async def _send_file(self, job: Job, result: PipelineResult, size: int) -> Message:
        """Отправляет файл, разбираясь с двумя типовыми отказами Telegram.

        Первый — сервер не принял путь ``file://``: откатываемся на обычную
        загрузку. Второй — исходное сообщение удалили, пока шла обработка,
        и ответить на него уже некуда: повторяем без привязки к ответу.

        Различать их обязательно: раньше любой ``TelegramBadRequest`` списывался
        на ``file://``, и удалённое сообщение навсегда выключало быстрый режим
        отправки, хотя он был совершенно ни при чём.
        """
        try:
            return await self._send_once(job, result, size)
        except TelegramBadRequest as exc:
            message = str(exc).lower()

            if _is_reply_target_gone(message) and job.request_message_id is not None:
                log.info("reply_target_gone", job_id=job.id)
                job.request_message_id = None
                return await self._send_once(job, result, size)

            if self._file_uri_enabled and _is_file_reference_problem(message):
                log.warning(
                    "file_uri_rejected",
                    job_id=job.id,
                    error=str(exc),
                    hint="перехожу на обычную загрузку файла; "
                    "чтобы не пробовать снова, поставь telegram.use_file_uri: false",
                )
                self._file_uri_enabled = False
                return await self._send_once(job, result, size)

            raise

    async def _send_once(self, job: Job, result: PipelineResult, size: int) -> Message:
        bot = self._bot
        chat_id = job.chat_id
        caption = self._caption(job, result.title)
        filename = sanitize_filename(result.suggested_name, fallback="video")
        payload = self._file_argument(result.path, filename)
        duration = int(result.duration_sec) if result.duration_sec else None
        timeout = self._settings.telegram.upload_timeout_sec

        async def _do() -> Message:
            if result.kind == "video" and self._settings.telegram.send_as_video:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                return await bot.send_video(
                    chat_id=chat_id,
                    video=payload,
                    caption=caption,
                    duration=duration,
                    width=result.width,
                    height=result.height,
                    supports_streaming=True,
                    reply_to_message_id=job.request_message_id,
                    request_timeout=int(timeout),
                )
            if result.kind == "audio":
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
                return await bot.send_audio(
                    chat_id=chat_id,
                    audio=payload,
                    caption=caption,
                    duration=duration,
                    title=result.title[:64],
                    performer="Яндекс Переводчик",
                    reply_to_message_id=job.request_message_id,
                    request_timeout=int(timeout),
                )
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            return await bot.send_document(
                chat_id=chat_id,
                document=payload,
                caption=caption,
                reply_to_message_id=job.request_message_id,
                request_timeout=int(timeout),
            )

        return await retry_async(
            _do,
            attempts=self._settings.telegram.send_retry_attempts,
            exceptions=(TelegramNetworkError, asyncio.TimeoutError, OSError),
            # TelegramEntityTooLarge — наследник TelegramNetworkError. Без этого
            # исключения ретрай принял бы «файл слишком большой» за сетевой сбой
            # и трижды перезалил бы двухгигабайтный файл впустую, прежде чем
            # управление вернулось наверх и файл ушёл бы ссылкой.
            stop_on=(TelegramEntityTooLarge,),
            start=self._settings.telegram.send_retry_delay_sec,
            factor=2.0,
            maximum=60.0,
            description=f"отправка файла ({size / 1024**2:.0f} МБ)",
        )

    async def _send_by_file_id(self, job: Job, entry: CacheEntry, caption: str) -> Message:
        bot = self._bot
        file_id = entry.telegram_file_id
        assert file_id is not None
        kind = _kind_for_mode(entry.mode)

        if kind == "video" and self._settings.telegram.send_as_video:
            return await bot.send_video(
                chat_id=job.chat_id,
                video=file_id,
                caption=caption,
                supports_streaming=True,
                reply_to_message_id=job.request_message_id,
            )
        if kind == "audio":
            return await bot.send_audio(
                chat_id=job.chat_id,
                audio=file_id,
                caption=caption,
                reply_to_message_id=job.request_message_id,
            )
        return await bot.send_document(
            chat_id=job.chat_id,
            document=file_id,
            caption=caption,
            reply_to_message_id=job.request_message_id,
        )

    async def _send_link(
        self,
        job: Job,
        result: PipelineResult,
        size: int,
        *,
        note: str | None = None,
    ) -> DeliveryOutcome:
        """Публикует файл под одноразовым токеном и присылает ссылку."""
        if not self._links.enabled:
            message = (
                f"Файл получился {size / 1024**3:.2f} ГБ — это больше лимита "
                f"{self._settings.telegram.max_upload_bytes / 1024**3:.2f} ГБ, "
                "а раздача ссылками выключена (links.enabled: false).\n"
                "Включи её в конфиге или понизь качество командой /q720."
            )
            await self._bot.send_message(
                job.chat_id, message, reply_to_message_id=job.request_message_id
            )
            return DeliveryOutcome(
                sent_to_telegram=False, telegram_file_id=None, link_url=None, note=message
            )

        link = await self._links.publish(
            result.path,
            display_name=result.suggested_name,
            user_id=job.user_id,
            job_id=job.id,
            move=False,
        )

        lines = [f"<b>{_escape(result.title)}</b>"]
        if note:
            lines.append(_escape(note))
        else:
            lines.append(
                _escape(
                    f"Файл {size / 1024**3:.2f} ГБ — это больше лимита Telegram, "
                    "поэтому вот прямая ссылка."
                )
            )
        lines.append("")
        lines.append(f'<a href="{_escape_attr(link.url)}">⬇️ Скачать</a>')
        lines.append("")
        lines.append(
            _escape(
                ("Ссылка одноразовая, " if link.one_time else "Ссылка действует ")
                + f"файл удалится через {link.ttl_hours:.0f} ч."
            )
        )

        await self._bot.send_message(
            job.chat_id,
            "\n".join(lines),
            reply_to_message_id=job.request_message_id,
            disable_web_page_preview=True,
        )

        log.info("delivered_link", job_id=job.id, size_mb=round(size / 1024**2, 1))
        return DeliveryOutcome(
            sent_to_telegram=False,
            telegram_file_id=None,
            link_url=link.url,
            note="отдано ссылкой",
        )


#: Telegram отвечает по-разному в зависимости от версии — держим оба варианта.
_REPLY_GONE_MARKERS = (
    "message to be replied not found",
    "replied message not found",
    "message to reply not found",
)

#: Отказы, по которым видно, что сервер не понял ссылку на файл.
_FILE_REFERENCE_MARKERS = (
    "wrong file identifier",
    "wrong remote file",
    "failed to get file",
    "file must be non-empty",
    "invalid file",
    "can't open file",
    "file not found",
    "wrong url",
    "unsupported url protocol",
    "local copy",
)


def _is_reply_target_gone(message: str) -> bool:
    return any(marker in message for marker in _REPLY_GONE_MARKERS)


def _is_file_reference_problem(message: str) -> bool:
    return any(marker in message for marker in _FILE_REFERENCE_MARKERS)


def _extract_file_id(message: Message | None) -> str | None:
    if message is None:
        return None
    if message.video is not None:
        return message.video.file_id
    if message.audio is not None:
        return message.audio.file_id
    if message.document is not None:
        return message.document.file_id
    if message.voice is not None:
        return message.voice.file_id
    return None


def _kind_for_mode(mode: JobMode) -> str:
    if mode is JobMode.AUDIO:
        return "audio"
    if mode is JobMode.SUBS:
        return "subtitles"
    return "video"
