"""Модели задачи: режим, статус, стадия, сама задача.

Модуль намеренно не зависит ни от aiogram, ни от базы: его импортируют и
пайплайн, и хранилище, и обработчики.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["JobMode", "JobStatus", "Stage", "Job", "STAGE_ORDER"]


class JobMode(StrEnum):
    """Что именно просят сделать с роликом."""

    VIDEO = "video"
    """MP4: русская озвучка поверх приглушённого оригинала (основной режим)."""

    ORIG_MUTED = "orig"
    """MP4: русская озвучка, оригинал заглушён полностью (/orig)."""

    AUDIO = "audio"
    """Только русская дорожка, MP3 (/audio)."""

    SUBS = "subs"
    """Только субтитры, SRT (/subs)."""

    @property
    def title(self) -> str:
        return {
            JobMode.VIDEO: "видео с озвучкой",
            JobMode.ORIG_MUTED: "видео, оригинал заглушён",
            JobMode.AUDIO: "аудиодорожка MP3",
            JobMode.SUBS: "субтитры SRT",
        }[self]

    @property
    def needs_video(self) -> bool:
        """Нужно ли качать видеопоток через yt-dlp."""
        return self in (JobMode.VIDEO, JobMode.ORIG_MUTED)

    @property
    def needs_audio(self) -> bool:
        """Нужна ли русская звуковая дорожка от Яндекса."""
        return self is not JobMode.SUBS


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def icon(self) -> str:
        return {
            JobStatus.PENDING: "⏸",
            JobStatus.RUNNING: "▶️",
            JobStatus.DONE: "✅",
            JobStatus.FAILED: "❌",
            JobStatus.CANCELLED: "🚫",
        }[self]


class Stage(StrEnum):
    """Стадия обработки. Показывается пользователю в едином сообщении."""

    QUEUED = "queued"
    METADATA = "metadata"
    TRANSLATE = "translate"
    FETCH_AUDIO = "fetch_audio"
    FETCH_SUBS = "fetch_subs"
    FETCH_VIDEO = "fetch_video"
    MUX = "mux"
    UPLOAD = "upload"
    FINISHED = "finished"

    @property
    def title(self) -> str:
        return {
            Stage.QUEUED: "в очереди",
            Stage.METADATA: "читаю информацию о ролике",
            Stage.TRANSLATE: "Яндекс переводит",
            Stage.FETCH_AUDIO: "качаю русскую дорожку",
            Stage.FETCH_SUBS: "качаю субтитры",
            Stage.FETCH_VIDEO: "качаю видео",
            Stage.MUX: "склеиваю",
            Stage.UPLOAD: "отправляю",
            Stage.FINISHED: "готово",
        }[self]

    @property
    def icon(self) -> str:
        return {
            Stage.QUEUED: "⏸",
            Stage.METADATA: "🔍",
            Stage.TRANSLATE: "🌐",
            Stage.FETCH_AUDIO: "🎧",
            Stage.FETCH_SUBS: "💬",
            Stage.FETCH_VIDEO: "📥",
            Stage.MUX: "🎬",
            Stage.UPLOAD: "📤",
            Stage.FINISHED: "✅",
        }[self]


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.QUEUED,
    Stage.METADATA,
    Stage.TRANSLATE,
    Stage.FETCH_AUDIO,
    Stage.FETCH_SUBS,
    Stage.FETCH_VIDEO,
    Stage.MUX,
    Stage.UPLOAD,
    Stage.FINISHED,
)


@dataclass(slots=True)
class Job:
    """Одна задача перевода. Хранится в SQLite, переживает рестарт бота."""

    user_id: int
    chat_id: int
    url: str
    platform: str
    video_id: str
    mode: JobMode = JobMode.VIDEO
    max_height: int = 1080

    id: int = 0
    progress_message_id: int | None = None
    request_message_id: int | None = None
    status: JobStatus = JobStatus.PENDING
    stage: Stage = Stage.QUEUED
    title: str | None = None
    duration_sec: float | None = None
    error: str | None = None
    result_path: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def display_title(self) -> str:
        """Название ролика для сообщений; если неизвестно — сама ссылка."""
        if self.title:
            return self.title if len(self.title) <= 80 else self.title[:79] + "…"
        return self.url

    @property
    def elapsed_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def cache_key(self) -> str:
        """Ключ кэша. Для аудио и субтитров качество и громкость не важны."""
        if self.mode in (JobMode.AUDIO, JobMode.SUBS):
            return f"{self.platform}:{self.video_id}:{self.mode.value}"
        return f"{self.platform}:{self.video_id}:{self.mode.value}:{self.max_height}"
