"""Обёртка над yt-dlp: метаданные ролика и скачивание видеопотока.

Две операции:

* ``probe()`` — быстрый ``-J``: название, длительность, язык, признак эфира.
  Нужен до всего остального, чтобы отсечь слишком длинные ролики и прямые
  эфиры до того, как мы полчаса прождём перевод, и чтобы подсказать vot-cli
  язык оригинала.
* ``download()`` — собственно скачивание с разбором прогресса.

Прогресс читается не из человекочитаемого вывода, а через
``--progress-template``: yt-dlp печатает ровно ту строку, которую мы просим,
и её не приходится разбирать регулярками по меняющемуся формату.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import PathsSettings, PrioritySettings, YtdlpSettings
from app.logging_setup import get_logger
from app.pipeline.errors import (
    DownloadFailed,
    JobCancelled,
    NotEnoughSpace,
    ToolMissing,
    VideoIsLive,
    VideoTooBig,
    VideoTooLong,
    VideoUnavailable,
    classify_ytdlp_output,
)
from app.utils.proc import (
    ProcCancelled,
    ProcError,
    ProcNotFound,
    ProcTimeout,
    format_command,
    run_process,
)
from app.utils.urls import VideoRef

__all__ = ["VideoMeta", "YtdlpClient"]

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]] | None

#: Формат строки прогресса, который мы просим печатать сам yt-dlp.
_PROGRESS_TEMPLATE = (
    "VTGPROGRESS|%(progress.downloaded_bytes)s|%(progress.total_bytes)s"
    "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
)
_PROGRESS_RE = re.compile(r"^VTGPROGRESS\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)$")


def _to_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw or raw in {"NA", "None", "N/A"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass(slots=True)
class VideoMeta:
    """То, что удалось узнать о ролике до скачивания."""

    video_id: str
    title: str
    duration_sec: float | None
    language: str | None
    is_live: bool
    was_live: bool
    uploader: str | None
    height: int | None
    filesize_approx: int | None
    extractor: str | None

    @property
    def duration_min(self) -> float:
        return (self.duration_sec or 0.0) / 60.0

    @property
    def pretty_duration(self) -> str:
        total = int(self.duration_sec or 0)
        if total <= 0:
            return "?"
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


class YtdlpClient:
    """Запуск yt-dlp с общими для всего бота настройками."""

    __slots__ = ("_settings", "_paths", "_priority")

    def __init__(
        self,
        settings: YtdlpSettings,
        paths: PathsSettings,
        priority: PrioritySettings | None = None,
    ) -> None:
        self._settings = settings
        self._paths = paths
        self._priority = priority or PrioritySettings()

    # -- общие аргументы ---------------------------------------------------- #

    def _common_args(self) -> list[str]:
        settings = self._settings
        args = [
            "--no-playlist",
            "--no-warnings",
            "--no-color",
            "--ignore-config",
            "--retries",
            str(settings.retries),
            "--fragment-retries",
            str(settings.fragment_retries),
            "--socket-timeout",
            "30",
        ]
        if self._paths.cookies_file is not None:
            if self._paths.cookies_file.is_file():
                args += ["--cookies", str(self._paths.cookies_file)]
            else:
                log.warning("cookies_file_missing", path=str(self._paths.cookies_file))
        if settings.proxy:
            args += ["--proxy", settings.proxy]
        return args

    # -- метаданные --------------------------------------------------------- #

    async def probe(
        self,
        ref: VideoRef,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> VideoMeta:
        """Читает метаданные ролика. Бросает доменную ошибку, если он не подходит."""
        command = [
            self._settings.binary,
            *self._common_args(),
            "--dump-single-json",
            "--skip-download",
            ref.url,
        ]

        log.debug("ytdlp_probe", video=str(ref), cmd=format_command(command))

        try:
            result = await run_process(
                command,
                timeout=self._settings.metadata_timeout_sec,
                cancel_event=cancel_event,
                output_limit=8 * 1024 * 1024,  # -J по плейлисту бывает большим
            )
        except ProcNotFound as exc:
            raise ToolMissing(self._settings.binary, str(exc)) from exc
        except ProcCancelled as exc:
            raise JobCancelled() from exc
        except ProcTimeout as exc:
            raise DownloadFailed(
                f"yt-dlp не ответил за {self._settings.metadata_timeout_sec:.0f} с"
            ) from exc

        if not result.ok:
            known = classify_ytdlp_output(result.combined_tail(4000))
            if known is not None:
                raise known
            raise VideoUnavailable(result.combined_tail())

        try:
            payload: Any = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise VideoUnavailable(f"yt-dlp вернул неразбираемый JSON: {exc}") from exc

        if isinstance(payload, dict) and payload.get("_type") == "playlist":
            entries = payload.get("entries") or []
            if not entries:
                raise VideoUnavailable("по ссылке нет ни одного видео")
            payload = entries[0]

        meta = VideoMeta(
            video_id=str(payload.get("id") or ref.video_id),
            title=str(payload.get("title") or "video").strip(),
            duration_sec=_coerce_duration(payload.get("duration")),
            language=(payload.get("language") or None),
            is_live=bool(payload.get("is_live")),
            was_live=bool(payload.get("was_live")),
            uploader=payload.get("uploader") or payload.get("channel"),
            height=_coerce_int(payload.get("height")),
            filesize_approx=_coerce_int(payload.get("filesize_approx")),
            extractor=payload.get("extractor_key") or payload.get("extractor"),
        )

        self._validate(meta)

        log.info(
            "ytdlp_meta",
            video=str(ref),
            title=meta.title[:80],
            duration=meta.pretty_duration,
            language=meta.language,
            extractor=meta.extractor,
        )
        return meta

    def _validate(self, meta: VideoMeta) -> None:
        if meta.is_live:
            raise VideoIsLive(f"{meta.video_id} is live")

        limit_min = self._settings.max_duration_minutes
        if limit_min > 0 and meta.duration_sec and meta.duration_min > limit_min:
            raise VideoTooLong(meta.duration_min, limit_min)

        limit_bytes = self._settings.max_filesize_bytes
        if limit_bytes > 0 and meta.filesize_approx and meta.filesize_approx > limit_bytes:
            raise VideoTooBig(meta.filesize_approx, limit_bytes)

    # -- скачивание --------------------------------------------------------- #

    async def download(
        self,
        ref: VideoRef,
        target_dir: Path,
        *,
        max_height: int,
        max_bytes: int = 0,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback = None,
    ) -> Path:
        """Скачивает видео в ``target_dir`` и возвращает путь к файлу.

        :param max_bytes: потолок размера файла. Передаётся в yt-dlp как
            ``--max-filesize``: он проверит размер по метаданным и откажется
            качать заведомо неподъёмное, не начиная загрузку и не заняв
            ни байта. 0 — без ограничения.
        """
        settings = self._settings
        target_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(target_dir / "source.%(ext)s")

        command = [
            settings.binary,
            *self._common_args(),
            "--format",
            settings.format_template.format(height=max_height),
            "--merge-output-format",
            settings.merge_output_format,
            "--output",
            output_template,
            "--concurrent-fragments",
            str(settings.concurrent_fragments),
            "--newline",
            "--progress",
            "--progress-template",
            _PROGRESS_TEMPLATE,
            "--no-part",
        ]
        if settings.rate_limit:
            command += ["--limit-rate", settings.rate_limit]

        effective_limit = _smallest_positive(max_bytes, settings.max_filesize_bytes)
        if effective_limit > 0:
            command += ["--max-filesize", str(effective_limit)]

        command.extend(settings.extra_args)
        command.append(ref.url)

        log.info(
            "ytdlp_download_start",
            video=str(ref),
            height=max_height,
            cmd=format_command(command),
        )

        reporter = _ProgressReporter(progress)

        try:
            result = await run_process(
                command,
                timeout=settings.download_timeout_sec,
                cancel_event=cancel_event,
                on_stdout=reporter.feed,
                output_limit=256 * 1024,
                nice_level=self._priority.nice_level,
                idle_io=self._priority.idle_io,
            )
        except ProcNotFound as exc:
            raise ToolMissing(settings.binary, str(exc)) from exc
        except ProcCancelled as exc:
            raise JobCancelled() from exc
        except ProcTimeout as exc:
            raise DownloadFailed(
                f"скачивание не уложилось в {settings.download_timeout_sec:.0f} с. "
                "Увеличь ytdlp.download_timeout_sec в конфиге."
            ) from exc
        except ProcError as exc:
            raise DownloadFailed(str(exc)) from exc

        if not result.ok:
            tail = result.combined_tail(4000)
            known = classify_ytdlp_output(tail)
            if known is not None:
                raise known
            raise DownloadFailed(tail)

        combined = result.combined_tail(4000).lower()
        oversized = "larger than max-filesize" in combined or "file is larger than" in combined

        downloaded = _pick_downloaded(target_dir)

        if oversized or (downloaded is None and effective_limit > 0):
            # yt-dlp отказался качать сам, до начала загрузки — ровно то
            # поведение, ради которого мы передаём --max-filesize.
            if max_bytes > 0 and effective_limit == max_bytes:
                # Ограничение пришло от контроля свободного места.
                raise NotEnoughSpace(
                    need_bytes=effective_limit,
                    free_bytes=effective_limit,
                    detail="yt-dlp: файл больше --max-filesize",
                )
            raise VideoTooBig(effective_limit, effective_limit)

        if downloaded is None:
            raise DownloadFailed(
                "yt-dlp отработал без ошибки, но файла в рабочем каталоге нет. "
                f"Вывод: {result.combined_tail(1000)}"
            )

        size = downloaded.stat().st_size
        if size == 0:
            raise DownloadFailed("скачанный файл пуст")

        log.info(
            "ytdlp_download_done",
            path=downloaded.name,
            size_mb=round(size / 1024**2, 1),
            duration_sec=round(result.duration, 1),
        )
        return downloaded


class _ProgressReporter:
    """Разбирает строки --progress-template и зовёт колбэк не чаще раза в 3 с."""

    __slots__ = ("_callback", "_last")

    def __init__(self, callback: ProgressCallback) -> None:
        self._callback = callback
        self._last = 0.0

    def feed(self, line: str) -> Awaitable[None] | None:
        if self._callback is None:
            return None
        match = _PROGRESS_RE.match(line.strip())
        if match is None:
            return None

        downloaded = _to_float(match.group(1))
        total = _to_float(match.group(2)) or _to_float(match.group(3))
        speed = _to_float(match.group(4))
        eta = _to_float(match.group(5))

        moment = time.monotonic()
        if moment - self._last < 3.0:
            return None
        self._last = moment

        parts: list[str] = []
        if downloaded is not None and total:
            parts.append(f"{downloaded * 100 / total:.0f}%")
            parts.append(f"{downloaded / 1024**2:.0f} из {total / 1024**2:.0f} МБ")
        elif downloaded is not None:
            parts.append(f"{downloaded / 1024**2:.0f} МБ")
        if speed:
            parts.append(f"{speed / 1024**2:.1f} МБ/с")
        if eta:
            parts.append(f"осталось ~{int(eta // 60)}:{int(eta % 60):02d}")

        if not parts:
            return None
        return self._callback("качаю видео: " + ", ".join(parts))


def _smallest_positive(*values: int) -> int:
    """Наименьшее из положительных значений; 0 — если все нули (без лимита)."""
    positive = [value for value in values if value and value > 0]
    return min(positive) if positive else 0


def _coerce_duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_downloaded(directory: Path) -> Path | None:
    """Находит скачанный файл: имя фиксировано, расширение зависит от формата."""
    candidates = [
        entry
        for entry in directory.glob("source.*")
        if entry.is_file() and not entry.name.endswith((".part", ".ytdl", ".temp"))
    ]
    if not candidates:
        candidates = [entry for entry in directory.iterdir() if entry.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.stat().st_size)
