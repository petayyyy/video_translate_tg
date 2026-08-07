"""Обработчики Telegram: команды и приём ссылок."""

from aiogram import Router

from app.handlers.common import router as common_router
from app.handlers.links import router as links_router

__all__ = ["build_router"]


def build_router() -> Router:
    """Собирает корневой роутер.

    Порядок важен: команды регистрируются раньше, чем «любое сообщение
    со ссылкой», иначе ``/audio https://…`` уйдёт в общий обработчик.
    """
    root = Router(name="root")
    root.include_router(common_router)
    root.include_router(links_router)
    return root
