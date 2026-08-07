"""Приём ссылок и команды режимов: /audio, /subs, /orig, /q720.

Единая точка входа ``_enqueue`` для всех режимов: находит ссылку, проверяет
дубликаты, создаёт задачу в базе, отправляет стартовое сообщение и ставит
задачу в очередь. Различаются режимы только значениями ``JobMode`` и высоты.

Ссылку можно передать тремя способами:

* прямо в команде — ``/audio https://…``;
* ответом на сообщение со ссылкой — ``/audio`` в ответ;
* просто прислать ссылку без команды (основной режим).
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from magic_filter import RegexpMode

from app.config import Settings
from app.jobs.manager import QueueFull, QueueManager
from app.jobs.models import Job, JobMode, JobStatus, Stage
from app.jobs.progress import cancel_keyboard
from app.logging_setup import get_logger
from app.storage.jobs_repo import JobsRepository
from app.utils.urls import extract_first_url, parse_video_ref

__all__ = ["router"]

log = get_logger(__name__)

router = Router(name="links")


def _escape(text: str) -> str:
    """Экранирование для ТЕКСТА внутри тега."""
    return html.escape(text, quote=False)


def _escape_attr(text: str) -> str:
    """Экранирование для значения АТРИБУТА (href): кавычки тоже."""
    return html.escape(text, quote=True)


def _find_url(message: Message, command: CommandObject | None) -> str | None:
    """Ищет ссылку в аргументах команды, в самом сообщении и в ответе на него."""
    if command is not None and command.args:
        found = extract_first_url(command.args)
        if found:
            return found

    found = extract_first_url(message.text or message.caption or "")
    if found:
        return found

    reply = message.reply_to_message
    if reply is not None:
        found = extract_first_url(reply.text or reply.caption or "")
        if found:
            return found

    return None


async def _enqueue(
    message: Message,
    command: CommandObject | None,
    *,
    mode: JobMode,
    max_height: int,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    """Общий путь постановки задачи для всех режимов."""
    url = _find_url(message, command)
    if url is None:
        await message.answer(
            "Не вижу ссылки. Пришли её текстом, добавь к команде "
            "(<code>/audio https://…</code>) или ответь командой на сообщение со ссылкой."
        )
        return

    if message.from_user is None:
        return

    ref = parse_video_ref(url)

    if not ref.supported:
        log.info("unknown_platform", url=ref.url, platform=ref.platform_title)

    # Дубликат: та же ссылка в том же режиме уже стоит в очереди.
    duplicate = await repo.find_active_duplicate(
        message.from_user.id,
        (ref.platform, ref.video_id, mode.value, max_height if mode.needs_video else 0),
    )
    if duplicate is not None:
        await message.answer(
            f"Этот ролик уже в работе — задача <code>#{duplicate.id}</code>.\n"
            "Посмотреть: /queue"
        )
        return

    job = Job(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        url=ref.url,
        platform=ref.platform,
        video_id=ref.video_id,
        mode=mode,
        max_height=max_height if mode.needs_video else 0,
        request_message_id=message.message_id,
        stage=Stage.QUEUED,
    )
    await repo.create(job)

    details = [mode.title]
    if mode.needs_video:
        details.append(f"до {max_height}p")
    if not ref.supported:
        details.append("площадка не в списке проверенных")

    placeholder = await message.answer(
        f'🎬 <a href="{_escape_attr(ref.url)}">{_escape(ref.url)}</a>\n'
        f"<i>{_escape(' · '.join(details))}</i>\n\n"
        f"⏸ принял, ставлю в очередь\n\n"
        f"<code>#{job.id}</code>",
        reply_markup=cancel_keyboard(job.id),
        disable_web_page_preview=True,
    )

    job.progress_message_id = placeholder.message_id
    await repo.update(job)

    try:
        await manager.submit(job)
    except QueueFull as exc:
        await repo.set_status(job.id, JobStatus.CANCELLED, error=str(exc))
        await placeholder.edit_text(f"⚠️ {_escape(str(exc))}")
        return

    log.info(
        "job_queued",
        job_id=job.id,
        mode=mode.value,
        platform=ref.platform,
        video_id=ref.video_id,
    )


@router.message(Command("audio"))
async def handle_audio(
    message: Message,
    command: CommandObject,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    await _enqueue(
        message,
        command,
        mode=JobMode.AUDIO,
        max_height=0,
        manager=manager,
        repo=repo,
        settings=settings,
    )


@router.message(Command("subs", "srt"))
async def handle_subs(
    message: Message,
    command: CommandObject,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    await _enqueue(
        message,
        command,
        mode=JobMode.SUBS,
        max_height=0,
        manager=manager,
        repo=repo,
        settings=settings,
    )


@router.message(Command("orig"))
async def handle_orig(
    message: Message,
    command: CommandObject,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    await _enqueue(
        message,
        command,
        mode=JobMode.ORIG_MUTED,
        max_height=settings.ytdlp.max_height,
        manager=manager,
        repo=repo,
        settings=settings,
    )


@router.message(Command("q720"))
async def handle_q720(
    message: Message,
    command: CommandObject,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    await _enqueue(
        message,
        command,
        mode=JobMode.VIDEO,
        max_height=settings.ytdlp.low_height,
        manager=manager,
        repo=repo,
        settings=settings,
    )


# ВАЖНО: mode=SEARCH обязателен. По умолчанию magic_filter использует
# re.match, то есть требует совпадения с САМОГО НАЧАЛА текста, и сообщение
# вида «смотри https://…» под фильтр не попадало бы — оно уходило бы в
# обработчик «это не похоже на ссылку». Именно так приходит большинство
# пересланных сообщений.
@router.message(F.text.regexp(r"https?://|www\.", mode=RegexpMode.SEARCH))
async def handle_plain_link(
    message: Message,
    manager: QueueManager,
    repo: JobsRepository,
    settings: Settings,
) -> None:
    """Обычное сообщение со ссылкой — основной режим."""
    await _enqueue(
        message,
        None,
        mode=JobMode.VIDEO,
        max_height=settings.ytdlp.max_height,
        manager=manager,
        repo=repo,
        settings=settings,
    )


@router.message(F.text)
async def handle_anything_else(message: Message) -> None:
    """Текст без ссылки — подсказываем, что делать."""
    await message.answer(
        "Это не похоже на ссылку на видео.\n"
        "Пришли ссылку на YouTube (или другую поддерживаемую площадку), "
        "либо посмотри /help"
    )
