"""Доменные ошибки пайплайна и распознавание чужого вывода.

Каждая ошибка несёт готовый текст для пользователя (``user_message``) —
без стектрейсов, путей и внутренних деталей. Полная диагностика уходит
в лог отдельно.

Здесь же живут классификаторы: они превращают невнятный stderr от yt-dlp
и vot-cli в конкретное исключение. Это единственное место, где приходится
опираться на текст чужих сообщений, поэтому все шаблоны собраны вместе —
если vot-cli или yt-dlp поменяют формулировки, править нужно только тут.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "PipelineError",
    "UnsupportedPlatform",
    "VideoUnavailable",
    "VideoIsLive",
    "VideoTooLong",
    "VideoTooBig",
    "TranslationUnavailable",
    "TranslationTimeout",
    "SubtitlesUnavailable",
    "DownloadFailed",
    "MuxFailed",
    "ToolMissing",
    "DiskFull",
    "NotEnoughSpace",
    "SpaceRanOut",
    "JobCancelled",
    "classify_vot_output",
    "classify_ytdlp_output",
]


class PipelineError(Exception):
    """Базовая ошибка обработки. Всегда содержит текст для пользователя."""

    #: Что показать пользователю. Переопределяется в наследниках.
    user_message = "Не получилось обработать этот ролик."

    #: Имеет ли смысл повторять задачу той же командой.
    retriable = False

    def __init__(self, detail: str = "", *, user_message: str | None = None) -> None:
        self.detail = detail.strip()
        if user_message is not None:
            self.user_message = user_message
        super().__init__(self.detail or self.user_message)

    def render(self) -> str:
        """Текст для отправки в чат."""
        message = self.user_message
        if self.retriable:
            message += "\n\nМожно попробовать ещё раз — ошибка похожа на временную."
        return message


class UnsupportedPlatform(PipelineError):
    user_message = (
        "Эта площадка не поддерживается переводчиком Яндекса.\n"
        "Поддерживаются YouTube, Vimeo, VK, Rutube, Twitch, Coub, Bilibili, "
        "Dailymotion и ещё несколько десятков сайтов."
    )


class VideoUnavailable(PipelineError):
    user_message = (
        "Видео недоступно: оно приватное, удалено, ограничено по возрасту "
        "или закрыто в этом регионе.\n"
        "Для роликов с возрастным ограничением помогает файл cookies "
        "(параметр paths.cookies_file в конфиге)."
    )


class VideoIsLive(PipelineError):
    user_message = (
        "Это прямой эфир. Перевести можно только запись — пришли ссылку, "
        "когда трансляция закончится и появится VOD."
    )


class VideoTooLong(PipelineError):
    def __init__(self, duration_min: float, limit_min: int) -> None:
        self.user_message = (
            f"Ролик слишком длинный: {duration_min:.0f} мин при лимите {limit_min} мин.\n"
            "Лимит меняется параметром ytdlp.max_duration_minutes в конфиге "
            "(0 — снять ограничение)."
        )
        super().__init__(f"duration={duration_min:.1f}min limit={limit_min}min")


class VideoTooBig(PipelineError):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.user_message = (
            f"Файл слишком большой: примерно {size_bytes / 1024**3:.1f} ГБ при лимите "
            f"{limit_bytes / 1024**3:.1f} ГБ.\n"
            "Попробуй /q720 — в 720p ролик будет заметно меньше."
        )
        super().__init__(f"size={size_bytes} limit={limit_bytes}")


class TranslationUnavailable(PipelineError):
    user_message = (
        "Яндекс не смог перевести этот ролик.\n"
        "Чаще всего так бывает, если в видео нет речи, язык оригинала "
        "не поддерживается, или ролик слишком длинный для их бэкенда."
    )


class TranslationTimeout(PipelineError):
    retriable = True

    def __init__(self, waited_sec: float, attempts: int) -> None:
        self.user_message = (
            f"Яндекс не отдал перевод за {waited_sec / 60:.0f} мин "
            f"({attempts} попыт{'ка' if attempts == 1 else 'ок'}).\n"
            "Длинные лекции иногда переводятся дольше — пришли ссылку ещё раз "
            "через несколько минут, перевод обычно уже готов и придёт сразу.\n"
            "Общий лимит ожидания задаётся параметром vot.total_timeout_sec."
        )
        super().__init__(f"waited={waited_sec:.0f}s attempts={attempts}")


class SubtitlesUnavailable(PipelineError):
    user_message = (
        "Для этого ролика нет субтитров в нужном языке.\n"
        "Попробуй обычный режим — озвучка делается и без готовых субтитров."
    )


class DownloadFailed(PipelineError):
    retriable = True
    user_message = (
        "Не удалось скачать видео.\n"
        "Возможные причины: площадка временно недоступна, изменился формат "
        "выдачи, или нужен файл cookies. Если ошибка повторяется — обнови "
        "yt-dlp (см. раздел «Обновление» в README)."
    )


class MuxFailed(PipelineError):
    user_message = (
        "Не получилось склеить видео с русской дорожкой.\n"
        "Подробности в логе бота. Если исходник в экзотическом формате, "
        "помогает включить ffmpeg.allow_video_transcode в конфиге."
    )


class ToolMissing(PipelineError):
    def __init__(self, tool: str, detail: str = "") -> None:
        self.user_message = (
            f"В контейнере не найдена утилита «{tool}».\n"
            "Похоже, образ собран неправильно — пересобери его: "
            "docker compose build --no-cache"
        )
        super().__init__(detail or tool)


class DiskFull(PipelineError):
    user_message = (
        "На диске кончилось место.\n"
        "Освободи место или уменьши cache.max_size_gb в конфиге."
    )


class NotEnoughSpace(PipelineError):
    """Места не хватит — выяснено ДО того, как что-то скачано."""

    retriable = True

    def __init__(
        self,
        *,
        need_bytes: int,
        free_bytes: int,
        detail: str = "",
        suggest_lower_quality: bool = True,
    ) -> None:
        lines = [
            "На диске не хватит места для этого ролика.",
            f"Нужно примерно {need_bytes / 1024**3:.1f} ГБ, "
            f"свободно {free_bytes / 1024**3:.1f} ГБ.",
            "",
            "Что можно сделать:",
        ]
        if suggest_lower_quality:
            lines.append("• /q720 — в 720p ролик занимает примерно вчетверо меньше")
            lines.append("• /audio — только звуковая дорожка, это десятки мегабайт")
        lines.append("• /cleanup — очистить кэш на сервере")
        self.user_message = "\n".join(lines)
        super().__init__(detail or f"need={need_bytes} free={free_bytes}")


class SpaceRanOut(PipelineError):
    """Место кончилось уже во время обработки — задача прервана монитором."""

    retriable = True
    user_message = (
        "Место на диске закончилось прямо во время обработки, задача остановлена.\n"
        "Временные файлы удалены.\n\n"
        "Попробуй /q720 или /audio, либо освободи место командой /cleanup."
    )


class JobCancelled(PipelineError):
    user_message = "Задача отменена."


# --------------------------------------------------------------------------- #
#  Распознавание чужого вывода
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    factory: type[PipelineError]


def _rules(*items: tuple[str, type[PipelineError]]) -> tuple[_Rule, ...]:
    return tuple(_Rule(re.compile(pattern, re.IGNORECASE), factory) for pattern, factory in items)


# Признаки того, что перевод ещё делается и надо просто подождать.
_VOT_PENDING = re.compile(
    r"(?:wait|ожид|подожд|in\s*progress|not\s*ready|ещ[её]\s*не|"
    r"translation\s+is\s+being|переводится|try\s+again\s+later|"
    r"remaining\s*time|estimated)",
    re.IGNORECASE,
)

_VOT_RULES = _rules(
    (r"(?:unsupported|not\s+supported).{0,40}(?:site|platform|host|url)", UnsupportedPlatform),
    (r"(?:can'?t|cannot|unable to)\s+(?:find|detect)\s+(?:video|service)", UnsupportedPlatform),
    (r"video\s+(?:is\s+)?(?:unavailable|not\s+found|private|deleted)", VideoUnavailable),
    (r"(?:404|403)\s*(?:not found|forbidden)?", VideoUnavailable),
    (r"(?:no|нет)\s+(?:subtitles|субтитр)", SubtitlesUnavailable),
    (r"subtitles?\s+(?:are\s+)?(?:not\s+available|unavailable|missing)", SubtitlesUnavailable),
    (r"(?:language|язык).{0,30}(?:not\s+supported|не\s+поддерж)", TranslationUnavailable),
    (r"translation\s+(?:failed|error|unavailable|is\s+not\s+available)", TranslationUnavailable),
    (r"(?:too\s+long|слишком\s+длин|video\s+duration\s+exceed)", TranslationUnavailable),
    (r"(?:no\s+speech|нет\s+речи|silent)", TranslationUnavailable),
)

_YTDLP_RULES = _rules(
    (r"unsupported url", UnsupportedPlatform),
    (r"no video formats found", VideoUnavailable),
    (r"video unavailable", VideoUnavailable),
    (r"private video", VideoUnavailable),
    (r"members[- ]only", VideoUnavailable),
    (r"this video has been removed", VideoUnavailable),
    (r"sign in to confirm your age", VideoUnavailable),
    (r"age[- ]restricted", VideoUnavailable),
    (r"confirm you'?re not a bot", VideoUnavailable),
    (r"not available in your country", VideoUnavailable),
    (r"account associated with this video has been terminated", VideoUnavailable),
    (r"is live|live event will begin|premieres in", VideoIsLive),
    (r"no space left on device", DiskFull),
    (r"(?:unable to download|http error 5\d\d|connection reset|timed out)", DownloadFailed),
)


def _match(text: str, rules: tuple[_Rule, ...]) -> type[PipelineError] | None:
    for rule in rules:
        if rule.pattern.search(text):
            return rule.factory
    return None


def classify_vot_output(text: str) -> PipelineError | None:
    """Определяет ошибку по выводу vot-cli. None — ошибка не распознана."""
    if not text:
        return None
    factory = _match(text, _VOT_RULES)
    if factory is None:
        return None
    return factory(detail=_tail(text))


def looks_like_translation_pending(text: str) -> bool:
    """True, если vot-cli сообщает «перевод ещё готовится, подожди»."""
    return bool(text) and bool(_VOT_PENDING.search(text))


def classify_ytdlp_output(text: str) -> PipelineError | None:
    """Определяет ошибку по выводу yt-dlp. None — ошибка не распознана."""
    if not text:
        return None
    factory = _match(text, _YTDLP_RULES)
    if factory is None:
        return None
    return factory(detail=_tail(text))


def _tail(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
