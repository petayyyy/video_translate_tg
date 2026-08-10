"""Обёртка над vot-cli: получение русской дорожки и субтитров от Яндекса.

Ключевая сложность здесь — асинхронность бэкенда Яндекса. На первый запрос
он отвечает не переводом, а «перевод готовится, зайди через N секунд».
Сам vot-cli это частично прячет (внутри у него рекурсивный опрос раз в 30 с),
но делает это без ограничения по времени и без обратной связи наружу.

Поэтому опрос вынесен на уровень бота:

* каждая попытка запускается со своим таймаутом ``vot.attempt_timeout_sec``;
  по его истечении процесс убивается вместе с группой;
* таймаут попытки трактуется как «перевод ещё не готов», а не как ошибка —
  ровно так же, как явное сообщение бэкенда об ожидании;
* между попытками — экспоненциальная задержка с джиттером, а не фиксированный
  ``sleep``; общий бюджет ограничен ``vot.total_timeout_sec``;
* состояние перевода Яндекс держит у себя и привязывает к ссылке на ролик,
  поэтому убитая на середине попытка ничего не теряет: следующая подхватывает
  ту же задачу, а не начинает заново.

Разбор вывода намеренно терпимый. Формат JSON у vot-cli между версиями менялся,
поэтому вместо жёсткой схемы используется рекурсивный поиск полей со ссылкой
или путём к файлу, а если JSON не разобрался вообще — включается запасной путь
со скачиванием файла самим vot-cli и поиском результата в рабочем каталоге.
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
from typing import Any, Awaitable, Callable, Iterable

import httpx

from app.config import PrioritySettings, VotSettings
from app.logging_setup import get_logger
from app.pipeline.errors import (
    DownloadFailed,
    JobCancelled,
    SubtitlesUnavailable,
    ToolMissing,
    TranslationTimeout,
    TranslationUnavailable,
    classify_vot_output,
    looks_like_translation_pending,
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
from app.utils.retry import BackoffSchedule, CancelledByUser, retry_async, sleep_with_cancel
from app.utils.urls import VideoRef

__all__ = ["VotClient", "VotArtifact"]

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]] | None

#: Расширения, которые может отдать vot-cli. Порядок — приоритет при поиске.
_AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav")
_SUBS_SUFFIXES = (".srt", ".vtt", ".json")

#: Поля JSON, в которых может лежать прямая ссылка на результат.
_URL_KEYS = ("url", "audiourl", "downloadurl", "link", "translationurl", "result")
#: Поля JSON, в которых может лежать путь к уже скачанному файлу.
_PATH_KEYS = ("outputpath", "output", "path", "file", "filepath", "outfile")

_HTTP_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

#: Сколько попыток подряд без признаков «ещё готовится» считать отказом.
#: Считаются только неудачи после того, как исчерпаны запасные пути.
_MAX_HARD_FAILURES = 4


def _short(text: str, limit: int = 300) -> str:
    """Обрезает вывод утилиты для показа пользователю в сообщении."""
    cleaned = " ".join(text.split())
    if len(cleaned) > limit:
        cleaned = cleaned[-limit:]
    return html.escape(cleaned, quote=False)


@dataclass(slots=True)
class VotArtifact:
    """Результат работы vot-cli: файл на диске плюс диагностика."""

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
    """Запуск vot-cli с корректным опросом статуса перевода."""

    __slots__ = ("_settings", "_http", "_owns_http", "_priority")

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
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback = None,
    ) -> VotArtifact:
        """Получает русскую звуковую дорожку. Файл кладётся в ``workdir``."""
        return await self._obtain(
            ref,
            workdir,
            kind="audio",
            source_lang=source_lang,
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
        """Получает субтитры в формате из конфига (по умолчанию SRT)."""
        return await self._obtain(
            ref,
            workdir,
            kind="subtitles",
            source_lang=source_lang,
            cancel_event=cancel_event,
            progress=progress,
        )

    # -- основная логика ---------------------------------------------------- #

    def _resolve_source_lang(self, hint: str | None) -> str:
        """Выбирает значение для --lang.

        ``auto`` не работает вместе с живыми голосами (это прямо сказано в
        релизе vot-cli 2.0.1), поэтому при включённых живых голосах ``auto``
        заменяется на язык, определённый через yt-dlp, а если и его нет —
        на ``vot.source_lang_fallback``.
        """
        configured = (self._settings.source_lang or "auto").strip().lower()
        if configured != "auto":
            return configured
        # Определённый yt-dlp язык предпочитается всегда, а не только при
        # живых голосах: "auto" бэкенд Яндекса принимает не во всех режимах,
        # а реальный код языка — всегда. Хвост локали отбрасывается:
        # yt-dlp отдаёт "en-US", vot-cli ждёт "en".
        if hint:
            return hint.split("-")[0].strip().lower()
        if self._settings.lively_voice:
            return self._settings.source_lang_fallback
        return "auto"

    def _build_command(
        self,
        ref: VideoRef,
        *,
        kind: str,
        source_lang: str,
        preview: bool,
        outdir: Path | None,
    ) -> list[str]:
        settings = self._settings
        command: list[str] = [settings.binary, "--json"]

        if settings.flavor == "foswly":
            # --no-visual намеренно НЕ передаётся. С ним vot-cli печатает
            # только JSON вида {"ok":false,...}, в котором нет ни слова о
            # причине отказа, и диагностировать нечего. Без него рядом с JSON
            # остаётся читаемая строка («Failed to request create session»),
            # по которой ошибка распознаётся и превращается во внятный совет.
            # Разбор JSON из окружающего шума реализован в _extract_json.
            command.append(f"--lang={source_lang}")
            command.append(f"--reslang={settings.target_lang}")
            if settings.lively_voice:
                command.append("--lively-voice")
            if settings.api_token:
                command.append(f"--api-token={settings.api_token}")
            if settings.proxy:
                command.append(f"--proxy={settings.proxy}")
            if settings.vot_host:
                command.append(f"--vot-host={settings.vot_host}")
            if settings.worker_host:
                command.append(f"--worker-host={settings.worker_host}")
            if kind == "subtitles":
                command.append("--subs")
                command.append(f"--subs-format={settings.subs_format}")
            if preview:
                command.append("--preview")
            else:
                assert outdir is not None
                command.append(f"--outdir={outdir}")
                # Имя по ID вместо названия ролика: названия бывают с эмодзи,
                # кавычками и слешами, а результат мы всё равно ищем перебором.
                command.append("--no-title")
        else:  # flavor == "live" (fantomcheg/vot-cli-live)
            command.append(f"--lang={source_lang}")
            command.append(f"--reslang={settings.target_lang}")
            command.append(
                "--voice-style=" + ("live" if settings.lively_voice else "tts")
            )
            if settings.proxy:
                command.append(f"--proxy={settings.proxy}")
            if kind == "subtitles":
                command.append("--subs")
                if settings.subs_format == "srt":
                    command.append("--subs-srt")
            assert outdir is not None
            command.append(f"--output={outdir}")

        command.extend(settings.extra_args)
        command.append(ref.url)
        return command

    async def _obtain(
        self,
        ref: VideoRef,
        workdir: Path,
        *,
        kind: str,
        source_lang: str | None,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> VotArtifact:
        settings = self._settings
        resolved_lang = self._resolve_source_lang(source_lang)

        # У форка нет режима --preview, поэтому там всегда путь со скачиванием.
        use_preview = settings.use_preview and settings.flavor == "foswly"

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

        while True:
            attempt += 1
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled()

            command = self._build_command(
                ref,
                kind=kind,
                source_lang=resolved_lang,
                preview=use_preview,
                outdir=None if use_preview else outdir,
            )

            if not use_preview:
                # Прошлая попытка могла быть убита по таймауту прямо во время
                # скачивания и оставить обрезанный файл. Он непустой, поэтому
                # прошёл бы проверку в _try_extract и был бы отдан как готовый.
                # Чистим каталог перед каждой попыткой.
                _clear_directory(outdir)

            log.info(
                "vot_attempt",
                attempt=attempt,
                kind=kind,
                lang=resolved_lang,
                preview=use_preview,
                video=str(ref),
                cmd=format_command(command),
            )

            pending_reason = "перевод ещё готовится"
            result: ProcResult | None = None

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
            except ProcTimeout as exc:
                # Таймаут попытки — это норма: vot-cli внутри ждёт бэкенд.
                log.info("vot_attempt_timeout", attempt=attempt, kind=kind)
                pending_reason = "Яндекс всё ещё переводит"
                combined = exc.output
            except ProcError as exc:
                raise TranslationUnavailable(str(exc)) from exc
            else:
                combined = "\n".join(part for part in (result.stdout, result.stderr) if part)

                artifact = await self._try_extract(
                    result,
                    combined,
                    kind=kind,
                    outdir=outdir,
                    workdir=workdir,
                    cancel_event=cancel_event,
                    progress=progress,
                )
                if artifact is not None:
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
                        lively_voice=settings.lively_voice,
                        attempts=attempt,
                        waited_sec=waited,
                    )

                # Результата нет. Разбираемся: ждать или падать.
                fatal = classify_vot_output(combined)
                if fatal is not None and not looks_like_translation_pending(combined):
                    if kind == "subtitles" and isinstance(fatal, SubtitlesUnavailable):
                        raise fatal
                    log.info("vot_fatal", kind=kind, error=type(fatal).__name__)
                    raise fatal

                pending = looks_like_translation_pending(combined)

                # Без этой строки диагностировать отказ невозможно: в логе
                # оставалась только строка vot_attempt без единого намёка,
                # почему попытка ничего не дала.
                log.info(
                    "vot_no_result",
                    attempt=attempt,
                    kind=kind,
                    rc=result.returncode,
                    pending=pending,
                    output=combined[-500:].strip() or "<пусто>",
                )

                if not pending:
                    hard_failures += 1

                    # Код возврата у vot-cli недостоверен: при явном отказе он
                    # отдаёт 0 в терминале и может отдать 1 без TTY. Поэтому
                    # переключение на запасной путь НЕ завязано на result.ok —
                    # раньше именно из-за этого фолбэк молча не срабатывал.
                    if use_preview and hard_failures >= 2:
                        log.warning(
                            "vot_preview_unusable",
                            hint="переключаюсь на режим скачивания файла",
                            output=combined[-400:],
                        )
                        use_preview = False
                        hard_failures = 0
                        continue

                    # Запасной путь уже испробован и тоже не помог. Крутиться
                    # до общего таймаута бессмысленно — это не «ещё не готово»,
                    # а устойчивый отказ. Сдаёмся и показываем, что ответила
                    # утилита, вместо часа тишины.
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
                                "Яндекс отказался переводить этот ролик — "
                                f"{hard_failures} попыток подряд без результата.\n\n"
                                "Ответ утилиты:\n"
                                f"<code>{_short(combined)}</code>\n\n"
                                "Если то же самое повторяется на любых роликах, "
                                "проблема не в видео, а в доступе к бэкенду Яндекса — "
                                "см. раздел «Диагностика» в README."
                            ),
                        )
                else:
                    hard_failures = 0

            # Перевод ещё не готов — ждём с backoff.
            if schedule.exhausted:
                waited = time.monotonic() - started
                raise TranslationTimeout(waited, attempt)

            delay = schedule.next_delay()
            if progress is not None:
                await progress(
                    f"{pending_reason} — попытка {attempt}, "
                    f"следующая через {delay:.0f} с"
                )
            log.debug("vot_wait", attempt=attempt, delay=round(delay, 1))

            try:
                await sleep_with_cancel(delay, cancel_event)
            except CancelledByUser as exc:
                raise JobCancelled() from exc

    # -- разбор результата --------------------------------------------------- #

    async def _try_extract(
        self,
        result: ProcResult,
        combined: str,
        *,
        kind: str,
        outdir: Path,
        workdir: Path,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> Path | None:
        """Достаёт готовый файл из результата вызова. None — результата нет."""
        suffixes = _SUBS_SUFFIXES if kind == "subtitles" else _AUDIO_SUFFIXES

        # 1. Файл, который vot-cli уже положил на диск.
        found = _newest_file(outdir, suffixes)
        if found is not None and found.stat().st_size > 0:
            return found

        payload = _extract_json(result.stdout) or _extract_json(combined)

        # 2. Путь к файлу, указанный в JSON (может быть вне outdir).
        if payload is not None:
            for candidate in _collect_values(payload, _PATH_KEYS):
                path = Path(candidate)
                if path.is_file() and path.stat().st_size > 0:
                    return path

        # 3. Прямая ссылка — скачиваем сами.
        url = None
        if payload is not None:
            url = _first_url(_collect_values(payload, _URL_KEYS))
        if url is None and result.ok:
            # Некоторые версии печатают голую ссылку без JSON.
            url = _first_url(_HTTP_URL_RE.findall(result.stdout))

        if url is None:
            return None

        if kind == "subtitles":
            target = outdir / ("subtitles" + _suffix_for(kind, self._settings))
        else:
            target = outdir / "translation.mp3"

        await self._download(url, target, cancel_event=cancel_event, progress=progress)
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            return None
        return target

    async def _download(
        self,
        url: str,
        target: Path,
        *,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback,
    ) -> None:
        """Скачивает результат перевода с ретраями и докачкой с нуля."""

        async def _once() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".part")
            downloaded = 0
            last_report = 0.0
            async with self._http.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
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

        try:
            await retry_async(
                _once,
                attempts=4,
                exceptions=(httpx.HTTPError, OSError),
                start=3.0,
                factor=2.0,
                maximum=30.0,
                cancel_event=cancel_event,
                description="скачивание дорожки перевода",
            )
        except CancelledByUser as exc:
            raise JobCancelled() from exc
        except (httpx.HTTPError, OSError) as exc:
            raise DownloadFailed(f"не удалось скачать {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
#  Разбор JSON и файлов
# --------------------------------------------------------------------------- #


def _suffix_for(kind: str, settings: VotSettings) -> str:
    if kind != "subtitles":
        return ".mp3"
    return {"srt": ".srt", "vtt": ".vtt", "json": ".json"}.get(settings.subs_format, ".srt")


def _extract_json(text: str) -> Any | None:
    """Достаёт JSON-объект из вывода, даже если вокруг него есть мусор.

    vot-cli печатает прогресс через listr2, поэтому чистого JSON на stdout
    может и не быть. Перебираем все позиции '{' и '[' и берём объект,
    съевший больше всего текста, — это внешний объект ответа, а не вложенный
    в него элемент ``results[]``. Если брать «последний успешно разобранный»,
    выигрывал бы как раз вложенный, потому что он идёт позже по строке.
    """
    if not text:
        return None

    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass

    decoder = json.JSONDecoder()
    best: Any | None = None
    best_length = 0
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, (dict, list)) and end > best_length:
            best, best_length = value, end
    return best


def _walk(node: Any) -> Iterable[tuple[str, Any]]:
    """Рекурсивно обходит структуру, выдавая пары (имя ключа в нижнем регистре, значение)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key).lower(), value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _collect_values(payload: Any, keys: tuple[str, ...]) -> list[str]:
    """Собирает строковые значения по интересующим ключам, сохраняя порядок."""
    wanted = set(keys)
    found: list[str] = []
    for key, value in _walk(payload):
        if key in wanted and isinstance(value, str) and value.strip():
            found.append(value.strip())
    return found


def _first_url(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        candidate = candidate.strip().rstrip(",;")
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return None


def _clear_directory(directory: Path) -> None:
    """Опустошает каталог, не удаляя его самого. Ошибки игнорируются."""
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
    """Самый свежий непустой файл с подходящим расширением."""
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
