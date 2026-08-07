"""Запуск внешних процессов (vot-cli, yt-dlp, ffmpeg) с гарантиями.

Что даёт этот модуль поверх голого ``asyncio.create_subprocess_exec``:

* **Таймаут на процесс.** По истечении процесс не просто «отпускается»,
  а убивается вместе со всей группой — иначе yt-dlp оставляет за собой
  дочерний ffmpeg, а тот продолжает жрать CPU и держать файл.
* **Отмена по событию.** ``cancel_event`` проверяется параллельно ожиданию;
  при взводе процесс убивается так же, как по таймауту.
* **Чтение прогресса.** yt-dlp и ffmpeg печатают прогресс через ``\\r``
  без перевода строки, поэтому вывод читается чанками и режется по обоим
  разделителям — иначе колбэк прогресса не сработал бы ни разу.
* **Ограничение буфера.** Накопленный вывод обрезается, чтобы «болтливый»
  процесс не съел память; в результат попадают начало и хвост.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

from app.logging_setup import get_logger, redact

__all__ = [
    "ProcResult",
    "ProcError",
    "ProcTimeout",
    "ProcCancelled",
    "ProcNotFound",
    "run_process",
    "which",
    "format_command",
]

log = get_logger(__name__)

_IS_POSIX = os.name == "posix"
_CHUNK_SIZE = 65536
_DEFAULT_OUTPUT_LIMIT = 512 * 1024  # 512 КиБ на поток
_LINE_SPLIT = re.compile(r"[\r\n]")

LineCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]


class ProcError(RuntimeError):
    """Базовая ошибка запуска внешнего процесса."""


class ProcNotFound(ProcError):
    """Исполняемый файл не найден в PATH."""


class ProcTimeout(ProcError):
    """Процесс не уложился в отведённое время и был убит."""

    def __init__(self, command: Sequence[str], timeout: float, output: str) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.output = output
        super().__init__(
            f"Процесс {command[0]!r} превысил таймаут {timeout:.0f} с и был остановлен"
        )


class ProcCancelled(ProcError):
    """Процесс убит по запросу отмены задачи."""

    def __init__(self, command: Sequence[str]) -> None:
        self.command = list(command)
        super().__init__(f"Процесс {command[0]!r} остановлен по отмене задачи")


@dataclass(slots=True)
class ProcResult:
    """Результат завершившегося процесса."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    truncated: bool = field(default=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def combined_tail(self, limit: int = 2000) -> str:
        """Хвост объединённого вывода — для сообщений об ошибках и логов."""
        merged = "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)
        merged = merged.strip()
        if len(merged) <= limit:
            return merged
        return "…" + merged[-limit:]


class _BoundedSink:
    """Копит вывод, сохраняя начало и хвост, но не более ``limit`` байт."""

    __slots__ = ("_head", "_tail", "_limit", "_head_size", "_tail_size", "truncated")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._head: list[str] = []
        self._tail: list[str] = []
        self._head_size = 0
        self._tail_size = 0
        self.truncated = False

    def add(self, text: str) -> None:
        size = len(text)
        if self._head_size + size <= self._limit // 2:
            self._head.append(text)
            self._head_size += size
            return
        self._tail.append(text)
        self._tail_size += size
        while self._tail_size > self._limit // 2 and len(self._tail) > 1:
            dropped = self._tail.pop(0)
            self._tail_size -= len(dropped)
            self.truncated = True

    def value(self) -> str:
        head = "\n".join(self._head)
        tail = "\n".join(self._tail)
        if not tail:
            return head
        separator = "\n…[вывод обрезан]…\n" if self.truncated else "\n"
        return head + separator + tail if head else tail


def which(binary: str) -> str | None:
    """Ищет исполняемый файл в PATH. Абсолютный путь возвращает как есть."""
    candidate = Path(binary)
    if candidate.is_absolute():
        return str(candidate) if os.access(candidate, os.X_OK) else None
    return shutil.which(binary)


def format_command(command: Sequence[str]) -> str:
    """Командная строка для лога — с замаскированными секретами."""
    parts: list[str] = []
    for argument in command:
        if any(character in argument for character in " \t\"'"):
            parts.append('"' + argument.replace('"', '\\"') + '"')
        else:
            parts.append(argument)
    return redact(" ".join(parts))


async def _invoke_callback(callback: LineCallback | None, line: str) -> None:
    if callback is None:
        return
    try:
        result = callback(line)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # колбэк прогресса не должен ронять процесс
        log.warning("output_callback_failed", exc_info=True)


async def _pump(
    stream: asyncio.StreamReader | None,
    sink: _BoundedSink,
    callback: LineCallback | None,
) -> None:
    if stream is None:
        return
    buffer = ""
    while True:
        chunk = await stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        pieces = _LINE_SPLIT.split(buffer)
        buffer = pieces.pop()
        for piece in pieces:
            piece = piece.rstrip()
            if not piece:
                continue
            sink.add(piece)
            await _invoke_callback(callback, piece)
    tail = buffer.strip()
    if tail:
        sink.add(tail)
        await _invoke_callback(callback, tail)


def _kill_group(process: asyncio.subprocess.Process, *, hard: bool) -> None:
    """Шлёт сигнал всей группе процессов; при неудаче — самому процессу."""
    if _IS_POSIX:
        sig = signal.SIGKILL if hard else signal.SIGTERM
        try:
            os.killpg(os.getpgid(process.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if hard:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


async def _terminate(process: asyncio.subprocess.Process, *, grace: float = 10.0) -> None:
    """Убивает процесс вместе с группой: сначала мягко, затем принудительно."""
    if process.returncode is not None:
        return

    _kill_group(process, hard=False)
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass

    _kill_group(process, hard=True)
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=grace)
    except asyncio.TimeoutError:
        log.error("process_kill_failed", pid=process.pid)


def _priority_prefix(nice_level: int, idle_io: bool) -> list[str]:
    """Собирает обёртку ``nice``/``ionice`` для фоновых утилит.

    Нужна, когда бот делит машину с сервисами, которым нельзя мешать:
    VPN, прокси, чужой бот. ``nice`` уступает процессорное время,
    ``ionice -c 3`` (класс idle) — дисковые операции. Оба понижения
    доступны непривилегированному пользователю.

    Если утилит нет в образе, обёртка молча пропускается: терять задачу
    из-за отсутствия ``ionice`` было бы глупо.
    """
    if not _IS_POSIX:
        return []
    prefix: list[str] = []
    if nice_level > 0 and which("nice") is not None:
        prefix += ["nice", "-n", str(min(19, nice_level))]
    if idle_io and which("ionice") is not None:
        prefix += ["ionice", "-c", "3"]
    return prefix


async def run_process(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
    on_stdout: LineCallback | None = None,
    on_stderr: LineCallback | None = None,
    output_limit: int = _DEFAULT_OUTPUT_LIMIT,
    check: bool = False,
    log_command: bool = True,
    nice_level: int = 0,
    idle_io: bool = False,
) -> ProcResult:
    """Запускает процесс и дожидается его завершения.

    :param timeout: предельное время работы, секунды.
    :param cancel_event: если взведён — процесс убивается, летит ProcCancelled.
    :param on_stdout / on_stderr: колбэк на каждую строку (для прогресса).
    :param check: при ненулевом коде возврата бросить ProcError.
    :param nice_level: 1..19 — насколько уступать процессор другим процессам.
    :param idle_io: отдавать диск другим процессам (ionice класс idle).
    :raises ProcNotFound: исполняемого файла нет в PATH.
    :raises ProcTimeout: не уложился в timeout.
    :raises ProcCancelled: задача отменена пользователем или shutdown-ом.
    :raises ProcError: ненулевой код возврата при ``check=True``.
    """
    command = [str(part) for part in command]
    if not command:
        raise ValueError("Пустая команда")

    tool_name = command[0]
    resolved = which(tool_name)
    if resolved is None:
        raise ProcNotFound(
            f"Не найден исполняемый файл {tool_name!r}. "
            "Проверь, что он установлен в образе и доступен в PATH."
        )

    # Реальный argv: обёртка приоритета + разрешённый путь + аргументы.
    prefix = _priority_prefix(nice_level, idle_io)
    argv = [*prefix, resolved, *command[1:]]
    executable = which(argv[0]) or argv[0]

    process_env = dict(os.environ)
    if env:
        process_env.update(env)

    if log_command:
        log.debug(
            "process_start",
            cmd=format_command(command),
            timeout=timeout,
            nice=nice_level or None,
            idle_io=idle_io or None,
        )

    creation_kwargs: dict[str, object] = {}
    if _IS_POSIX:
        creation_kwargs["start_new_session"] = True
    elif sys.platform == "win32":  # локальная отладка вне контейнера
        creation_kwargs["creationflags"] = getattr(
            __import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0
        )

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=process_env,
            limit=_CHUNK_SIZE * 4,
            **creation_kwargs,
        )
    except FileNotFoundError as exc:
        raise ProcNotFound(f"Не удалось запустить {tool_name!r}: {exc}") from exc
    except OSError as exc:
        raise ProcError(f"Не удалось запустить {tool_name!r}: {exc}") from exc

    stdout_sink = _BoundedSink(output_limit)
    stderr_sink = _BoundedSink(output_limit)

    pumps = asyncio.gather(
        _pump(process.stdout, stdout_sink, on_stdout),
        _pump(process.stderr, stderr_sink, on_stderr),
    )

    waiters: list[asyncio.Task[object]] = [asyncio.ensure_future(process.wait())]
    cancel_waiter: asyncio.Task[object] | None = None
    if cancel_event is not None:
        cancel_waiter = asyncio.ensure_future(cancel_event.wait())
        waiters.append(cancel_waiter)

    timed_out = False
    cancelled = False
    try:
        done, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            timed_out = True
        elif cancel_waiter is not None and cancel_waiter in done and process.returncode is None:
            cancelled = True
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        if timed_out or cancelled:
            await _terminate(process)
        try:
            await asyncio.wait_for(pumps, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pumps.cancel()
        if process.returncode is None:
            await _terminate(process)

    duration = time.monotonic() - started
    stdout_text = stdout_sink.value()
    stderr_text = stderr_sink.value()

    if timed_out:
        log.warning(
            "process_timeout",
            cmd=format_command(command),
            timeout=timeout,
            duration=round(duration, 1),
        )
        raise ProcTimeout(command, timeout, (stdout_text + "\n" + stderr_text).strip())

    if cancelled:
        log.info("process_cancelled", cmd=format_command(command))
        raise ProcCancelled(command)

    result = ProcResult(
        command=command,
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
        duration=duration,
        truncated=stdout_sink.truncated or stderr_sink.truncated,
    )

    log.debug(
        "process_done",
        cmd=command[0],
        rc=result.returncode,
        duration=round(duration, 1),
    )

    if check and not result.ok:
        raise ProcError(
            f"{command[0]} завершился с кодом {result.returncode}: {result.combined_tail()}"
        )

    return result
