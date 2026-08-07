"""Временные каталоги с гарантированной очисткой.

Требование «корректная очистка временных файлов при любом исходе, включая
падение» закрывается тремя слоями:

1. ``async with TempWorkspace(...)`` — уборка при выходе из блока, в том числе
   по исключению и по ``asyncio.CancelledError``.
2. Глобальный реестр живых рабочих каталогов + ``cleanup_all()``, который
   вызывается из обработчика сигналов и из ``atexit``. Если процесс убивают
   SIGTERM-ом на середине склейки, мусор всё равно уходит.
3. ``sweep_stale()`` на старте: если контейнер прибили SIGKILL-ом и второй
   слой не отработал, осиротевшие каталоги подчищаются при следующем запуске.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import time
import uuid
from pathlib import Path
from types import TracebackType

from app.logging_setup import get_logger

__all__ = ["TempWorkspace", "cleanup_all", "sweep_stale", "directory_size", "safe_rmtree"]

log = get_logger(__name__)

_LIVE: set[Path] = set()
_PREFIX = "job-"


def safe_rmtree(path: Path) -> None:
    """Удаляет каталог или файл, никогда не бросая исключение наружу."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        log.warning("cleanup_failed", path=str(path), error=str(exc))


def directory_size(path: Path) -> int:
    """Суммарный размер файлов в каталоге, байты. Ошибки чтения игнорируются."""
    total = 0
    if not path.is_dir():
        return 0
    for root, _dirs, files in os.walk(path, onerror=lambda _err: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


class TempWorkspace:
    """Изолированный рабочий каталог одной задачи.

    Пример::

        async with TempWorkspace(settings.paths.tmp_dir, job_id=17) as workspace:
            audio = workspace.path / "ru.mp3"
            ...
        # каталог уже удалён, что бы ни случилось внутри
    """

    __slots__ = ("_root", "_path", "_keep", "_label")

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        job_id: int | str | None = None,
        keep_on_error: bool = False,
    ) -> None:
        self._root = Path(root)
        self._keep = keep_on_error
        suffix = str(job_id) if job_id is not None else uuid.uuid4().hex[:8]
        self._label = f"{_PREFIX}{suffix}-{uuid.uuid4().hex[:6]}"
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("TempWorkspace ещё не открыт (используй async with)")
        return self._path

    @property
    def size_bytes(self) -> int:
        return directory_size(self.path) if self._path is not None else 0

    def child(self, name: str) -> Path:
        """Путь внутри рабочего каталога. Имя очищается от разделителей."""
        cleaned = name.replace("/", "_").replace("\\", "_").strip() or "file"
        return self.path / cleaned

    def open(self) -> Path:
        """Синхронное создание каталога (для не-async кода и тестов)."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / self._label
        path.mkdir(parents=True, exist_ok=False)
        self._path = path
        _LIVE.add(path)
        log.debug("workspace_created", path=str(path))
        return path

    def close(self, *, failed: bool = False) -> None:
        """Синхронное удаление каталога."""
        if self._path is None:
            return
        path, self._path = self._path, None
        _LIVE.discard(path)
        if failed and self._keep:
            log.warning("workspace_kept_for_debug", path=str(path))
            return
        safe_rmtree(path)
        log.debug("workspace_removed", path=str(path))

    async def __aenter__(self) -> "TempWorkspace":
        await asyncio.to_thread(self.open)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        await asyncio.to_thread(self.close, failed=exc_type is not None)
        return False


def cleanup_all() -> None:
    """Удаляет все ещё живые рабочие каталоги. Безопасно вызывать многократно."""
    for path in list(_LIVE):
        _LIVE.discard(path)
        safe_rmtree(path)


def sweep_stale(root: str | os.PathLike[str], *, older_than_sec: float = 3600.0) -> int:
    """Удаляет осиротевшие каталоги задач, оставшиеся от прошлых запусков.

    Возвращает количество удалённых каталогов.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in root_path.iterdir():
        if not entry.name.startswith(_PREFIX):
            continue
        if entry in _LIVE:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < older_than_sec:
            continue
        safe_rmtree(entry)
        removed += 1
    if removed:
        log.info("stale_workspaces_removed", count=removed, root=str(root_path))
    return removed


atexit.register(cleanup_all)
