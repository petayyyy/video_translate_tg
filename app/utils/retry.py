"""Экспоненциальные задержки и ретраи для сетевых операций.

Используется в двух режимах:

1. ``retry_async`` — обёртка «повторить корутину N раз при заданных ошибках».
   Подходит для скачивания дорожки, отправки в Telegram, HTTP-запросов.
2. ``BackoffSchedule`` — генератор задержек, когда «повтор» это не исключение,
   а нормальный ответ бэкенда («перевод ещё не готов, жди N секунд»).
   Именно так опрашивается Яндекс.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterator, Sequence, TypeVar

from app.logging_setup import get_logger

__all__ = ["BackoffSchedule", "retry_async", "sleep_with_cancel", "CancelledByUser"]

log = get_logger(__name__)

T = TypeVar("T")


class CancelledByUser(RuntimeError):
    """Ожидание прервано, потому что задачу отменили."""


@dataclass(slots=True)
class BackoffSchedule:
    """Последовательность задержек: start, start*factor, … но не больше maximum.

    ``jitter`` — случайная добавка от 0 до указанного значения. Нужна, чтобы
    несколько задач не били в бэкенд синхронно.

    ``total_budget`` — общий лимит времени с момента создания расписания.
    ``exhausted`` становится True, когда бюджет исчерпан.
    """

    start: float = 20.0
    factor: float = 1.5
    maximum: float = 120.0
    jitter: float = 5.0
    total_budget: float | None = None

    _attempt: int = 0
    _started: float = 0.0

    def __post_init__(self) -> None:
        if self.start <= 0:
            raise ValueError("start должен быть больше нуля")
        if self.factor < 1.0:
            raise ValueError("factor не может быть меньше 1.0")
        self._started = time.monotonic()

    @property
    def attempt(self) -> int:
        """Сколько задержек уже выдано."""
        return self._attempt

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining(self) -> float:
        if self.total_budget is None:
            return float("inf")
        return max(0.0, self.total_budget - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def reset(self) -> None:
        self._attempt = 0
        self._started = time.monotonic()

    def next_delay(self) -> float:
        """Следующая задержка в секундах. Не превышает остаток бюджета."""
        delay = min(self.start * (self.factor**self._attempt), self.maximum)
        self._attempt += 1
        if self.jitter > 0:
            delay += random.uniform(0.0, self.jitter)
        if self.total_budget is not None:
            delay = min(delay, self.remaining)
        return max(0.0, delay)

    def __iter__(self) -> Iterator[float]:
        while not self.exhausted:
            yield self.next_delay()


async def sleep_with_cancel(
    delay: float, cancel_event: asyncio.Event | None = None
) -> None:
    """Спит указанное время, но просыпается сразу при взводе cancel_event.

    :raises CancelledByUser: если событие взведено.
    """
    if delay <= 0:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledByUser("Задача отменена")
        return
    if cancel_event is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return
    raise CancelledByUser("Задача отменена")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    exceptions: Sequence[type[BaseException]] = (Exception,),
    stop_on: Sequence[type[BaseException]] = (),
    start: float = 2.0,
    factor: float = 2.0,
    maximum: float = 60.0,
    jitter: float = 1.0,
    cancel_event: asyncio.Event | None = None,
    description: str = "операция",
) -> T:
    """Повторяет корутину при указанных исключениях с экспоненциальной паузой.

    :param exceptions: что считать поводом повторить.
    :param stop_on: что пробрасывать немедленно, даже если оно попадает
        в ``exceptions``. Нужно для наследников: например,
        ``TelegramEntityTooLarge`` — потомок ``TelegramNetworkError``, но
        повторять на нём бессмысленно и дорого (каждая попытка — перезалив
        файла в гигабайты). Проверяется раньше ``exceptions``.

    Последнее исключение пробрасывается наружу как есть, чтобы вызывающий код
    мог отличить «сеть отвалилась» от «видео недоступно».
    """
    if attempts < 1:
        raise ValueError("attempts должен быть не меньше 1")

    schedule = BackoffSchedule(start=start, factor=factor, maximum=maximum, jitter=jitter)
    last_error: BaseException | None = None
    fatal = tuple(stop_on)

    for attempt_number in range(1, attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledByUser("Задача отменена")
        try:
            return await operation()
        except fatal:  # noqa: B012 — пустой кортеж просто ничего не ловит
            raise
        except tuple(exceptions) as exc:  # noqa: PERF203 — ретрай по своей природе в цикле
            last_error = exc
            if attempt_number >= attempts:
                break
            delay = schedule.next_delay()
            log.warning(
                "retry",
                what=description,
                attempt=attempt_number,
                of=attempts,
                delay=round(delay, 1),
                error=str(exc),
            )
            await sleep_with_cancel(delay, cancel_event)

    assert last_error is not None
    raise last_error
