"""Одно сообщение на задачу, которое редактируется по мере прогресса.

Вместо потока сообщений «начал», «скачал», «склеиваю» пользователь видит одну
строку, которая меняется. Технически это ``editMessageText``, и с ним есть
три нюанса, ради которых и существует этот модуль:

* **Лимит частоты.** Telegram довольно быстро отвечает 429 на частые правки
  одного сообщения. Правки прореживаются интервалом из конфига, а финальные
  состояния (успех, ошибка, отмена) отправляются в обход прореживания.
* **«Message is not modified».** Если текст не изменился, Telegram отвечает
  ошибкой. Последний отправленный текст запоминается, повтор не отправляется.
* **Гонка с отменой.** Пока летит правка, задачу могут отменить. Финальные
  состояния всегда выигрывают: после ``success``/``failure``/``cancelled``
  сообщение больше не трогается.
"""

from __future__ import annotations

import asyncio
import html
import time
from typing import Sequence

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.jobs.models import Job, Stage
from app.logging_setup import get_logger

__all__ = ["ProgressView", "cancel_keyboard"]

log = get_logger(__name__)


def cancel_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Кнопка отмены под сообщением о прогрессе."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отменить", callback_data=f"cancel:{job_id}")]
        ]
    )


def _escape(text: str) -> str:
    """Экранирование для ТЕКСТА внутри тега."""
    return html.escape(text, quote=False)


def _escape_attr(text: str) -> str:
    """Экранирование для значения АТРИБУТА (href).

    Отдельная функция нужна из-за кавычек: ``quote=False`` их не трогает,
    и ссылка с символом ``"`` разорвала бы тег — Telegram отверг бы всё
    сообщение с ошибкой разбора HTML.
    """
    return html.escape(text, quote=True)


def _format_elapsed(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class ProgressView:
    """Управляет единственным сообщением задачи."""

    __slots__ = (
        "_bot",
        "_job",
        "_interval",
        "_last_edit",
        "_last_text",
        "_finished",
        "_lock",
        "_started",
        "_stage",
        "_detail",
        "_queue_position",
    )

    def __init__(self, bot: Bot, job: Job, *, interval: float) -> None:
        self._bot = bot
        self._job = job
        self._interval = max(1.0, interval)
        self._last_edit = 0.0
        self._last_text = ""
        self._finished = False
        self._lock = asyncio.Lock()
        self._started = time.monotonic()
        self._stage: Stage = job.stage
        self._detail = ""
        self._queue_position: int | None = None

    # -- построение текста --------------------------------------------------- #

    def _header(self) -> str:
        job = self._job
        title = _escape(job.display_title)
        if job.title:
            return f"🎬 <b>{title}</b>"
        return f'🎬 <a href="{_escape_attr(job.url)}">{title}</a>'

    def _mode_line(self) -> str:
        job = self._job
        parts = [job.mode.title]
        if job.mode.needs_video:
            parts.append(f"до {job.max_height}p")
        if job.duration_sec:
            parts.append(_format_elapsed(job.duration_sec))
        return "<i>" + _escape(" · ".join(parts)) + "</i>"

    def _status_line(self) -> str:
        stage = self._stage
        text = f"{stage.icon} {stage.title}"
        if stage is Stage.QUEUED and self._queue_position:
            text += f" — {self._queue_position}-я"
        if self._detail:
            text += f" — {_escape(self._detail)}"
        return text

    def _footer(self) -> str:
        elapsed = _format_elapsed(time.monotonic() - self._started)
        return f"<code>#{self._job.id}</code> · ⏱ {elapsed}"

    def _render(self) -> str:
        return "\n".join(
            (self._header(), self._mode_line(), "", self._status_line(), "", self._footer())
        )

    # -- отправка ------------------------------------------------------------ #

    async def _edit(
        self,
        text: str,
        *,
        keyboard: InlineKeyboardMarkup | None,
        force: bool,
    ) -> None:
        job = self._job
        if job.progress_message_id is None:
            return
        if text == self._last_text and not force:
            return

        moment = time.monotonic()
        if not force and moment - self._last_edit < self._interval:
            return

        try:
            await self._bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.progress_message_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramRetryAfter as exc:
            # Слишком часто правим — просто пропускаем эту правку и отодвигаем
            # следующую. Терять задачу из-за оформления смысла нет.
            log.debug("progress_flood", retry_after=exc.retry_after, job_id=job.id)
            self._last_edit = moment + float(exc.retry_after)
            return
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if "message is not modified" in message:
                self._last_text = text
                return
            if "message to edit not found" in message or "message can't be edited" in message:
                log.warning("progress_message_lost", job_id=job.id)
                job.progress_message_id = None
                return
            log.warning("progress_edit_failed", job_id=job.id, error=str(exc))
            return
        except (TelegramForbiddenError, TelegramNetworkError) as exc:
            log.warning("progress_edit_unreachable", job_id=job.id, error=str(exc))
            return

        self._last_text = text
        self._last_edit = moment

    # -- публичное API -------------------------------------------------------- #

    async def set_queue_position(self, position: int) -> None:
        """Показывает место в очереди, пока задача не начала выполняться."""
        async with self._lock:
            if self._finished:
                return
            self._queue_position = position
            await self._edit(
                self._render(), keyboard=cancel_keyboard(self._job.id), force=False
            )

    async def push(self, stage: Stage, detail: str = "", *, force: bool = False) -> None:
        """Обновляет стадию и уточнение. Смена стадии всегда показывается сразу."""
        async with self._lock:
            if self._finished:
                return
            stage_changed = stage is not self._stage
            self._stage = stage
            self._detail = detail
            if stage is not Stage.QUEUED:
                self._queue_position = None
            await self._edit(
                self._render(),
                keyboard=cancel_keyboard(self._job.id),
                force=force or stage_changed,
            )

    async def success(self, lines: Sequence[str] = ()) -> None:
        """Финальное состояние: готово. Кнопка отмены убирается."""
        async with self._lock:
            self._finished = True
            self._stage = Stage.FINISHED
            body = [self._header(), self._mode_line(), ""]
            body.append(f"✅ Готово за {_format_elapsed(time.monotonic() - self._started)}")
            for line in lines:
                body.append(_escape(line))
            body.append("")
            body.append(f"<code>#{self._job.id}</code>")
            await self._edit("\n".join(body), keyboard=None, force=True)

    async def failure(self, message: str, *, detail: str | None = None) -> None:
        """Финальное состояние: ошибка. Текст уже подготовлен для пользователя."""
        async with self._lock:
            self._finished = True
            body = [self._header(), self._mode_line(), "", f"❌ {_escape(message)}"]
            if detail:
                body.append("")
                body.append(f"<code>{_escape(detail[:300])}</code>")
            body.append("")
            body.append(f"<code>#{self._job.id}</code>")
            await self._edit("\n".join(body), keyboard=None, force=True)

    async def cancelled(self) -> None:
        """Финальное состояние: отменено пользователем или остановкой бота."""
        async with self._lock:
            self._finished = True
            body = [
                self._header(),
                self._mode_line(),
                "",
                "🚫 Задача отменена",
                "",
                f"<code>#{self._job.id}</code>",
            ]
            await self._edit("\n".join(body), keyboard=None, force=True)

    @property
    def finished(self) -> bool:
        return self._finished
