"""Обёртка над vot-cli-live (fantomcheg): получение русской дорожки и субтитров от Яндекса.

Используется только форк fantomcheg/vot-cli-live 1.7.5. Оригинальный
foswly/vot-cli 2.0.1 неработоспособен: его промежуточный бэкенд на
vot.toil.cc больше не резолвится.

Ключевые наблюдаемые свойства форка:
* Вывод --json — МАССИВ объектов [{url, platform, videoTitle, success, audioUrl, error, voiceType}].
* В режиме субтитров перед основным JSON печатается строка «Subtitles response (URL): <JSON>».
* Код возврата всегда 0, даже при явном отказе. На него нельзя полагаться.
* Для закешированных роликов ответ приходит мгновенно с валидной дорожкой.
* Для незакешированных — success:true и audioUrl отдаются ДО готовности перевода,
  по ссылке лежит заглушка. Её нужно опознать через ffprobe и отправиться на повторный опрос.
* Яндекс переводит асинхронно: первый запрос запускает перевод, готовность — через минуты.
  Нужен опрос с экспоненциальной задержкой, а не фиксированный sleep.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.config import PrioritySettings, VotSettings
from app.logging_setup import get_logger
from app.pipeline.errors import (
    DownloadFailed,
    JobCancelled,
    ToolMissing,
    TranslationTimeout,
    TranslationUnavailable,
)
from app.utils.proc import (
    ProcCancelled,
    ProcError,
    ProcNotFound,
    ProcResult,
    ProcTimeout,
    format_command,
    run_process,
)
from app.utils.retry import BackoffSchedule, CancelledByUser, sleep_with_cancel
from app.utils.urls import VideoRef

__all__ = ["VotClient", "VotArtifact"]

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]] | None

_AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav")
_SUBS_SUFFIXES = (".srt", ".vtt", ".json")

# Ошибки, которые означают «это видео нельзя перевести» — повторять бессмысленно.
_FATAL_ERROR_PATTERNS = (
    re.compile(r"translation\s+(?:is\s+)?not\s+available\s+for\s+this\s+video", re.IGNORECASE),
    re.compile(r"unsupported\s+(?:site|platform|url)", re.IGNORECASE),
    re.compile(r"video\s+(?:is\s+)?(?:unavailable|not\s+found|private|deleted)", re.IGNORECASE),
    re.compile(r"no\s+(?:subtitles|субтитр)", re.IGNORECASE),
)

# Ошибки, которые означают «сеть/сервер временно недоступен» — надо повторить.
_RETRYABLE_ERROR_PATTERNS = (
    re.compile(r"timed\s*out|etimedout|econnrefused|econnreset|enotfound|eai_again", re.IGNORECASE),
    re.compile(r"failed\s+to\s+(?:request|create)\s+session", re.IGNORECASE),
    re.compile(r"(?:network|connection)\s+(?:error|refused|reset)", re.IGNORECASE),
    re.compile(r"(?:5\d\d|server\s+error)", re.IGNORECASE),
)

# Максимальное число устойчивых отказов подряд, после которых сдаёмся.
_MAX_HARD_FAILURES = 4

# Допустимое расхождение длительностей: дорожка от Яндекса может быть на 20%
# короче или длиннее оригинала (реклама, заставки, отличия в темпе речи).
_DURATION_TOLERANCE = 0.25


def _short(text: str, limit: int = 300) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) > limit:
        cleaned = cleaned[-limit:]
    return html.escape(cleaned, quote=False)


@dataclass(slots=True)
class VotArtifact:
    """Результат работы vot-cli-live: файл на диске плюс диагностика."""

    path: Path
    kind: str  # "audio" | "subtitles"
    source_lang: str
    lively_voice: bool
    attempts: int
    waited_sec: float

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


class VotClient:
    """Запуск vot-cli-live с опросом до фактической готовности перевода."""

    __slots__ = ("_settings", "_http", "_owns_http", "_priority", "_binary")

    def __init__(
        self,
        settings: VotSettings,
        http: httpx.AsyncClient | None = None,
        priority: PrioritySettings | None = None,
    ) -> None:
        self._settings = settings
        self._priority = priority or PrioritySettings()
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) video_tg/1.0"},
        )
        self._binary = settings.binary

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- публичное API ----------------------------------------------------- #

    async def get_audio(
        self,
        ref: VideoRef,
        workdir: Path,
        *,
        source_lang: str | None = None,
        expected_duration_sec: float | None = None,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback = None,
    ) -> VotArtifact:
        return await self._obtain(
            ref,
            workdir,
            kind="audio",
            source_lang=source_lang,
            expected_duration_sec=expected_duration_sec,
            cancel_event=cancel_event,
            progress=progress,
        )

    async def get_subtitles(
        self,
        ref: VideoRef,
        workdir: Path,
        *,
        source_lang: str | None = None,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback = None,
    ) -> VotArtifact:
        return await self._obtain(
            ref,
            workdir,
            kind="subtitles",
            source_lang=source_lang,
            expected_duration_sec=None,
            cancel_event=cancel_event,
            progress=progress,
        )

    # -- основная логика ---------------------------------------------------- #

    def _resolve_source_lang(self, hint: str | None) -> str:
        configured = (self._settings.source_lang or "auto").strip().lower()
        if configured != "auto":
            return configured
        if hint:
            return hint.split("-")[0].strip().lower()
        return self._settings.source_lang_fallback

    def _build_command(
        self,
        ref: VideoRef,
        *,
        kind: str,
        source_lang: str,
        outdir: Path,
        live_voices: bool | None = None,
    ) -> list[str]:
        settings = self._settings
        # live_voices передаётся явно, потому что по ходу опроса бот может
        # откатиться с живых голосов на обычный синтез, не трогая конфиг.
        use_live = settings.lively_voice if live_voices is None else live_voices
        cmd: list[str] = [
            self._binary,
            "--json",
            f"--lang={source_lang}",
            f"--reslang={settings.target_lang}",
            "--voice-style=" + ("live" if use_live else "tts"),
        ]

        if use_live and settings.force_live_voices:
            cmd.append("--force-live-voices")

        if settings.proxy:
            cmd.append(f"--proxy={settings.proxy}")

        if kind == "subtitles":
            cmd.append("--subs")
            if settings.subs_format == "srt":
                cmd.append("--subs-srt")

        cmd.append(f"--output={outdir}")
        cmd.extend(settings.extra_args)
        cmd.append(ref.url)
        return cmd

    async def _obtain(
        self,
        ref: VideoRef,
        workdir: Path,
        *,
        kind: str,
        source_lang: str | None,
        expected_duration_sec: float | None,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> VotArtifact:
        settings = self._settings
        resolved_lang = self._resolve_source_lang(source_lang)

        outdir = workdir / ("vot-subs" if kind == "subtitles" else "vot-audio")
        outdir.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        schedule = BackoffSchedule(
            start=settings.backoff_start_sec,
            factor=settings.backoff_factor,
            maximum=settings.backoff_max_sec,
            jitter=settings.backoff_jitter_sec,
            total_budget=settings.total_timeout_sec,
        )

        attempt = 0
        hard_failures = 0
        # Может смениться по ходу опроса: если для ролика нет живых голосов,
        # откатываемся на обычный синтез вместо того, чтобы падать.
        use_live_voices = settings.lively_voice

        while True:
            attempt += 1
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled()

            command = self._build_command(
                ref,
                kind=kind,
                source_lang=resolved_lang,
                outdir=outdir,
                live_voices=use_live_voices,
            )

            _clear_directory(outdir)

            log.info(
                "vot_attempt",
                attempt=attempt,
                kind=kind,
                lang=resolved_lang,
                video=str(ref),
                cmd=format_command(command),
            )

            result: ProcResult | None = None
            pending_reason = "перевод ещё готовится"

            try:
                result = await run_process(
                    command,
                    timeout=settings.attempt_timeout_sec,
                    cancel_event=cancel_event,
                    output_limit=256 * 1024,
                    nice_level=self._priority.nice_level,
                    idle_io=self._priority.idle_io,
                )
            except ProcNotFound as exc:
                raise ToolMissing(settings.binary, str(exc)) from exc
            except ProcCancelled as exc:
                raise JobCancelled() from exc
            except ProcTimeout:
                log.info("vot_attempt_timeout", attempt=attempt, kind=kind)
                pending_reason = "Яндекс всё ещё переводит"
                combined = ""
            except ProcError as exc:
                raise TranslationUnavailable(str(exc)) from exc
            else:
                combined = "\n".join(
                    part for part in (result.stdout, result.stderr) if part
                )

                # 1. Пробуем извлечь файл из вывода.
                artifact = await self._extract_artifact(
                    result,
                    combined,
                    kind=kind,
                    outdir=outdir,
                    cancel_event=cancel_event,
                    progress=progress,
                )

                # 2. Парсим JSON, чтобы узнать verdict бэкенда.
                payload = _extract_main_json(combined)
                backend_success = _is_backend_success(payload)
                backend_error = _get_backend_error(payload)

                if artifact is not None and backend_success is not False:
                    # 3. Валидация: реально ли файл готов или это заглушка.
                    problem = _validate_artifact(artifact, kind, expected_duration_sec)
                    if problem is not None:
                        log.info(
                            "vot_artifact_rejected",
                            attempt=attempt,
                            kind=kind,
                            reason=problem,
                            size=artifact.stat().st_size if artifact.is_file() else 0,
                        )
                        artifact.unlink(missing_ok=True)
                        pending_reason = "перевод ещё готовится"
                    else:
                        waited = time.monotonic() - started
                        log.info(
                            "vot_done",
                            kind=kind,
                            attempts=attempt,
                            waited_sec=round(waited, 1),
                            size=artifact.stat().st_size,
                        )
                        return VotArtifact(
                            path=artifact,
                            kind=kind,
                            source_lang=resolved_lang,
                            lively_voice=use_live_voices,
                            attempts=attempt,
                            waited_sec=waited,
                        )

                # 4. Классификация: ждать или падать.
                transient = False
                if backend_success is False and backend_error:
                    if _is_fatal_error(backend_error):
                        # Живые голоса есть не у всех роликов, и форк сообщает
                        # об их отсутствии тем же «Translation not available»,
                        # что и о полной невозможности перевода. Отличить одно
                        # от другого можно только попыткой: откатываемся на
                        # обычный синтез, прежде чем объявлять ролик
                        # непереводимым. Голоса — предпочтение, а не условие.
                        if use_live_voices:
                            log.warning(
                                "vot_live_voices_unavailable",
                                attempt=attempt,
                                kind=kind,
                                error=backend_error,
                                hint="повторяю с обычным синтезом",
                            )
                            use_live_voices = False
                            if progress is not None:
                                await progress(
                                    "живых голосов для этого ролика нет — "
                                    "перехожу на обычный синтез"
                                )
                            continue

                        log.error(
                            "vot_backend_refused",
                            attempt=attempt,
                            kind=kind,
                            error=backend_error,
                        )
                        raise TranslationUnavailable(
                            backend_error,
                            user_message=(
                                f"Яндекс не может перевести это видео: {backend_error}"
                            ),
                        )
                    if _is_retryable_error(backend_error):
                        log.info(
                            "vot_transient_error",
                            attempt=attempt,
                            kind=kind,
                            error=backend_error,
                        )
                        pending_reason = "Яндекс временно недоступен"
                        transient = True
                    else:
                        log.warning(
                            "vot_unknown_error",
                            attempt=attempt,
                            kind=kind,
                            error=backend_error,
                        )

                # pending = «перевод ещё готовится, надо подождать»
                is_pending = transient or (
                    (artifact is None and backend_success is True)
                    or (artifact is None and backend_success is None
                        and _looks_like_translation_pending(combined))
                    or (artifact is not None and backend_success is not False)
                )

                if not is_pending:
                    hard_failures += 1
                    if hard_failures >= _MAX_HARD_FAILURES:
                        log.error(
                            "vot_giving_up",
                            attempts=attempt,
                            hard_failures=hard_failures,
                            output=combined[-800:],
                        )
                        raise TranslationUnavailable(
                            combined[-800:],
                            user_message=(
                                f"Яндекс отказался переводить — "
                                f"{hard_failures} попыток подряд без результата.\n\n"
                                "Ответ утилиты:\n"
                                f"<code>{_short(combined)}</code>"
                            ),
                        )

                log.info(
                    "vot_no_result",
                    attempt=attempt,
                    kind=kind,
                    rc=result.returncode if result else -1,
                    pending=is_pending,
                    output=combined[-500:].strip() or "<пусто>",
                )

                if is_pending:
                    hard_failures = 0

            # 4. Ждём с backoff и пробуем снова.
            if schedule.exhausted:
                waited = time.monotonic() - started
                raise TranslationTimeout(waited, attempt)

            delay = schedule.next_delay()
            if progress is not None:
                await progress(
                    f"{pending_reason} — попытка {attempt}, "
                    f"следующая через {delay:.0f} с"
                )

            try:
                await sleep_with_cancel(delay, cancel_event)
            except CancelledByUser as exc:
                raise JobCancelled() from exc

    # -- извлечение результата ----------------------------------------------- #

    async def _extract_artifact(
        self,
        result: ProcResult,
        combined: str,
        *,
        kind: str,
        outdir: Path,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> Path | None:
        """Извлекает готовый файл из результата вызова.

        Принципиально: НИКОГДА не скачивает audioUrl самостоятельно.
        Если форк не сохранил файл в --output — значит перевод ещё не готов,
        и audioUrl — это ссылка на заглушку Яндекса. Единственный надёжный
        признак готовности — файл, записанный самим fork-ом в каталог.
        """
        suffixes = _SUBS_SUFFIXES if kind == "subtitles" else _AUDIO_SUFFIXES

        found = _newest_file(outdir, suffixes)
        if found is not None:
            return found

        # Файла нет. Для субтитров пробуем скачать (там нет проблемы заглушек).
        if kind == "subtitles":
            payload = _extract_main_json(combined)
            if payload is not None and isinstance(payload, dict):
                url = _find_any_url(payload)
                if url is not None:
                    target = outdir / "downloaded.srt"
                    try:
                        await self._download(
                            url, target,
                            cancel_event=cancel_event,
                            progress=progress,
                        )
                    except (DownloadFailed, JobCancelled):
                        target.unlink(missing_ok=True)
                        return None
                    if target.stat().st_size > 0:
                        return target
                    target.unlink(missing_ok=True)

        # Для аудио: нет файла → перевод ещё не готов. Ждём следующей попытки.
        if kind == "audio":
            payload = _extract_main_json(combined)
            audio_url = None
            if isinstance(payload, dict):
                audio_url = payload.get("audioUrl") or ""
            if audio_url:
                log.info(
                    "vot_no_local_file_audioUrl_present",
                    hint="Yandex returned audioUrl but fork did not save the file "
                         "— translation is not ready yet, will poll",
                )

        return None

    async def _download(
        self,
        url: str,
        target: Path,
        *,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        downloaded = 0
        last_report = 0.0

        try:
            async with self._http.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise JobCancelled()
                        handle.write(chunk)
                        downloaded += len(chunk)
                        moment = time.monotonic()
                        if progress is not None and moment - last_report > 3:
                            last_report = moment
                            if total:
                                await progress(
                                    f"качаю дорожку: {downloaded * 100 // total}% "
                                    f"({downloaded / 1024**2:.1f} из {total / 1024**2:.1f} МБ)"
                                )
                            else:
                                await progress(
                                    f"качаю дорожку: {downloaded / 1024**2:.1f} МБ"
                                )
            temporary.replace(target)
        except httpx.HTTPError as exc:
            raise DownloadFailed(f"не удалось скачать {url}: {exc}") from exc
        except OSError as exc:
            raise DownloadFailed(f"ошибка при сохранении {target}: {exc}") from exc


# --------------------------------------------------------------------------- #
#  Разбор JSON из перемешанного вывода vot-cli-live
# --------------------------------------------------------------------------- #

# Строка с субтитрами в выводе: «Subtitles response (URL): <JSON_ARRAY>»
_SUBS_RESPONSE_RE = re.compile(
    r"Subtitles\s+response\s*\([^)]*\)\s*:\s*(\[.+?\])", re.IGNORECASE | re.DOTALL
)


def _extract_main_json(text: str) -> Any | None:
    """Достаёт основной JSON из вывода — массив результата перевода.

    vot-cli-live печатает JSON-массив `[{...}]`. В режиме субтитров перед ним
    может идти «Subtitles response (URL): [...]» — это отдельный блок.
    Берём последний найденный JSON-массив (после строки субтитров).
    """
    if not text:
        return None

    # Ищем все JSON-массивы: [{...}]
    decoder = json.JSONDecoder()
    candidates: list[dict] = []

    for idx, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict):
            candidates.append(value[0])

    if not candidates:
        return None

    # Возвращаем последний валидный (основной ответ, не subtitles вложенный).
    return candidates[-1]


def _find_any_url(payload: dict) -> str | None:
    """Рекурсивно ищет первую HTTP-ссылку в структуре JSON."""
    for _, value in _walk_dict(payload):
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def _walk_dict(node: Any) -> list[tuple[str, Any]]:
    """Плоский список (ключ, значение) с рекурсивным обходом."""
    result: list[tuple[str, Any]] = []
    _walk_recursive(node, result)
    return result


def _walk_recursive(node: Any, acc: list[tuple[str, Any]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            acc.append((str(key).lower(), value))
            _walk_recursive(value, acc)
    elif isinstance(node, list):
        for item in node:
            _walk_recursive(item, acc)


# --------------------------------------------------------------------------- #
#  Валидация артефакта через ffprobe
# --------------------------------------------------------------------------- #

def _validate_artifact(
    path: Path, kind: str, expected_duration_sec: float | None
) -> str | None:
    """Проверяет, что файл — настоящий аудио/субтитры, а не заглушка.

    :returns: причину отбраковки или None, если файл валиден.
    """
    if kind == "subtitles":
        return _validate_subtitles(path)

    if kind == "audio":
        return _validate_audio_ffprobe(path, expected_duration_sec)

    return None


def _validate_audio_ffprobe(
    path: Path, expected_duration_sec: float | None
) -> str | None:
    """Проверяет аудиофайл через ffprobe.

    Ключевое отличие от проверки по сигнатуре байт: заглушка, которую Яндекс
    отдаёт до готовности перевода, может начинаться с корректного MP3-заголовка,
    но иметь гротескно малую длительность или вовсе не декодироваться.
    ffprobe надёжнее.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"файл недоступен: {exc}"

    if size < 4096:
        return f"слишком маленький файл ({size} Б)"

    # Быстрая проверка: настоящие аудиоданные не начинаются с <, {, [ и т.п.
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        return f"не удалось прочитать: {exc}"

    if head and head[0:1] in (b"<", b"{", b"["):
        return f"похоже на HTML/JSON, а не аудио (первые байты: {head!r})"

    # ffprobe проверка
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            text=True,
        )
    except FileNotFoundError:
        log.warning("ffprobe not found, falling back to basic checks")
        return _validate_audio_basic(path, expected_duration_sec)
    except subprocess.TimeoutExpired:
        return "ffprobe завис на файле"
    except OSError as exc:
        return f"ffprobe ошибка: {exc}"

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-200:]
        return f"ffprobe не смог прочитать файл: {stderr_tail.strip()}"

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return "ffprobe вернул не-JSON"

    streams = data.get("streams") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not audio_streams:
        if any(s.get("codec_type") == "video" for s in streams):
            return "в файле только видео, но не аудио"
        return "ffprobe не нашёл аудиопотоков"

    # Проверяем sample_rate и channels: у заглушек они нулевые
    first_audio = audio_streams[0]
    if int(first_audio.get("sample_rate", 0) or 0) == 0:
        return "sample_rate=0 (заглушка)"
    if int(first_audio.get("channels", 0) or 0) == 0:
        return "channels=0 (заглушка)"

    # Проверяем длительность
    fmt = data.get("format") or {}
    duration_str = fmt.get("duration")
    if duration_str is not None:
        try:
            actual_duration = float(duration_str)
        except (TypeError, ValueError):
            actual_duration = None
    else:
        # Пробуем из первого аудиопотока
        actual_duration = None
        for stream in audio_streams:
            dur = stream.get("duration")
            if dur is not None:
                try:
                    actual_duration = float(dur)
                except (TypeError, ValueError):
                    pass
                break

    if actual_duration is None:
        # Заглушки из нулевых байт дают 0.0 с, но чаще — просто не имеют
        # поля duration вообще. И то, и другое — признак неготовности.
        return "длительность не определена (заглушка)"

    if actual_duration <= 0.5:
        return f"длительность почти нулевая: {actual_duration:.2f} с"

    if expected_duration_sec and expected_duration_sec > 0:
        diff = abs(actual_duration - expected_duration_sec)
        max_diff = expected_duration_sec * _DURATION_TOLERANCE
        if diff > max_diff and actual_duration < expected_duration_sec * 0.3:
            return (
                f"дорожка значительно короче видео: "
                f"{actual_duration:.0f} с вместо {expected_duration_sec:.0f} с"
            )

    # Проверяем битрейт: заглушки имеют аномально низкий битрейт.
    bitrate_str = fmt.get("bit_rate")
    if bitrate_str is not None and actual_duration is not None and actual_duration > 0:
        try:
            bitrate = int(bitrate_str)
        except (TypeError, ValueError):
            bitrate = None
        if bitrate is not None and bitrate < 8000:  # 8 kbps — ниже речи не бывает
            return f"подозрительно низкий битрейт: {bitrate} бит/с"

    return None


def _validate_audio_basic(
    path: Path, expected_duration_sec: float | None
) -> str | None:
    """Запасная проверка без ffprobe: размер файла относительно длительности."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"файл недоступен: {exc}"

    if size < 4096:
        return f"слишком маленький файл ({size} Б)"

    if expected_duration_sec and expected_duration_sec > 0:
        min_bytes_per_sec = 700
        minimum = int(expected_duration_sec * min_bytes_per_sec)
        if size < minimum:
            return (
                f"дорожка короче ожидаемого: {size / 1024**2:.2f} МБ при "
                f"минимуме {minimum / 1024**2:.2f} МБ на "
                f"{expected_duration_sec / 60:.0f} мин видео"
            )

    return None


def _validate_subtitles(path: Path) -> str | None:
    """Беглая проверка субтитров."""
    try:
        if path.stat().st_size < 16:
            return "пустой файл субтитров"
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError as exc:
        return f"не удалось прочитать: {exc}"

    if path.suffix.lower() == ".json":
        return None if head.lstrip().startswith(("{", "[")) else "это не JSON"

    return None if "-->" in head else "в файле нет таймингов субтитров"


# --------------------------------------------------------------------------- #
#  Вспомогательные: файлы, директории, анализ ошибок
# --------------------------------------------------------------------------- #

def _clear_directory(directory: Path) -> None:
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        return
    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _newest_file(directory: Path, suffixes: tuple[str, ...]) -> Path | None:
    if not directory.is_dir():
        return None
    best: Path | None = None
    best_mtime = -1.0
    for entry in directory.rglob("*"):
        if not entry.is_file() or entry.suffix.lower() not in suffixes:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_size == 0:
            continue
        if stat.st_mtime > best_mtime:
            best, best_mtime = entry, stat.st_mtime
    return best


def _is_backend_success(payload: Any) -> bool | None:
    """Возвращает значение поля success из JSON. None — JSON не найден."""
    if isinstance(payload, dict):
        val = payload.get("success")
        if isinstance(val, bool):
            return val
    return None


def _get_backend_error(payload: Any) -> str | None:
    """Возвращает текст ошибки из JSON. None — нет ошибки."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return None


def _is_fatal_error(error_text: str) -> bool:
    """True, если ошибка означает «это видео нельзя перевести»."""
    for pattern in _FATAL_ERROR_PATTERNS:
        if pattern.search(error_text):
            return True
    return False


def _is_retryable_error(error_text: str) -> bool:
    """True, если ошибка временная (сеть, таймаут) — надо повторить."""
    for pattern in _RETRYABLE_ERROR_PATTERNS:
        if pattern.search(error_text):
            return True
    return False


def _looks_like_translation_pending(text: str) -> bool:
    """True, если вывод похож на «перевод ещё не готов, подожди»."""
    if not text:
        return False
    lowered = text.lower()
    markers = (
        "wait", "pending", "in progress", "not ready", "preparing",
        "translating", "processing", "очеред", "ожида", "готов",
        "переводится", "подожди",
    )
    return any(marker in lowered for marker in markers)
