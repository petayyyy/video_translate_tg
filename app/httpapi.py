"""Внутренний HTTP-сервер бота.

Два эндпоинта, оба доступны только изнутри docker-сети:

* ``GET /_auth`` — на него nginx ходит через ``auth_request`` перед выдачей
  файла по прямой ссылке. Сам nginx одноразовость реализовать не умеет, а бот
  умеет: проверяет токен, срок жизни и факт использования. Ответ 200 —
  отдавать, 403 — отказать.
* ``GET /healthz`` — проверка живости для ``docker compose ps`` и мониторинга.

Токен берётся из заголовка ``X-Original-URI`` (его подставляет nginx) или из
query-параметра ``token`` — второй вариант нужен, чтобы эндпоинт можно было
дёрнуть руками при диагностике.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from app.config import Settings
from app.logging_setup import get_logger
from app.storage.links import LinkStore

__all__ = ["HttpApi"]

log = get_logger(__name__)

#: /dl/<token>/<имя файла>
_URI_TOKEN_RE = re.compile(r"^/dl/(?P<token>[A-Za-z0-9_\-]{16,64})(?:/|$)")


class HttpApi:
    """aiohttp-приложение, живущее рядом с ботом."""

    __slots__ = ("_settings", "_links", "_runner", "_site", "_extra")

    def __init__(
        self,
        settings: Settings,
        links: LinkStore,
        *,
        extra_status: dict[str, Any] | None = None,
    ) -> None:
        self._settings = settings
        self._links = links
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._extra = extra_status or {}

    # -- жизненный цикл -------------------------------------------------------- #

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/_auth", self._handle_auth)
        app.router.add_get("/healthz", self._handle_health)
        return app

    async def start(self) -> None:
        settings = self._settings.links
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, settings.http_host, settings.http_port)
        await self._site.start()
        log.info("httpapi_started", host=settings.http_host, port=settings.http_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            log.info("httpapi_stopped")

    # -- обработчики ------------------------------------------------------------ #

    @staticmethod
    def _token_from_request(request: web.Request) -> str | None:
        original = request.headers.get("X-Original-URI", "")
        match = _URI_TOKEN_RE.match(original.split("?")[0])
        if match is not None:
            return match.group("token")
        token = request.query.get("token")
        return token.strip() if token else None

    async def _handle_auth(self, request: web.Request) -> web.Response:
        token = self._token_from_request(request)
        if token is None:
            log.info("auth_no_token", uri=request.headers.get("X-Original-URI", ""))
            return web.Response(status=403, text="no token")

        allowed, reason = await self._links.authorize(token)
        if allowed:
            return web.Response(status=200, text="ok")

        log.info("auth_denied", token=token[:8] + "…", reason=reason)
        return web.Response(status=403, text=reason)

    async def _handle_health(self, _request: web.Request) -> web.Response:
        payload: dict[str, Any] = {"status": "ok"}
        for key, value in self._extra.items():
            payload[key] = value() if callable(value) else value
        return web.json_response(payload)
