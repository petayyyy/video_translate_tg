"""Контроль свободного места.

На небольшом диске (десяток гигабайт) обработка часового ролика в 1080p —
это реальный риск забить раздел под ноль: одновременно на диске лежат
исходное видео, русская дорожка и результат склейки, то есть примерно
удвоенный размер исходника.

Защита выстроена в четыре слоя, от самого дешёвого к самому дорогому:

1. **Проверка на старте бота** — если места уже мало, об этом видно сразу,
   а не через неделю в момент падения.
2. **Проверка перед постановкой задачи** — отказ с понятным текстом до того,
   как что-то скачано.
3. **Оценка по метаданным** — зная длительность и качество, прикидываем,
   сколько понадобится, и отказываемся заранее.
4. **Монитор во время задачи** — фоновая корутина следит за остатком и
   прерывает работу, если место кончается, вместо того чтобы позволить
   системе упереться в 0 байт (в этом состоянии ломается и SQLite,
   и запись логов).
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from app.logging_setup import get_logger

__all__ = [
    "DiskStatus",
    "DiskGuard",
    "DiskSettingsProtocol",
    "NotEnoughSpace",
    "disk_status",
    "format_bytes",
    "estimate_video_bytes",
    "estimate_audio_bytes",
]


class DiskSettingsProtocol(Protocol):
    """То, что DiskGuard ожидает от конфига.

    Протокол, а не импорт ``DiskSettings``: ``app.config`` уже импортирует
    утилиты, и прямой импорт замкнул бы цикл.
    """

    min_free_gb: float
    reserve_gb: float
    abort_free_gb: float
    estimate_multiplier: float
    monitor_enabled: bool
    monitor_interval_sec: float


#: Колбэк, который зовут перед аварийным прерыванием из-за нехватки места.
OnLowCallback = Callable[["DiskStatus"], Awaitable[None]]

log = get_logger(__name__)

GIB = 1024**3
MIB = 1024**2

#: Грубая оценка битрейта по высоте кадра, байт в секунду.
#: Взято по верхней границе типичного YouTube: лучше переоценить и отказать,
#: чем недооценить и забить диск под ноль на середине склейки.
_BYTES_PER_SEC_BY_HEIGHT: tuple[tuple[int, int], ...] = (
    (2160, 1_800_000),   # 4K   ~14 Мбит/с
    (1440, 1_100_000),   # 1440p ~9 Мбит/с
    (1080, 700_000),     # 1080p ~5.6 Мбит/с
    (720, 400_000),      # 720p  ~3.2 Мбит/с
    (480, 200_000),      # 480p  ~1.6 Мбит/с
    (360, 120_000),
    (0, 80_000),
)

#: Русская дорожка от Яндекса — MP3, примерно 128 кбит/с.
_AUDIO_BYTES_PER_SEC = 16_000


def format_bytes(value: float) -> str:
    """Человекочитаемый размер."""
    if value >= GIB:
        return f"{value / GIB:.2f} ГБ"
    if value >= MIB:
        return f"{value / MIB:.0f} МБ"
    return f"{value / 1024:.0f} КБ"


@dataclass(frozen=True, slots=True)
class DiskStatus:
    """Снимок состояния раздела."""

    path: Path
    total: int
    used: int
    free: int

    @property
    def free_gb(self) -> float:
        return self.free / GIB

    @property
    def total_gb(self) -> float:
        return self.total / GIB

    @property
    def used_percent(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 0.0

    def describe(self) -> str:
        return (
            f"свободно {format_bytes(self.free)} из {format_bytes(self.total)} "
            f"(занято {self.used_percent:.0f}%)"
        )


def disk_status(path: str | Path) -> DiskStatus:
    """Состояние раздела, на котором лежит указанный путь."""
    target = Path(path)
    # Если каталога ещё нет, поднимаемся до существующего родителя.
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return DiskStatus(path=Path(path), total=usage.total, used=usage.used, free=usage.free)


def estimate_video_bytes(duration_sec: float | None, height: int | None) -> int:
    """Прикидывает размер видеофайла по длительности и качеству.

    Используется, когда yt-dlp не сообщил ``filesize_approx`` — а он молчит
    довольно часто, особенно для раздельных потоков DASH.
    """
    if not duration_sec or duration_sec <= 0:
        return 0
    target_height = height or 1080
    rate = _BYTES_PER_SEC_BY_HEIGHT[-1][1]
    for threshold, bytes_per_sec in _BYTES_PER_SEC_BY_HEIGHT:
        if target_height >= threshold:
            rate = bytes_per_sec
            break
    return int(duration_sec * rate)


def estimate_audio_bytes(duration_sec: float | None) -> int:
    """Прикидывает размер русской дорожки."""
    if not duration_sec or duration_sec <= 0:
        return 0
    return int(duration_sec * _AUDIO_BYTES_PER_SEC)


class NotEnoughSpace(RuntimeError):
    """Места не хватает. Несёт цифры, чтобы вызывающий код составил текст."""

    def __init__(self, *, need: int, available: int, status: DiskStatus) -> None:
        self.need = need
        self.available = available
        self.status = status
        super().__init__(
            f"нужно {format_bytes(need)}, доступно {format_bytes(available)}"
        )


class DiskGuard:
    """Проверки свободного места и фоновый монитор."""

    __slots__ = ("_settings", "_data_dir")

    def __init__(self, settings: DiskSettingsProtocol, data_dir: Path) -> None:
        self._settings = settings
        self._data_dir = Path(data_dir)

    # -- состояние ----------------------------------------------------------- #

    def status(self) -> DiskStatus:
        return disk_status(self._data_dir)

    def available_for_work(self) -> int:
        """Сколько байт реально можно занять, не трогая неприкосновенный запас."""
        return max(0, self.status().free - int(self._settings.reserve_gb * GIB))

    # -- проверки ------------------------------------------------------------- #

    def check_minimum(self) -> DiskStatus:
        """Проверяет нижний порог свободного места.

        :raises NotEnoughSpace: свободного места меньше ``disk.min_free_gb``.
        """
        status = self.status()
        minimum = int(self._settings.min_free_gb * GIB)
        if status.free < minimum:
            raise NotEnoughSpace(need=minimum, available=status.free, status=status)
        return status

    def check_for_job(self, estimated_output: int) -> None:
        """Проверяет, хватит ли места на задачу целиком.

        Рабочее место больше итогового файла: одновременно лежат исходник,
        дорожка и результат склейки. Отсюда ``disk.estimate_multiplier``.

        :raises NotEnoughSpace: не хватает.
        """
        self.check_minimum()
        if estimated_output <= 0:
            return
        need = int(estimated_output * self._settings.estimate_multiplier)
        available = self.available_for_work()
        if need > available:
            raise NotEnoughSpace(need=need, available=available, status=self.status())

    def max_download_bytes(self) -> int:
        """Потолок для ``yt-dlp --max-filesize``.

        Оставляем место под дорожку и результат склейки: делим доступное
        на множитель оценки.
        """
        available = self.available_for_work()
        multiplier = max(1.0, float(self._settings.estimate_multiplier))
        return max(0, int(available / multiplier))

    # -- монитор --------------------------------------------------------------- #

    async def monitor(
        self,
        cancel_event: asyncio.Event,
        *,
        on_low: OnLowCallback | None = None,
    ) -> None:
        """Следит за остатком места, пока идёт задача.

        При падении ниже ``disk.abort_free_gb`` взводит ``cancel_event`` —
        тот же механизм, что и у ручной отмены, поэтому дочерние процессы
        умирают немедленно, а временные файлы вычищаются штатно.

        Корутина работает до отмены снаружи; исключений не бросает.
        """
        if not self._settings.monitor_enabled:
            return
        threshold = int(self._settings.abort_free_gb * GIB)
        interval = max(5.0, float(self._settings.monitor_interval_sec))

        try:
            while not cancel_event.is_set():
                await asyncio.sleep(interval)
                try:
                    status = self.status()
                except OSError:
                    continue
                if status.free >= threshold:
                    continue

                log.error(
                    "disk_low_aborting",
                    free_mb=round(status.free / MIB),
                    threshold_mb=round(threshold / MIB),
                )
                if on_low is not None:
                    try:
                        await on_low(status)
                    except Exception:
                        log.debug("disk_low_callback_failed", exc_info=True)
                cancel_event.set()
                return
        except asyncio.CancelledError:
            raise
