"""Middleware: белый список пользователей и контекст логирования.

Бот приватный, поэтому фильтрация делается не в каждом обработчике, а один
раз на входе в диспетчер. Чужие апдейты отбрасываются молча: отвечать
«вам сюда нельзя» — значит подтверждать, что бот существует и жив.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.logging_setup import get_logger

__all__ = ["WhitelistMiddleware", "LoggingContextMiddleware"]

log = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _extract_user(event: TelegramObject, data: dict[str, Any]) -> User | None:
    user = data.get("event_from_user")
    if isinstance(user, User):
        return user
    if isinstance(event, (Message, CallbackQuery)):
        return event.from_user
    return None


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает только пользователей из ``telegram.allowed_user_ids``."""

    __slots__ = ("_allowed",)

    def __init__(self, allowed_user_ids: list[int]) -> None:
        self._allowed = frozenset(allowed_user_ids)
        super().__init__()

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event, data)
        if user is None:
            return None
        if user.id not in self._allowed:
            log.warning(
                "access_denied",
                user_id=user.id,
                username=user.username,
                event=type(event).__name__,
            )
            return None
        return await handler(event, data)


class LoggingContextMiddleware(BaseMiddleware):
    """Привязывает user_id и chat_id ко всем записям лога внутри обработчика."""

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = _extract_user(event, data)
        chat = data.get("event_chat")
        tokens = structlog.contextvars.bind_contextvars(
            user_id=user.id if user is not None else None,
            chat_id=getattr(chat, "id", None),
        )
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
