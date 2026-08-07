"""Очередь задач: приём, последовательная обработка, отмена, graceful shutdown.

Устройство:

* Очередь в памяти (``asyncio.Queue``) плюс зеркало в SQLite. При рестарте
  ``recover_interrupted()`` возвращает недоделанные задачи обратно в очередь.
* Воркеров ``queue.concurrency`` штук; по умолчанию один, потому что Яндекс
  плохо реагирует на параллельные запросы с одного адреса.
* У каждой выполняющейся задачи есть ``asyncio.Event`` отмены. Он проходит
  насквозь через весь пайплайн: до дочерних процессов, до пауз между
  попытками, до потокового скачивания. Отмена срабатывает за доли секунды,
  а не «когда дойдёт до следующей стадии».
* Остановка: приём новых задач прекращается, текущая доводится до конца
  (в пределах ``queue.shutdown_grace_sec``), остальные остаются в базе
  в статусе pending и подхватятся после перезапуска.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from aiogram import Bot

from app.config import Settings
from app.delivery import Delivery, DeliveryOutcome
from app.jobs.models import Job, JobStatus, Stage
from app.jobs.progress import ProgressView
from app.logging_setup import get_logger
from app.pipeline.errors import (
    JobCancelled,
    NotEnoughSpace,
    PipelineError,
    SpaceRanOut,
)
from app.pipeline.runner import PipelineRunner, PipelineResult
from app.storage.cache import CacheEntry, ResultCache
from app.storage.jobs_repo import JobsRepository
from app.utils.disk import DiskGuard, DiskStatus, format_bytes
from app.utils.disk import NotEnoughSpace as GuardNotEnoughSpace
from app.utils.tempdirs import TempWorkspace

__all__ = ["QueueManager", "QueueFull", "QueueSnapshot"]

log = get_logger(__name__)


class QueueFull(RuntimeError):
    """Очередь переполнена: больше ``queue.max_pending`` задач не принимаем."""


@dataclass(slots=True)
class QueueSnapshot:
    """Состояние очереди для команды /queue."""

    running: list[Job]
    pending: list[Job]

    @property
    def total(self) -> int:
        return len(self.running) + len(self.pending)


class QueueManager:
    """Владелец очереди и воркеров."""

    def __init__(
        self,
        settings: Settings,
        *,
        bot: Bot,
        repo: JobsRepository,
        cache: ResultCache,
        runner: PipelineRunner,
        delivery: Delivery,
        disk: DiskGuard,
    ) -> None:
        self._settings = settings
        self._bot = bot
        self._repo = repo
        self._cache = cache
        self._runner = runner
        self._delivery = delivery
        self._disk = disk

        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()

        self._pending: dict[int, Job] = {}
        self._running: dict[int, Job] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._views: dict[int, ProgressView] = {}

    # -- жизненный цикл -------------------------------------------------------- #

    async def start(self) -> None:
        """Запускает воркеров и возвращает в очередь прерванные задачи."""
        recovered = await self._repo.recover_interrupted()
        for job in recovered:
            self._pending[job.id] = job
            self._queue.put_nowait(job.id)
        if recovered:
            self._drained.clear()

        for index in range(self._settings.queue.concurrency):
            task = asyncio.create_task(self._worker(index), name=f"job-worker-{index}")
            self._workers.append(task)

        log.info(
            "queue_started",
            workers=len(self._workers),
            recovered=len(recovered),
        )

        for job in recovered:
            await self._notify_recovered(job)

    async def shutdown(self) -> None:
        """Останавливает приём задач и ждёт завершения текущих."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        grace = self._settings.queue.shutdown_grace_sec

        active = list(self._running.values())
        if active:
            log.info(
                "queue_shutdown_waiting",
                running=len(active),
                grace_sec=grace,
                jobs=[job.id for job in active],
            )
            for job in active:
                view = self._views.get(job.id)
                if view is not None:
                    await view.push(
                        job.stage,
                        "бот перезапускается, задача доводится до конца",
                        force=True,
                    )

        # Будим воркеров, ожидающих на пустой очереди.
        for _ in self._workers:
            self._queue.put_nowait(-1)

        try:
            await asyncio.wait_for(
                asyncio.gather(*self._workers, return_exceptions=True),
                timeout=grace if grace > 0 else None,
            )
        except asyncio.TimeoutError:
            log.warning("queue_shutdown_timeout", grace_sec=grace)
            for job_id, event in list(self._cancel_events.items()):
                log.warning("queue_force_cancel", job_id=job_id)
                event.set()
            # Второе ожидание — тоже с лимитом. Если воркер завис так, что не
            # реагирует даже на отмену, глухое ожидание не даст контейнеру
            # остановиться вообще и Docker убьёт его SIGKILL-ом, потеряв уборку.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True), timeout=60
                )
            except asyncio.TimeoutError:
                log.error("queue_shutdown_forced", hint="воркеры не ответили на отмену")
                for task in self._workers:
                    task.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)

        pending_left = len(self._pending)
        if pending_left:
            log.info("queue_pending_left", count=pending_left)
        log.info("queue_stopped")

    # -- приём задач ----------------------------------------------------------- #

    async def submit(self, job: Job, view_factory_message_id: int | None = None) -> Job:
        """Ставит задачу в очередь. Задача уже должна быть создана в базе."""
        if self._stopping.is_set():
            raise QueueFull("Бот останавливается, новые задачи не принимаются")

        if len(self._pending) >= self._settings.queue.max_pending:
            raise QueueFull(
                f"В очереди уже {len(self._pending)} задач — это максимум "
                f"(queue.max_pending). Дождись, пока освободится место."
            )

        if view_factory_message_id is not None:
            job.progress_message_id = view_factory_message_id

        self._pending[job.id] = job
        self._views[job.id] = ProgressView(
            self._bot, job, interval=self._settings.telegram.progress_edit_interval_sec
        )
        self._drained.clear()
        self._queue.put_nowait(job.id)

        position = len(self._pending) + len(self._running)
        if position > 1:
            await self._views[job.id].set_queue_position(position)

        log.info("job_submitted", job_id=job.id, mode=job.mode.value, url=job.url)
        return job

    async def cancel(self, job_id: int, *, user_id: int | None = None) -> tuple[bool, str]:
        """Отменяет задачу. Возвращает ``(получилось, пояснение)``."""
        job = self._running.get(job_id) or self._pending.get(job_id)
        if job is None:
            stored = await self._repo.get(job_id)
            if stored is None:
                return False, f"Задачи #{job_id} не существует."
            if stored.status.is_final:
                return False, f"Задача #{job_id} уже завершена ({stored.status.value})."
            job = stored

        if user_id is not None and job.user_id != user_id:
            return False, "Это не твоя задача."

        if job_id in self._running:
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            log.info("job_cancel_requested", job_id=job_id)
            return True, f"Отменяю задачу #{job_id}, это займёт пару секунд."

        self._pending.pop(job_id, None)
        await self._repo.set_status(job_id, JobStatus.CANCELLED, error="отменена пользователем")
        view = self._views.pop(job_id, None)
        if view is not None:
            await view.cancelled()
        log.info("job_cancelled_pending", job_id=job_id)
        return True, f"Задача #{job_id} убрана из очереди."

    def snapshot(self) -> QueueSnapshot:
        """Текущее состояние очереди для /queue."""
        return QueueSnapshot(
            running=list(self._running.values()),
            pending=list(self._pending.values()),
        )

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    # -- воркер ----------------------------------------------------------------- #

    async def _worker(self, index: int) -> None:
        log.debug("worker_started", worker=index)
        while True:
            job_id = await self._queue.get()
            try:
                if job_id < 0:
                    # Сигнал пробуждения при остановке.
                    return
                if self._stopping.is_set():
                    # Задача остаётся pending в базе и подхватится после старта.
                    log.info("worker_skip_on_shutdown", job_id=job_id)
                    return
                job = self._pending.pop(job_id, None)
                if job is None:
                    continue  # уже отменена
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker_crashed", worker=index, job_id=job_id)
            finally:
                self._queue.task_done()
                if not self._pending and not self._running:
                    self._drained.set()

    async def _process(self, job: Job) -> None:
        """Полный цикл одной задачи: кэш → пайплайн → отдача → уборка."""
        cancel_event = asyncio.Event()
        self._cancel_events[job.id] = cancel_event
        self._running[job.id] = job
        space_ran_out = False
        monitor: asyncio.Task[None] | None = None

        view = self._views.get(job.id)
        if view is None:
            view = ProgressView(
                self._bot, job, interval=self._settings.telegram.progress_edit_interval_sec
            )
            self._views[job.id] = view

        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.stage = Stage.METADATA
        await self._repo.update(job)

        log.info("job_started", job_id=job.id, mode=job.mode.value, url=job.url)

        try:
            if await self._try_cache(job, view):
                return

            self._check_space_before_start(job)

            async def report(stage: Stage, detail: str) -> None:
                if job.stage is not stage:
                    job.stage = stage
                    await self._repo.set_stage(job.id, stage)
                await view.push(stage, detail)

            async def on_low_space(status: DiskStatus) -> None:
                nonlocal space_ran_out
                space_ran_out = True
                await view.push(
                    job.stage,
                    f"место на диске кончилось ({format_bytes(status.free)}) — "
                    "останавливаю",
                    force=True,
                )

            # Монитор живёт ровно столько же, сколько задача. Он взводит тот же
            # cancel_event, что и ручная отмена, поэтому дочерние процессы
            # умирают немедленно, а TempWorkspace вычищается штатно.
            monitor = asyncio.create_task(
                self._disk.monitor(cancel_event, on_low=on_low_space),
                name=f"disk-monitor-{job.id}",
            )

            async with TempWorkspace(
                self._settings.paths.tmp_dir, job_id=job.id
            ) as workspace:
                result = await self._runner.run(
                    job,
                    workspace.path,
                    cancel_event=cancel_event,
                    progress=report,
                )
                await self._repo.update(job)
                await self._deliver(job, result, view, cancel_event)

        except JobCancelled:
            if space_ran_out:
                await self._finish_failed(job, view, SpaceRanOut())
            else:
                await self._finish_cancelled(job, view)
        except asyncio.CancelledError:
            await self._finish_cancelled(job, view)
            raise
        except PipelineError as error:
            await self._finish_failed(job, view, error)
        except Exception as error:  # неожиданное — в лог целиком, пользователю коротко
            log.exception("job_crashed", job_id=job.id)
            job.status = JobStatus.FAILED
            job.error = repr(error)[:1000]
            job.finished_at = time.time()
            await self._repo.update(job)
            await view.failure(
                "Внутренняя ошибка бота. Подробности — в логе "
                "(docker compose logs -f bot).",
                detail=type(error).__name__,
            )
        finally:
            if monitor is not None:
                monitor.cancel()
                # gather с return_exceptions никогда не бросает наружу —
                # в отличие от голого await, который поднял бы CancelledError
                # монитора прямо в блоке finally.
                await asyncio.gather(monitor, return_exceptions=True)
            self._cancel_events.pop(job.id, None)
            self._running.pop(job.id, None)
            self._views.pop(job.id, None)

    # -- шаги ------------------------------------------------------------------- #

    async def _try_cache(self, job: Job, view: ProgressView) -> bool:
        """Пробует отдать результат из кэша. True — отдали, задача закрыта."""
        entry = await self._cache.get(job)
        if entry is None:
            return False

        await view.push(Stage.UPLOAD, "нашёл в кэше, отправляю", force=True)
        job.title = job.title or entry.title

        try:
            outcome = await self._delivery.send_cached(job, entry)
        except FileNotFoundError:
            log.info("cache_entry_unusable", job_id=job.id, key=entry.key)
            await self._cache.drop(entry.key, delete_file=False)
            return False

        if outcome.telegram_file_id:
            await self._cache.remember_file_id(entry.key, outcome.telegram_file_id)

        job.status = JobStatus.DONE
        job.stage = Stage.FINISHED
        job.finished_at = time.time()
        await self._repo.update(job)
        await view.success(["Отдано из кэша — переводить заново не потребовалось."])
        log.info("job_done_from_cache", job_id=job.id)
        return True

    async def _deliver(
        self,
        job: Job,
        result: PipelineResult,
        view: ProgressView,
        cancel_event: asyncio.Event,
    ) -> None:
        """Отправляет результат и кладёт его в кэш."""
        if cancel_event.is_set():
            raise JobCancelled()

        job.stage = Stage.UPLOAD
        await self._repo.set_stage(job.id, Stage.UPLOAD)

        async def progress(detail: str) -> None:
            await view.push(Stage.UPLOAD, detail)

        outcome = await self._delivery.send_result(job, result, progress=progress)

        # Файл кладём в кэш ПОСЛЕ отправки: если отправка не удалась, кэшировать
        # нечего, а рабочий каталог всё равно будет вычищен.
        entry = await self._release_or_store(job, result, outcome)

        job.status = JobStatus.DONE
        job.stage = Stage.FINISHED
        job.finished_at = time.time()
        job.result_path = str(entry.file_path) if entry and entry.file_path else None
        await self._repo.update(job)

        lines = list(result.warnings)
        if outcome.link_url:
            lines.append("Файл отдан прямой ссылкой — он не помещался в Telegram.")
        await view.success(lines)

        log.info(
            "job_done",
            job_id=job.id,
            seconds=round(job.elapsed_sec),
            size_mb=round(result.size_bytes / 1024**2, 1),
            kind=result.kind,
        )

    async def _release_or_store(
        self, job: Job, result: PipelineResult, outcome: DeliveryOutcome
    ) -> "CacheEntry | None":
        """Решает судьбу готового файла сразу после подтверждённой отправки.

        Это тот самый «хук освобождения места». Три случая:

        1. Файл ушёл в Telegram и вернулся ``file_id``, а
           ``cache.keep_files_after_send`` выключен — в кэш пишется только
           ``file_id``, файл на диск не копируется вовсе. Он остаётся во
           временном каталоге задачи и исчезает вместе с ним через пару
           секунд, при выходе из ``TempWorkspace``. Повторная выдача этого
           ролика всё равно мгновенная: она идёт по ``file_id`` с серверов
           Telegram, минуя диск полностью.

        2. Файл ушёл ссылкой — его нельзя трогать, пока не скачали. Копия
           уже лежит в ``files_dir`` под токеном; в кэш кладём результат
           обычным порядком, а уборка ссылок удалит копию, когда сеанс
           скачивания завершится.

        3. ``keep_files_after_send`` включён — обычное поведение, файл
           переезжает в кэш и живёт там до истечения TTL.
        """
        keep = self._settings.cache.keep_files_after_send

        if outcome.telegram_file_id and not keep:
            entry = await self._cache.store_reference(
                job,
                telegram_file_id=outcome.telegram_file_id,
                size_bytes=result.size_bytes,
            )
            status = self._disk.status()
            log.info(
                "space_released_after_send",
                job_id=job.id,
                skipped_mb=round(result.size_bytes / 1024**2, 1),
                free_mb=round(status.free / 1024**2),
            )
            return entry

        entry = await self._cache.store(
            job,
            result.path,
            telegram_file_id=outcome.telegram_file_id,
            # Переносить файл можно, только если он больше никому не нужен.
            # При отдаче ссылкой в files_dir лежит жёсткая ссылка на него,
            # так что перенос безопасен и там, но копию оставляем на месте.
            move=outcome.link_url is None,
        )
        if entry is not None and outcome.telegram_file_id:
            await self._cache.remember_file_id(entry.key, outcome.telegram_file_id)
        return entry

    def _check_space_before_start(self, job: Job) -> None:
        """Грубая проверка нижнего порога до запуска пайплайна.

        Точная оценка по длительности делается позже, в runner: там уже
        известны метаданные. Здесь отсекается очевидный случай «на диске
        и так почти ничего нет».
        """
        if not self._settings.disk.check_before_job:
            return
        try:
            self._disk.check_minimum()
        except GuardNotEnoughSpace as exc:
            raise NotEnoughSpace(
                need_bytes=exc.need,
                free_bytes=exc.available,
                detail=f"стартовая проверка: {exc}",
                suggest_lower_quality=False,
            ) from exc

    async def _finish_failed(
        self, job: Job, view: ProgressView, error: PipelineError
    ) -> None:
        log.warning(
            "job_failed",
            job_id=job.id,
            error=type(error).__name__,
            detail=error.detail[:500],
        )
        job.status = JobStatus.FAILED
        job.error = f"{type(error).__name__}: {error.detail}"[:1000]
        job.finished_at = time.time()
        await self._repo.update(job)
        await view.failure(error.render())

    async def _finish_cancelled(self, job: Job, view: ProgressView) -> None:
        job.status = JobStatus.CANCELLED
        job.error = "отменена"
        job.finished_at = time.time()
        await self._repo.update(job)
        await view.cancelled()
        log.info("job_cancelled", job_id=job.id, seconds=round(job.elapsed_sec))

    async def _notify_recovered(self, job: Job) -> None:
        """Сообщает, что прерванная рестартом задача вернулась в очередь."""
        view = self._views.get(job.id)
        if view is None:
            view = ProgressView(
                self._bot, job, interval=self._settings.telegram.progress_edit_interval_sec
            )
            self._views[job.id] = view
        try:
            await view.push(
                Stage.QUEUED, "бот перезапустился, задача вернулась в очередь", force=True
            )
        except Exception:
            log.debug("recovered_notify_failed", job_id=job.id, exc_info=True)
