"""Оркестрация стадий обработки одной задачи.

Runner ничего не знает ни про Telegram, ни про очередь: на вход получает
``Job`` и колбэк прогресса, на выходе отдаёт готовый файл. Благодаря этому
его можно вызвать из воркера, из скрипта или из теста одинаково.

Порядок стадий зависит от режима:

    /subs   → метаданные → субтитры
    /audio  → метаданные → перевод → дорожка → MP3
    обычный → метаданные → перевод → дорожка → видео → склейка
    /orig   → то же, но оригинал не подмешивается

Порядок «сначала перевод, потом видео» выбран сознательно: если Яндекс этот
ролик не переводит, мы узнаем об этом до того, как скачаем гигабайт видео.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.config import Settings
from app.jobs.models import Job, JobMode, Stage
from app.logging_setup import get_logger
from app.pipeline.errors import MuxFailed, NotEnoughSpace
from app.pipeline.ffmpeg import FfmpegClient, MediaInfo
from app.pipeline.vot import VotClient
from app.pipeline.ytdlp import VideoMeta, YtdlpClient
from app.storage.links import sanitize_filename
from app.utils.disk import DiskGuard, estimate_audio_bytes, estimate_video_bytes
from app.utils.disk import NotEnoughSpace as GuardNotEnoughSpace
from app.utils.urls import VideoRef, parse_video_ref

__all__ = ["PipelineResult", "PipelineRunner", "ProgressReporter"]

log = get_logger(__name__)

#: Колбэк прогресса: (стадия, уточнение) → корутина.
ProgressReporter = Callable[[Stage, str], Awaitable[None]]


async def _noop(stage: Stage, detail: str) -> None:  # noqa: ARG001
    return None


@dataclass(slots=True)
class PipelineResult:
    """Готовый к отдаче результат."""

    path: Path
    kind: str  # "video" | "audio" | "subtitles"
    suggested_name: str
    title: str
    duration_sec: float | None
    size_bytes: int
    width: int | None = None
    height: int | None = None
    warnings: list[str] = field(default_factory=list)


class PipelineRunner:
    """Выполняет все стадии обработки задачи."""

    __slots__ = ("_settings", "_vot", "_ytdlp", "_ffmpeg", "_disk")

    def __init__(
        self,
        settings: Settings,
        *,
        vot: VotClient,
        ytdlp: YtdlpClient,
        ffmpeg: FfmpegClient,
        disk: DiskGuard,
    ) -> None:
        self._settings = settings
        self._vot = vot
        self._ytdlp = ytdlp
        self._ffmpeg = ffmpeg
        self._disk = disk

    async def run(
        self,
        job: Job,
        workdir: Path,
        *,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressReporter | None = None,
    ) -> PipelineResult:
        """Обрабатывает задачу целиком. Все ошибки — доменные PipelineError."""
        report = progress or _noop
        ref = parse_video_ref(job.url)

        # 1. Метаданные ------------------------------------------------------
        await report(Stage.METADATA, "")
        meta = await self._ytdlp.probe(ref, cancel_event=cancel_event)

        job.title = meta.title
        job.duration_sec = meta.duration_sec

        warnings: list[str] = []

        # 1a. Хватит ли места ------------------------------------------------
        # Делается сразу после метаданных: здесь уже известны длительность
        # и качество, но ещё ничего не скачано. Отказать сейчас дёшево.
        if job.mode.needs_video and self._settings.disk.check_before_job:
            self._check_space(job, meta, warnings)
        if not ref.supported:
            warnings.append(
                f"Площадка {ref.platform_title} не в списке заведомо поддерживаемых — "
                "если что-то пойдёт не так, дело скорее всего в этом."
            )

        if job.mode is JobMode.SUBS:
            return await self._run_subtitles(
                job, ref, meta, workdir, cancel_event, report, warnings
            )

        # 2. Перевод + русская дорожка ---------------------------------------
        await report(Stage.TRANSLATE, "отправляю ролик на перевод")

        async def _vot_progress(detail: str) -> None:
            stage = Stage.FETCH_AUDIO if detail.startswith("качаю") else Stage.TRANSLATE
            await report(stage, detail)

        artifact = await self._vot.get_audio(
            ref,
            workdir,
            source_lang=meta.language,
            cancel_event=cancel_event,
            progress=_vot_progress,
        )

        log.info(
            "translation_ready",
            job_id=job.id,
            attempts=artifact.attempts,
            waited=round(artifact.waited_sec),
            size_mb=round(artifact.size_bytes / 1024**2, 1),
        )

        if job.mode is JobMode.AUDIO:
            return await self._finish_audio(job, meta, artifact.path, workdir, cancel_event, warnings)

        # 3. Видео ------------------------------------------------------------
        await report(Stage.FETCH_VIDEO, "")

        async def _download_progress(detail: str) -> None:
            await report(Stage.FETCH_VIDEO, detail)

        video_path = await self._ytdlp.download(
            ref,
            workdir / "video",
            max_height=job.max_height,
            # Потолок пересчитывается прямо сейчас, а не на старте задачи:
            # пока шёл перевод, место могла занять другая задача или уборка,
            # наоборот, его освободила.
            max_bytes=(
                self._disk.max_download_bytes()
                if self._settings.disk.limit_download_size
                else 0
            ),
            cancel_event=cancel_event,
            progress=_download_progress,
        )

        # 4. Склейка -----------------------------------------------------------
        await report(Stage.MUX, "")

        video_info = await self._ffmpeg.probe(video_path, cancel_event=cancel_event)
        audio_info = await self._ffmpeg.probe(artifact.path, cancel_event=cancel_event)

        self._check_sync(video_info, audio_info, warnings)

        if not video_info.has_video:
            raise MuxFailed("в скачанном файле нет видеопотока")

        target = workdir / (sanitize_filename(meta.title, fallback=meta.video_id) + ".mp4")

        async def _mux_progress(detail: str) -> None:
            await report(Stage.MUX, detail)

        await self._ffmpeg.mux(
            video=video_path,
            audio=artifact.path,
            target=target,
            mix_original=job.mode is JobMode.VIDEO,
            video_info=video_info,
            cancel_event=cancel_event,
            progress=_mux_progress,
        )

        result_info = await self._ffmpeg.probe(target, cancel_event=cancel_event)

        return PipelineResult(
            path=target,
            kind="video",
            suggested_name=target.name,
            title=meta.title,
            duration_sec=result_info.duration_sec or meta.duration_sec,
            size_bytes=result_info.size_bytes or target.stat().st_size,
            width=result_info.width,
            height=result_info.height,
            warnings=warnings,
        )

    # -- отдельные ветки ----------------------------------------------------- #

    async def _run_subtitles(
        self,
        job: Job,
        ref: VideoRef,
        meta: VideoMeta,
        workdir: Path,
        cancel_event: asyncio.Event | None,
        report: ProgressReporter,
        warnings: list[str],
    ) -> PipelineResult:
        await report(Stage.TRANSLATE, "запрашиваю субтитры")

        async def _progress(detail: str) -> None:
            stage = Stage.FETCH_SUBS if detail.startswith("качаю") else Stage.TRANSLATE
            await report(stage, detail)

        artifact = await self._vot.get_subtitles(
            ref,
            workdir,
            source_lang=meta.language,
            cancel_event=cancel_event,
            progress=_progress,
        )

        suffix = artifact.path.suffix or ".srt"
        target = workdir / (sanitize_filename(meta.title, fallback=meta.video_id) + suffix)
        if artifact.path != target:
            await asyncio.to_thread(_replace, artifact.path, target)

        return PipelineResult(
            path=target,
            kind="subtitles",
            suggested_name=target.name,
            title=meta.title,
            duration_sec=meta.duration_sec,
            size_bytes=target.stat().st_size,
            warnings=warnings,
        )

    async def _finish_audio(
        self,
        job: Job,
        meta: VideoMeta,
        source: Path,
        workdir: Path,
        cancel_event: asyncio.Event | None,
        warnings: list[str],
    ) -> PipelineResult:
        target = workdir / (sanitize_filename(meta.title, fallback=meta.video_id) + ".mp3")
        await self._ffmpeg.prepare_mp3(
            source, target, title=meta.title, cancel_event=cancel_event
        )
        info = await self._ffmpeg.probe(target, cancel_event=cancel_event)
        return PipelineResult(
            path=target,
            kind="audio",
            suggested_name=target.name,
            title=meta.title,
            duration_sec=info.duration_sec or meta.duration_sec,
            size_bytes=info.size_bytes or target.stat().st_size,
            warnings=warnings,
        )

    # -- проверки ------------------------------------------------------------ #

    def _check_space(self, job: Job, meta: VideoMeta, warnings: list[str]) -> None:
        """Отказывает заранее, если места на ролик заведомо не хватит.

        Размер берётся из ``filesize_approx``, а если yt-dlp его не сообщил
        (для раздельных потоков DASH это обычное дело) — оценивается по
        длительности и запрошенному качеству.
        """
        estimated = meta.filesize_approx or 0
        source = "метаданные"
        if estimated <= 0:
            estimated = estimate_video_bytes(meta.duration_sec, job.max_height)
            source = "оценка по длительности"
        estimated += estimate_audio_bytes(meta.duration_sec)

        if estimated <= 0:
            return

        try:
            self._disk.check_for_job(estimated)
        except GuardNotEnoughSpace as exc:
            log.warning(
                "not_enough_space",
                job_id=job.id,
                need_mb=round(exc.need / 1024**2),
                free_mb=round(exc.available / 1024**2),
                estimate_source=source,
            )
            raise NotEnoughSpace(
                need_bytes=exc.need,
                free_bytes=exc.available,
                detail=f"{source}: {exc}",
                suggest_lower_quality=job.max_height > self._settings.ytdlp.low_height,
            ) from exc

        status = self._disk.status()
        log.info(
            "space_check_passed",
            job_id=job.id,
            need_mb=round(estimated * self._settings.disk.estimate_multiplier / 1024**2),
            free_mb=round(status.free / 1024**2),
        )

    def _check_sync(
        self, video: MediaInfo, audio: MediaInfo, warnings: list[str]
    ) -> None:
        """Предупреждает о заметном расхождении длительностей.

        Яндекс иногда отдаёт дорожку короче или длиннее исходника. Ошибкой
        это не является — озвучка всё равно пригодна, — но пользователю
        честнее сказать заранее, чем получить вопрос «почему в конце тишина».
        """
        tolerance = self._settings.ffmpeg.sync_tolerance_sec
        if tolerance <= 0 or not video.duration_sec or not audio.duration_sec:
            return
        difference = abs(video.duration_sec - audio.duration_sec)
        if difference <= tolerance:
            return
        warnings.append(
            f"Длительность русской дорожки отличается от видео на "
            f"{difference:.0f} с — возможен рассинхрон ближе к концу."
        )
        log.info(
            "duration_mismatch",
            video_sec=round(video.duration_sec),
            audio_sec=round(audio.duration_sec),
            diff=round(difference),
        )


def _replace(source: Path, target: Path) -> None:
    """Переименование внутри рабочего каталога; при неудаче — копирование."""
    try:
        source.replace(target)
    except OSError:
        shutil.copy2(str(source), str(target))
