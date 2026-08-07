"""Работа с ffmpeg/ffprobe: анализ дорожек и склейка результата.

Основной фильтр смешивания::

    [0:a]volume=0.10[orig];
    [1:a]volume=1.0[ru];
    [orig][ru]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];
    [mix]dynaudnorm=f=250:g=15:p=0.9:m=10[aout]

Два неочевидных места:

* ``normalize=0`` в ``amix`` обязателен. По умолчанию amix делит громкость на
  число входов, то есть тихо становится всё, а настройка ``original_volume``
  перестаёт что-либо значить: 10% от уже поделенного пополам — это не 10%.
* ``duration=first`` вместо ``longest``. Русская дорожка от Яндекса регулярно
  оказывается на несколько секунд длиннее или короче исходника; привязка к
  первому входу (видео) не даёт результату вырасти и не оставляет чёрный хвост.

Видеопоток всегда копируется (``-c:v copy``). Перекодирование включается
только если ffmpeg отказался класть исходный кодек в MP4 — определяется это
не по таблице кодеков (она устаревает), а по факту падения первой попытки.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import FfmpegSettings, PrioritySettings
from app.logging_setup import get_logger
from app.pipeline.errors import (
    DiskFull,
    JobCancelled,
    MuxFailed,
    ToolMissing,
)
from app.utils.proc import (
    ProcCancelled,
    ProcError,
    ProcNotFound,
    ProcTimeout,
    format_command,
    run_process,
)

__all__ = ["MediaInfo", "FfmpegClient"]

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]] | None

#: ffmpeg с -progress pipe:1 печатает "ключ=значение" построчно.
_PROGRESS_LINE = re.compile(r"^(out_time_ms|out_time_us|out_time|speed|progress)=(.*)$")

#: Признаки того, что дело именно в несовместимости кодека с контейнером.
_INCOMPATIBLE_MARKERS = (
    "could not find tag for codec",
    "incorrect codec parameters",
    "only version 3 or 4 supported",
    "muxer does not support",
    "track 1: could not find tag",
    "unsupported codec",
    "codec not currently supported in container",
)

_DISK_FULL_MARKERS = ("no space left on device", "disk quota exceeded")


@dataclass(slots=True)
class MediaInfo:
    """Сведения о файле, полученные ffprobe."""

    path: Path
    duration_sec: float | None
    has_video: bool
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    size_bytes: int

    @property
    def pretty_duration(self) -> str:
        total = int(self.duration_sec or 0)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


class FfmpegClient:
    """Тонкая обёртка: ffprobe, склейка видео, подготовка MP3."""

    __slots__ = ("_settings", "_priority")

    def __init__(
        self, settings: FfmpegSettings, priority: PrioritySettings | None = None
    ) -> None:
        self._settings = settings
        self._priority = priority or PrioritySettings()

    # -- анализ ------------------------------------------------------------- #

    async def probe(
        self, path: Path, *, cancel_event: asyncio.Event | None = None
    ) -> MediaInfo:
        """Читает характеристики файла через ffprobe."""
        command = [
            self._settings.ffprobe_binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            result = await run_process(
                command,
                timeout=self._settings.probe_timeout_sec,
                cancel_event=cancel_event,
                log_command=False,
            )
        except ProcNotFound as exc:
            raise ToolMissing(self._settings.ffprobe_binary, str(exc)) from exc
        except ProcCancelled as exc:
            raise JobCancelled() from exc
        except (ProcTimeout, ProcError) as exc:
            raise MuxFailed(f"ffprobe не смог прочитать {path.name}: {exc}") from exc

        if not result.ok:
            raise MuxFailed(f"ffprobe вернул ошибку для {path.name}: {result.combined_tail()}")

        try:
            payload: dict[str, Any] = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MuxFailed(f"ffprobe вернул неразбираемый JSON: {exc}") from exc

        streams = payload.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        container = payload.get("format") or {}

        duration = _to_float(container.get("duration"))
        if duration is None and video is not None:
            duration = _to_float(video.get("duration"))
        if duration is None and audio is not None:
            duration = _to_float(audio.get("duration"))

        try:
            size = int(container.get("size") or path.stat().st_size)
        except (OSError, TypeError, ValueError):
            size = 0

        info = MediaInfo(
            path=path,
            duration_sec=duration,
            has_video=video is not None,
            has_audio=audio is not None,
            video_codec=(video or {}).get("codec_name"),
            audio_codec=(audio or {}).get("codec_name"),
            width=_to_int((video or {}).get("width")),
            height=_to_int((video or {}).get("height")),
            size_bytes=size,
        )
        log.debug(
            "ffprobe",
            file=path.name,
            duration=info.pretty_duration,
            v=info.video_codec,
            a=info.audio_codec,
            size_mb=round(size / 1024**2, 1),
        )
        return info

    # -- склейка ------------------------------------------------------------ #

    def _build_filter(self, *, mix_original: bool, original_has_audio: bool) -> str:
        """Собирает filter_complex под нужный режим."""
        settings = self._settings
        translation_volume = settings.translation_volume

        use_original = mix_original and original_has_audio and settings.original_volume > 0

        if use_original:
            chain = (
                f"[0:a]volume={settings.original_volume:.4f}[orig];"
                f"[1:a]volume={translation_volume:.4f}[ru];"
                "[orig][ru]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]"
            )
            last = "[mix]"
        else:
            chain = f"[1:a]volume={translation_volume:.4f}[mix]"
            last = "[mix]"

        if settings.dynaudnorm:
            chain += f";{last}{settings.dynaudnorm_filter}[aout]"
            last = "[aout]"

        # Метка выхода всегда должна называться [aout] — на неё ссылается -map.
        if last != "[aout]":
            chain += f";{last}anull[aout]"
        return chain

    def _build_mux_command(
        self,
        *,
        video: Path,
        audio: Path,
        target: Path,
        mix_original: bool,
        original_has_audio: bool,
        transcode_video: bool,
    ) -> list[str]:
        settings = self._settings
        command = [
            settings.binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-progress",
            "pipe:1",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-filter_complex",
            self._build_filter(
                mix_original=mix_original, original_has_audio=original_has_audio
            ),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
        ]

        if transcode_video:
            command += list(settings.video_transcode_args)
        else:
            command += ["-c:v", "copy"]

        command += [
            "-c:a",
            settings.audio_codec,
            "-b:a",
            settings.audio_bitrate,
            "-ac",
            str(settings.audio_channels),
            "-movflags",
            "+faststart",
            "-shortest",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
        ]
        if settings.threads > 0:
            command += ["-threads", str(settings.threads)]

        command.append(str(target))
        return command

    async def mux(
        self,
        *,
        video: Path,
        audio: Path,
        target: Path,
        mix_original: bool,
        video_info: MediaInfo | None = None,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback = None,
    ) -> Path:
        """Склеивает видео с русской дорожкой.

        :param mix_original: True — подмешать приглушённый оригинал;
                             False — оставить только перевод.
        :returns: путь к готовому MP4.
        """
        if video_info is None:
            video_info = await self.probe(video, cancel_event=cancel_event)

        total_duration = video_info.duration_sec
        target.parent.mkdir(parents=True, exist_ok=True)

        attempts: list[bool] = [False]  # сначала пробуем без перекодирования
        if self._settings.allow_video_transcode:
            attempts.append(True)

        last_output = ""
        for transcode in attempts:
            if transcode:
                log.warning(
                    "ffmpeg_fallback_transcode",
                    codec=video_info.video_codec,
                    reason="copy не прошёл, перекодирую видео",
                )
                if progress is not None:
                    await progress(
                        "исходный кодек не кладётся в MP4 без изменений — "
                        "перекодирую видео, это заметно дольше"
                    )

            command = self._build_mux_command(
                video=video,
                audio=audio,
                target=target,
                mix_original=mix_original,
                original_has_audio=video_info.has_audio,
                transcode_video=transcode,
            )
            timeout = (
                self._settings.transcode_timeout_sec
                if transcode
                else self._settings.mux_timeout_sec
            )

            log.info(
                "ffmpeg_mux_start",
                transcode=transcode,
                mix_original=mix_original,
                cmd=format_command(command),
            )

            reporter = _MuxProgress(progress, total_duration, transcoding=transcode)

            try:
                result = await run_process(
                    command,
                    timeout=timeout,
                    cancel_event=cancel_event,
                    on_stdout=reporter.feed,
                    output_limit=128 * 1024,
                    # Склейка — самый прожорливый шаг: на одном ядре ffmpeg
                    # занимает его целиком. Уступаем процессор и диск всему,
                    # что работает на этой же машине.
                    nice_level=self._priority.nice_level,
                    idle_io=self._priority.idle_io,
                )
            except ProcNotFound as exc:
                raise ToolMissing(self._settings.binary, str(exc)) from exc
            except ProcCancelled as exc:
                target.unlink(missing_ok=True)
                raise JobCancelled() from exc
            except ProcTimeout as exc:
                target.unlink(missing_ok=True)
                raise MuxFailed(
                    f"склейка не уложилась в {timeout:.0f} с. Увеличь "
                    f"ffmpeg.{'transcode' if transcode else 'mux'}_timeout_sec."
                ) from exc
            except ProcError as exc:
                target.unlink(missing_ok=True)
                raise MuxFailed(str(exc)) from exc

            if result.ok and target.is_file() and target.stat().st_size > 0:
                log.info(
                    "ffmpeg_mux_done",
                    size_mb=round(target.stat().st_size / 1024**2, 1),
                    seconds=round(result.duration, 1),
                    transcode=transcode,
                )
                return target

            last_output = result.combined_tail(3000)
            lowered = last_output.lower()
            target.unlink(missing_ok=True)

            if any(marker in lowered for marker in _DISK_FULL_MARKERS):
                raise DiskFull(last_output)

            if not transcode and any(
                marker in lowered for marker in _INCOMPATIBLE_MARKERS
            ):
                continue  # переходим ко второй попытке с перекодированием

            if not transcode and self._settings.allow_video_transcode:
                # Причина неочевидна — всё равно дадим второй попытке шанс.
                continue

            break

        raise MuxFailed(last_output or "ffmpeg завершился с ошибкой без вывода")

    # -- аудио -------------------------------------------------------------- #

    async def prepare_mp3(
        self,
        source: Path,
        target: Path,
        *,
        title: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> Path:
        """Готовит MP3 для режима /audio.

        Если Яндекс уже отдал MP3, поток копируется без перекодирования —
        добавляются только теги. Иначе дорожка кодируется в MP3.
        """
        info = await self.probe(source, cancel_event=cancel_event)
        already_mp3 = (info.audio_codec or "").lower() == "mp3"

        command = [
            self._settings.binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
        ]
        if already_mp3:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "libmp3lame", "-b:a", self._settings.audio_bitrate]
        if title:
            command += ["-metadata", f"title={title[:200]}"]
        command += ["-write_xing", "1", str(target)]

        try:
            result = await run_process(
                command,
                timeout=self._settings.mux_timeout_sec,
                cancel_event=cancel_event,
                nice_level=self._priority.nice_level,
                idle_io=self._priority.idle_io,
            )
        except ProcNotFound as exc:
            raise ToolMissing(self._settings.binary, str(exc)) from exc
        except ProcCancelled as exc:
            raise JobCancelled() from exc
        except (ProcTimeout, ProcError) as exc:
            raise MuxFailed(f"не удалось подготовить MP3: {exc}") from exc

        if not result.ok or not target.is_file() or target.stat().st_size == 0:
            # Перекодирование в MP3 — последний шанс, если copy не прошёл.
            if already_mp3:
                log.warning("mp3_copy_failed_reencoding")
                return await self._reencode_mp3(source, target, title, cancel_event)
            raise MuxFailed(result.combined_tail())

        return target

    async def _reencode_mp3(
        self,
        source: Path,
        target: Path,
        title: str | None,
        cancel_event: asyncio.Event | None,
    ) -> Path:
        command = [
            self._settings.binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            self._settings.audio_bitrate,
        ]
        if title:
            command += ["-metadata", f"title={title[:200]}"]
        command.append(str(target))

        result = await run_process(
            command,
            timeout=self._settings.transcode_timeout_sec,
            cancel_event=cancel_event,
            nice_level=self._priority.nice_level,
            idle_io=self._priority.idle_io,
        )
        if not result.ok or not target.is_file():
            raise MuxFailed(result.combined_tail())
        return target


class _MuxProgress:
    """Разбирает вывод ``-progress pipe:1`` и пересчитывает его в проценты."""

    __slots__ = ("_callback", "_total", "_last", "_transcoding")

    def __init__(
        self, callback: ProgressCallback, total_sec: float | None, *, transcoding: bool
    ) -> None:
        self._callback = callback
        self._total = total_sec if total_sec and total_sec > 0 else None
        self._last = 0.0
        self._transcoding = transcoding

    def feed(self, line: str) -> Awaitable[None] | None:
        if self._callback is None:
            return None
        match = _PROGRESS_LINE.match(line.strip())
        if match is None:
            return None
        key, value = match.group(1), match.group(2).strip()
        if key not in {"out_time_ms", "out_time_us"}:
            return None

        microseconds = _to_float(value)
        if microseconds is None:
            return None
        # out_time_ms в ffmpeg на самом деле микросекунды — исторический баг,
        # который так и не стали чинить ради обратной совместимости.
        seconds = microseconds / 1_000_000.0

        moment = time.monotonic()
        if moment - self._last < 4.0:
            return None
        self._last = moment

        label = "перекодирую" if self._transcoding else "склеиваю"
        if self._total:
            percent = min(100.0, seconds * 100.0 / self._total)
            return self._callback(f"{label}: {percent:.0f}%")
        return self._callback(f"{label}: обработано {seconds / 60:.1f} мин")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # отсекаем NaN


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
