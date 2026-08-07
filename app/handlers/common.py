"""Команды общего назначения: /start, /help, /queue, /cancel, /stats, /id."""

from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.jobs.manager import QueueManager
from app.jobs.models import Job
from app.logging_setup import get_logger
from app.storage.cache import ResultCache
from app.storage.jobs_repo import JobsRepository
from app.storage.links import LinkStore
from app.utils.disk import DiskGuard
from app.utils.tempdirs import sweep_stale

__all__ = ["router"]

log = get_logger(__name__)

router = Router(name="common")


HELP_TEXT = """<b>Что я умею</b>

Пришли ссылку на видео — верну его же с русской закадровой озвучкой
от Яндекса, наложенной поверх приглушённого оригинала.

<b>Команды</b>
/audio &lt;ссылка&gt; — только русская дорожка, MP3
/subs &lt;ссылка&gt; — только субтитры, SRT
/orig &lt;ссылка&gt; — видео, оригинал заглушён полностью
/q720 &lt;ссылка&gt; — разово понизить качество до 720p

/queue — что сейчас в работе
/cancel &lt;id&gt; — отменить задачу
/stats — кэш, очередь, место на диске
/cleanup — освободить место на диске
/help — эта справка

<b>Полезно знать</b>
• Команду можно отправить и ответом на сообщение со ссылкой — тогда
  саму ссылку писать не нужно.
• Ролик, который уже переводился, отдаётся из кэша мгновенно.
• Яндекс переводит асинхронно: первые минуты сообщение будет висеть
  на «Яндекс переводит» — это нормально, длинные лекции требуют времени.
• Поддерживаются YouTube, Vimeo, VK, Rutube, Twitch, Coub, Bilibili,
  Dailymotion, Одноклассники и ещё несколько десятков площадок.
"""


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _format_size(num_bytes: float) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} ГБ"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.0f} МБ"
    return f"{num_bytes / 1024:.0f} КБ"


def _format_elapsed(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _job_line(job: Job, *, running: bool) -> str:
    icon = job.stage.icon if running else "⏸"
    title = _escape(job.display_title[:60])
    parts = [f"{icon} <code>#{job.id}</code> {title}"]
    tail = [job.mode.title]
    if running:
        tail.append(job.stage.title)
        tail.append(_format_elapsed(job.elapsed_sec))
    parts.append("    <i>" + _escape(" · ".join(tail)) + "</i>")
    return "\n".join(parts)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет. Пришли ссылку на видео — верну его с русской озвучкой.\n\n"
        "Список команд: /help",
    )


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(HELP_TEXT, disable_web_page_preview=True)


@router.message(Command("id"))
async def handle_id(message: Message) -> None:
    """Показывает user_id — удобно при первичной настройке белого списка."""
    user_id = message.from_user.id if message.from_user else "?"
    await message.answer(
        f"Твой user_id: <code>{user_id}</code>\n"
        f"ID этого чата: <code>{message.chat.id}</code>"
    )


@router.message(Command("queue"))
async def handle_queue(message: Message, manager: QueueManager) -> None:
    snapshot = manager.snapshot()

    if snapshot.total == 0:
        text = "Очередь пуста."
        if manager.is_stopping:
            text += "\n\n⚠️ Бот останавливается, новые задачи не принимаются."
        await message.answer(text)
        return

    lines: list[str] = []
    if snapshot.running:
        lines.append("<b>В работе</b>")
        lines.extend(_job_line(job, running=True) for job in snapshot.running)
    if snapshot.pending:
        if lines:
            lines.append("")
        lines.append(f"<b>Ждут очереди: {len(snapshot.pending)}</b>")
        lines.extend(_job_line(job, running=False) for job in snapshot.pending)

    lines.append("")
    lines.append("Отменить: <code>/cancel &lt;id&gt;</code>")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("cancel"))
async def handle_cancel(
    message: Message, command: CommandObject, manager: QueueManager
) -> None:
    argument = (command.args or "").strip().lstrip("#")

    if not argument:
        snapshot = manager.snapshot()
        if snapshot.total == 1:
            only = (snapshot.running + snapshot.pending)[0]
            _ok, reply = await manager.cancel(
                only.id, user_id=message.from_user.id if message.from_user else None
            )
            await message.answer(reply)
            return
        await message.answer(
            "Укажи номер задачи: <code>/cancel 17</code>\n"
            "Список номеров — в /queue"
        )
        return

    if not argument.isdigit():
        await message.answer("Номер задачи — это число. Например: <code>/cancel 17</code>")
        return

    _ok, reply = await manager.cancel(
        int(argument), user_id=message.from_user.id if message.from_user else None
    )
    await message.answer(reply)


@router.callback_query(F.data.startswith("cancel:"))
async def handle_cancel_button(query: CallbackQuery, manager: QueueManager) -> None:
    raw = (query.data or "").split(":", 1)[-1]
    if not raw.isdigit():
        await query.answer("Некорректная кнопка")
        return
    ok, reply = await manager.cancel(
        int(raw), user_id=query.from_user.id if query.from_user else None
    )
    await query.answer(reply, show_alert=not ok)


@router.message(Command("stats"))
async def handle_stats(
    message: Message,
    manager: QueueManager,
    cache: ResultCache,
    links: LinkStore,
    repo: JobsRepository,
    settings: Settings,
    disk: DiskGuard,
) -> None:
    cache_stats = await cache.stats()
    link_stats = await links.stats()
    snapshot = manager.snapshot()
    recent = await repo.list_recent(
        message.from_user.id if message.from_user else 0, limit=200
    )

    done = sum(1 for job in recent if job.status.value == "done")
    failed = sum(1 for job in recent if job.status.value == "failed")

    status = disk.status()
    bar = _disk_bar(status.used_percent)
    minimum = settings.disk.min_free_gb

    if status.free_gb < minimum:
        verdict = "🔴 ниже порога — новые задачи не принимаются"
    elif status.free_gb < minimum * 2:
        verdict = "🟡 места мало, хватит на одну-две задачи"
    else:
        verdict = "🟢 места достаточно"

    lines = [
        "<b>Диск</b>",
        f"<code>{bar}</code> {status.used_percent:.0f}%",
        f"свободно {_format_size(status.free)} из {_format_size(status.total)}",
        verdict,
        f"<i>порог: {minimum:g} ГБ · доступно под задачу: "
        f"{_format_size(disk.available_for_work())}</i>",
        "",
        "<b>Очередь</b>",
        f"в работе: {len(snapshot.running)} · ждут: {len(snapshot.pending)}",
        "",
        "<b>Кэш</b>",
        f"записей: {cache_stats['entries']} · файлов на диске: {cache_stats['files']} · "
        f"{_format_size(cache_stats['bytes'])}",
        f"попаданий: {cache_stats['hits']}",
    ]
    if not settings.cache.keep_files_after_send:
        lines.append(
            "<i>файлы удаляются сразу после отправки, хранятся только file_id</i>"
        )

    lines += [
        "",
        "<b>Выложено ссылками</b>",
        f"активных: {link_stats['active']} · {_format_size(link_stats['bytes'])}",
        "",
        "<b>История (последние 200)</b>",
        f"успешно: {done} · с ошибкой: {failed}",
        "",
        "Освободить место: /cleanup",
    ]

    await message.answer("\n".join(lines))


@router.message(Command("cleanup"))
async def handle_cleanup(
    message: Message,
    manager: QueueManager,
    cache: ResultCache,
    links: LinkStore,
    repo: JobsRepository,
    settings: Settings,
    disk: DiskGuard,
) -> None:
    """Принудительно освобождает место: кэш, протухшие ссылки, временные файлы.

    Записи кэша с ``file_id`` не теряются — удаляются только сами файлы,
    повторная выдача роликов останется мгновенной.
    """
    snapshot = manager.snapshot()
    if snapshot.running:
        await message.answer(
            "Сейчас идёт обработка задачи — уборка может забрать файлы, "
            "которые ей нужны.\n"
            f"Дождись завершения (/queue) или отмени: /cancel {snapshot.running[0].id}"
        )
        return

    before = disk.status().free
    notice = await message.answer("🧹 Убираю…")

    files, freed_cache = await cache.purge_all_files()
    expired_links = await links.cleanup_expired()
    await repo.purge_old(settings.queue.history_ttl_days)
    stale = await asyncio.to_thread(
        sweep_stale, settings.paths.tmp_dir, older_than_sec=0.0
    )

    after = disk.status()
    freed_total = max(0, after.free - before)

    lines = [
        "✅ <b>Уборка завершена</b>",
        "",
        f"файлов кэша удалено: {files} ({_format_size(freed_cache)})",
        f"ссылок убрано: {expired_links}",
        f"временных каталогов: {stale}",
        "",
        f"освобождено: <b>{_format_size(freed_total)}</b>",
        f"свободно теперь: {_format_size(after.free)} из {_format_size(after.total)}",
    ]
    if cache.keeps_file_ids:
        lines += ["", "<i>file_id сохранены — уже переведённые ролики "
                  "по-прежнему отдаются мгновенно</i>"]

    await notice.edit_text("\n".join(lines))


def _disk_bar(used_percent: float, width: int = 20) -> str:
    """Текстовая полоска заполнения диска."""
    filled = int(round(used_percent / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)
