"""Структурное логирование: structlog поверх stdlib logging.

В файл пишется JSON (по одной записи на строку) с ротацией по размеру,
в stdout контейнера — те же события, но человекочитаемо и с цветом.

Секреты (токен бота, OAuth-токен Яндекса, пароль в URL прокси) маскируются
процессором ``_redact_secrets`` перед тем, как запись попадёт в вывод.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, MutableMapping

import structlog

from app.config import LoggingSettings

__all__ = ["setup_logging", "get_logger", "register_secret", "redact"]


# --------------------------------------------------------------------------- #
#  Маскирование секретов
# --------------------------------------------------------------------------- #

_SECRETS: set[str] = set()
_MASK = "***"

# Токен бота вида 123456789:AA... и OAuth-токен Яндекса вида y0_AgAAAA...
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),
    re.compile(r"\by[0-9]_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(--api-token=)\S+"),
    re.compile(r"(://[^/\s:@]+):[^/\s@]+(@)"),  # пароль в http://user:pass@host
)


def register_secret(value: str | None) -> None:
    """Добавляет строку в список того, что нужно вырезать из логов."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Возвращает строку без секретов. Используется и вне логгера."""
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, _MASK)
    text = _TOKEN_PATTERNS[0].sub(_MASK, text)
    text = _TOKEN_PATTERNS[1].sub(_MASK, text)
    text = _TOKEN_PATTERNS[2].sub(r"\1" + _MASK, text)
    text = _TOKEN_PATTERNS[3].sub(r"\1:" + _MASK + r"\2", text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (list, tuple)):
        redacted = [_redact_value(item) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _redact_secrets(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        event_dict[key] = _redact_value(value)
    return event_dict


# --------------------------------------------------------------------------- #
#  Настройка
# --------------------------------------------------------------------------- #

_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.UnicodeDecoder(),
    _redact_secrets,
]

# Библиотеки, которые иначе засоряют DEBUG-лог тысячами строк.
_NOISY_LOGGERS: dict[str, int] = {
    "aiogram.event": logging.WARNING,
    "aiohttp.access": logging.WARNING,
    "aiosqlite": logging.INFO,
    "asyncio": logging.INFO,
    "charset_normalizer": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "multipart": logging.WARNING,
    "urllib3": logging.WARNING,
}


def setup_logging(settings: LoggingSettings) -> None:
    """Конфигурирует structlog и stdlib logging. Вызывается один раз на старте."""
    level = getattr(logging, settings.level, logging.INFO)

    log_path = Path(settings.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if settings.json_format:
        file_renderer: Any = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        file_renderer = structlog.dev.ConsoleRenderer(colors=False)

    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            file_renderer,
        ],
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path),
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    if settings.console:
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(console_formatter)
        root.addHandler(console_handler)

    root.setLevel(level)

    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(noisy_level, level))

    logging.captureWarnings(True)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Возвращает связанный логгер. Имя обычно ``__name__`` модуля."""
    return structlog.stdlib.get_logger(name)
