"""Точка входа: ``python -m app``.

Отвечает за три вещи и больше ни за что:

1. Загрузить конфиг и настроить логирование до того, как что-то пойдёт не так.
2. Повесить обработчики SIGTERM и SIGINT. SIGTERM прилетает от ``docker stop``,
   и обработать его нужно правильно: не убивать текущую задачу, а дать ей
   доработать (см. ``QueueManager.shutdown``). Второй сигнал подряд —
   аварийный выход, если ждать больше нельзя.
3. Гарантировать уборку временных файлов при любом исходе.

Код возврата: 0 — штатная остановка, 1 — ошибка конфигурации или падение.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from app import __version__
from app.bot import Application
from app.config import ConfigError, Settings, load_settings
from app.logging_setup import get_logger, setup_logging
from app.utils.tempdirs import cleanup_all

log = get_logger(__name__)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, application: Application) -> None:
    """Ставит обработчики сигналов остановки.

    Первый сигнал — вежливая остановка. Второй — немедленный выход: если
    администратор жмёт Ctrl+C дважды, он уже не хочет ждать склейку.
    """
    state = {"count": 0}

    def _handle(signal_number: int) -> None:
        state["count"] += 1
        name = signal.Signals(signal_number).name
        if state["count"] == 1:
            log.info("signal_received", signal=name, action="graceful shutdown")
            application.request_stop()
        else:
            log.warning("signal_received_again", signal=name, action="force exit")
            cleanup_all()
            os._exit(1)

    for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, _handle, signal_number)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler не поддерживается для части сигналов.
            try:
                signal.signal(signal_number, lambda number, _frame: _handle(number))
            except (ValueError, OSError):
                log.debug("signal_handler_unavailable", signal=signal_name)


async def _amain(settings: Settings) -> int:
    application = Application(settings)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, application)

    try:
        await application.start()
    except Exception:
        log.exception("startup_failed")
        await application.stop()
        return 1

    exit_code = 0
    try:
        await application.run()
    except asyncio.CancelledError:
        log.info("run_cancelled")
    except Exception:
        log.exception("runtime_failed")
        exit_code = 1
    finally:
        await application.stop()

    return exit_code


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        settings = load_settings(config_path)
    except ConfigError as error:
        # Логирование ещё не настроено — пишем прямо в stderr.
        print(f"Ошибка конфигурации:\n{error}", file=sys.stderr)
        return 1

    setup_logging(settings.logging)
    log.info(
        "starting",
        version=__version__,
        python=sys.version.split()[0],
        config=config_path or "<автопоиск>",
    )

    try:
        return asyncio.run(_amain(settings))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    finally:
        cleanup_all()


if __name__ == "__main__":
    raise SystemExit(main())
