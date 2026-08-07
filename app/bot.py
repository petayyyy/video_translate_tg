"""Сборка приложения: бот, диспетчер, зависимости, фоновая уборка.

Класс ``Application`` — единственное место, где все части знают друг о друге.
Он же отвечает за корректный порядок запуска и остановки:

    старт:    база → клиенты → очередь → HTTP → polling
    останов:  polling → очередь (дожидаемся текущей задачи) → HTTP → база

Зависимости обработчиков передаются через ``workflow_data`` диспетчера:
aiogram подставляет их в параметры по имени, поэтому в обработчиках нет
ни глобальных переменных, ни импортов из этого модуля.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from app.config import Settings
from app.delivery import Delivery
from app.handlers import build_router
from app.httpapi import HttpApi
from app.jobs.manager import QueueManager
from app.logging_setup import get_logger, register_secret
from app.middlewares import LoggingContextMiddleware, WhitelistMiddleware
from app.pipeline.ffmpeg import FfmpegClient
from app.pipeline.runner import PipelineRunner
from app.pipeline.vot import VotClient
from app.pipeline.ytdlp import YtdlpClient
from app.storage.cache import ResultCache
from app.storage.db import Database
from app.storage.jobs_repo import JobsRepository
from app.storage.links import LinkStore
from app.utils.disk import DiskGuard
from app.utils.proc import which
from app.utils.tempdirs import cleanup_all, sweep_stale

__all__ = ["Application"]

log = get_logger(__name__)

BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="help", description="справка"),
    BotCommand(command="audio", description="только русская дорожка MP3"),
    BotCommand(command="subs", description="только субтитры SRT"),
    BotCommand(command="orig", description="видео, оригинал заглушён"),
    BotCommand(command="q720", description="разово понизить качество до 720p"),
    BotCommand(command="queue", description="что сейчас в работе"),
    BotCommand(command="cancel", description="отменить задачу"),
    BotCommand(command="stats", description="кэш, очередь, диск"),
    BotCommand(command="cleanup", description="освободить место на диске"),
]


class Application:
    """Контейнер зависимостей и точка управления жизненным циклом."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        register_secret(settings.telegram.token)
        register_secret(settings.vot.api_token)

        self.database = Database(settings.paths.db_path)
        self.repo = JobsRepository(self.database)
        self.cache = ResultCache(self.database, settings.cache, settings.paths.cache_dir)
        self.links = LinkStore(self.database, settings.links, settings.paths.files_dir)

        self.http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) video_tg/1.0"},
        )

        self.disk = DiskGuard(settings.disk, settings.paths.data_dir)

        self.vot = VotClient(settings.vot, self.http, settings.priority)
        self.ytdlp = YtdlpClient(settings.ytdlp, settings.paths, settings.priority)
        self.ffmpeg = FfmpegClient(settings.ffmpeg, settings.priority)
        self.runner = PipelineRunner(
            settings,
            vot=self.vot,
            ytdlp=self.ytdlp,
            ffmpeg=self.ffmpeg,
            disk=self.disk,
        )

        self.bot = self._build_bot()
        self.delivery = Delivery(self.bot, settings, self.links)
        self.manager = QueueManager(
            settings,
            bot=self.bot,
            repo=self.repo,
            cache=self.cache,
            runner=self.runner,
            delivery=self.delivery,
            disk=self.disk,
        )
        self.dispatcher = self._build_dispatcher()
        self.httpapi = HttpApi(
            settings,
            self.links,
            extra_status={
                "queue": lambda: self.manager.snapshot().total,
                "stopping": lambda: self.manager.is_stopping,
            },
        )

        self._background: list[asyncio.Task[None]] = []
        self._stop_requested = asyncio.Event()

    # -- сборка ---------------------------------------------------------------- #

    def _build_bot(self) -> Bot:
        telegram = self.settings.telegram
        if telegram.use_local_api:
            api = TelegramAPIServer.from_base(telegram.api_base_url, is_local=True)
            log.info("using_local_bot_api", base_url=telegram.api_base_url)
        else:
            api = TelegramAPIServer.from_base("https://api.telegram.org")
            log.warning(
                "using_cloud_bot_api",
                hint="лимит отправки 50 МБ; для 2 ГБ включи telegram.use_local_api",
            )

        session = AiohttpSession(api=api, timeout=telegram.upload_timeout_sec)
        return Bot(
            token=telegram.token,
            session=session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )

    def _build_dispatcher(self) -> Dispatcher:
        dispatcher = Dispatcher(
            settings=self.settings,
            manager=self.manager,
            repo=self.repo,
            cache=self.cache,
            links=self.links,
            delivery=self.delivery,
            disk=self.disk,
        )
        dispatcher.update.outer_middleware(LoggingContextMiddleware())
        dispatcher.update.outer_middleware(
            WhitelistMiddleware(self.settings.telegram.allowed_user_ids)
        )
        dispatcher.include_router(build_router())
        return dispatcher

    # -- предполётные проверки -------------------------------------------------- #

    def preflight(self) -> None:
        """Проверяет наличие внешних утилит, место на диске и создаёт каталоги."""
        self.settings.paths.ensure_directories()

        status = self.disk.status()
        log.info(
            "disk_status",
            free_gb=round(status.free_gb, 2),
            total_gb=round(status.total_gb, 2),
            used_percent=round(status.used_percent),
        )
        minimum = self.settings.disk.min_free_gb
        if status.free_gb < minimum:
            # Не падаем: бот полезен и в таком состоянии — он покажет /stats
            # и даст выполнить /cleanup. Но предупредить обязан громко.
            log.error(
                "disk_low_at_startup",
                free_gb=round(status.free_gb, 2),
                min_free_gb=minimum,
                hint="новые задачи приниматься не будут; освободи место "
                "или выполни /cleanup в чате с ботом",
            )
        elif status.free_gb < minimum * 2:
            log.warning(
                "disk_getting_low",
                free_gb=round(status.free_gb, 2),
                hint="места хватит на одну-две задачи",
            )

        removed = sweep_stale(self.settings.paths.tmp_dir, older_than_sec=0.0)
        if removed:
            log.info("stale_tmp_cleaned", count=removed)

        missing: list[str] = []
        for binary in (
            self.settings.vot.binary,
            self.settings.ytdlp.binary,
            self.settings.ffmpeg.binary,
            self.settings.ffmpeg.ffprobe_binary,
        ):
            resolved = which(binary)
            if resolved is None:
                missing.append(binary)
            else:
                log.info("tool_found", tool=binary, path=resolved)

        if missing:
            raise RuntimeError(
                "Не найдены внешние утилиты: "
                + ", ".join(missing)
                + ". Пересобери образ: docker compose build --no-cache"
            )

    # -- запуск и остановка ------------------------------------------------------ #

    async def start(self) -> None:
        self.preflight()
        await self.database.connect()

        me = await self.bot.get_me()
        log.info("bot_identified", username=me.username, bot_id=me.id)

        try:
            await self.bot.set_my_commands(BOT_COMMANDS)
        except TelegramAPIError as exc:
            log.warning("set_commands_failed", error=str(exc))

        await self.manager.start()
        await self.httpapi.start()

        self._background = [
            asyncio.create_task(self._cleanup_loop(), name="cleanup-loop"),
        ]

        log.info(
            "application_started",
            allowed_users=len(self.settings.telegram.allowed_user_ids),
            concurrency=self.settings.queue.concurrency,
            vot_flavor=self.settings.vot.flavor,
        )

    async def run(self) -> None:
        """Основной цикл: polling до запроса остановки."""
        polling = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                handle_signals=False,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
            ),
            name="polling",
        )
        stopper = asyncio.create_task(self._stop_requested.wait(), name="stop-waiter")

        done, _pending = await asyncio.wait(
            {polling, stopper}, return_when=asyncio.FIRST_COMPLETED
        )

        if polling in done:
            # Polling упал сам — вытащим исключение в лог.
            stopper.cancel()
            exception = polling.exception()
            if exception is not None:
                log.error("polling_failed", error=str(exception))
                raise exception
            return

        log.info("stop_requested")
        await self.dispatcher.stop_polling()
        try:
            await asyncio.wait_for(polling, timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            polling.cancel()

    def request_stop(self) -> None:
        """Просит приложение остановиться. Безопасно из обработчика сигнала."""
        self._stop_requested.set()

    async def stop(self) -> None:
        """Останавливает всё в обратном порядке. Идемпотентно."""
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background = []

        await self.manager.shutdown()
        await self.httpapi.stop()

        try:
            await self.bot.session.close()
        except Exception:
            log.debug("bot_session_close_failed", exc_info=True)

        await self.http.aclose()
        await self.database.close()
        cleanup_all()

        log.info("application_stopped")

    # -- фоновая уборка ------------------------------------------------------------ #

    async def _cleanup_loop(self) -> None:
        """Периодически чистит кэш, ссылки, историю и осиротевшие каталоги."""
        cache_interval = self.settings.cache.cleanup_interval_min * 60
        links_interval = self.settings.links.cleanup_interval_min * 60
        interval = max(60.0, min(cache_interval, links_interval))

        # Первая уборка — сразу после старта: за время простоя могло протухнуть.
        next_cache = 0.0
        next_links = 0.0

        while True:
            try:
                loop_time = asyncio.get_running_loop().time()

                if loop_time >= next_links and self.settings.links.enabled:
                    await self.links.cleanup_expired()
                    next_links = loop_time + links_interval

                if loop_time >= next_cache:
                    await self.cache.cleanup_expired()
                    await self.cache.enforce_size_limit()
                    await self.repo.purge_old(self.settings.queue.history_ttl_days)
                    sweep_stale(self.settings.paths.tmp_dir, older_than_sec=6 * 3600)
                    next_cache = loop_time + cache_interval

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cleanup_loop_error")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    # -- диагностика ----------------------------------------------------------------- #

    async def status(self) -> dict[str, Any]:
        snapshot = self.manager.snapshot()
        return {
            "queue_running": len(snapshot.running),
            "queue_pending": len(snapshot.pending),
            "cache": await self.cache.stats(),
            "links": await self.links.stats(),
        }
