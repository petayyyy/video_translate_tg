"""Загрузка и валидация конфигурации из YAML.

Конфиг описан pydantic-моделями с ``extra="forbid"``: опечатка в имени ключа
приводит к внятной ошибке на старте, а не к молчаливому игнорированию.

В любых строковых значениях поддерживается подстановка переменных окружения:
    "${BOT_TOKEN}"            — переменная обязательна, иначе ошибка;
    "${BOT_TOKEN:-default}"   — со значением по умолчанию.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "Settings",
    "TelegramSettings",
    "PathsSettings",
    "VotSettings",
    "YtdlpSettings",
    "FfmpegSettings",
    "QueueSettings",
    "PrioritySettings",
    "DiskSettings",
    "CacheSettings",
    "LinksSettings",
    "LoggingSettings",
    "ConfigError",
    "load_settings",
]


class ConfigError(RuntimeError):
    """Конфиг не найден, не разобран или не прошёл валидацию."""


# --------------------------------------------------------------------------- #
#  Подстановка переменных окружения
# --------------------------------------------------------------------------- #

_ENV_PATTERN = re.compile(
    r"""\$\{
        (?P<name>[A-Za-z_][A-Za-z0-9_]*)
        (?::-(?P<default>[^}]*))?
    \}""",
    re.VERBOSE,
)


def _expand_env_string(value: str, *, where: str) -> str:
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        env_value = os.environ.get(name)
        if env_value is not None and env_value != "":
            return env_value
        if default is not None:
            return default
        missing.append(name)
        return ""

    result = _ENV_PATTERN.sub(_replace, value)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"В параметре '{where}' используется переменная окружения {names}, "
            f"но она не задана. Пропиши её в .env или подставь значение прямо в конфиг."
        )
    return result


def _expand_env(node: Any, *, where: str = "") -> Any:
    if isinstance(node, str):
        return _expand_env_string(node, where=where or "<корень>")
    if isinstance(node, dict):
        return {
            key: _expand_env(item, where=f"{where}.{key}" if where else str(key))
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [_expand_env(item, where=f"{where}[{index}]") for index, item in enumerate(node)]
    return node


# --------------------------------------------------------------------------- #
#  Модели
# --------------------------------------------------------------------------- #


#: Признаки того, что значение осталось заглушкой из config.example.yaml.
#: Проверяются до всех остальных правил, чтобы человек получил внятное
#: «ты забыл это заполнить», а не формальную придирку к синтаксису.
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "замени",
    "заполни",
    "вставь",
    "example.com",
    "твой_",
    "your_",
    "changeme",
    "<",
)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )


class TelegramSettings(_Base):
    token: str
    allowed_user_ids: list[int] = Field(default_factory=list)
    use_local_api: bool = True
    api_base_url: str = "http://telegram-bot-api:8081"
    local_mode: bool = True
    use_file_uri: bool = True
    send_as_video: bool = True
    upload_timeout_sec: float = Field(default=3600, gt=0)
    request_timeout_sec: float = Field(default=120, gt=0)
    max_upload_bytes: int = Field(default=2_000_000_000, gt=0)
    progress_edit_interval_sec: float = Field(default=4, ge=1)
    send_retry_attempts: int = Field(default=3, ge=1)
    send_retry_delay_sec: float = Field(default=5, ge=0)

    @field_validator("token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("telegram.token пуст — вставь токен от @BotFather")
        if ":" not in value:
            raise ValueError(
                "telegram.token не похож на токен бота (ожидается вид 123456789:AA...)"
            )
        return value

    @field_validator("allowed_user_ids")
    @classmethod
    def _check_whitelist(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError(
                "telegram.allowed_user_ids пуст. Бот приватный: укажи хотя бы свой user_id, "
                "иначе им не сможет пользоваться никто."
            )
        return value

    @field_validator("api_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_local_consistency(self) -> "TelegramSettings":
        if not self.use_local_api:
            if self.max_upload_bytes > 50_000_000:
                raise ValueError(
                    "telegram.use_local_api=false — это облачный api.telegram.org с лимитом "
                    "50 МБ. Уменьши telegram.max_upload_bytes до 50000000 или включи "
                    "локальный Bot API."
                )
            if self.use_file_uri:
                raise ValueError(
                    "telegram.use_file_uri работает только с локальным Bot API. "
                    "Поставь use_file_uri: false или use_local_api: true."
                )
        elif self.use_file_uri and not self.local_mode:
            raise ValueError(
                "telegram.use_file_uri требует telegram.local_mode: true "
                "(сервер telegram-bot-api должен быть запущен с ключом --local)."
            )
        return self


class PathsSettings(_Base):
    data_dir: Path = Path("/data")
    tmp_dir: Path = Path("/data/tmp")
    cache_dir: Path = Path("/data/cache")
    files_dir: Path = Path("/data/files")
    logs_dir: Path = Path("/data/logs")
    db_path: Path = Path("/data/db.sqlite3")
    cookies_file: Path | None = None

    @field_validator("cookies_file", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def ensure_directories(self) -> None:
        """Создаёт все рабочие каталоги. Идемпотентно."""
        for directory in (
            self.data_dir,
            self.tmp_dir,
            self.cache_dir,
            self.files_dir,
            self.logs_dir,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class VotSettings(_Base):
    binary: str = "vot-cli"
    flavor: Literal["foswly", "live"] = "live"
    source_lang: str = "auto"
    source_lang_fallback: str = "en"
    target_lang: str = "ru"
    lively_voice: bool = False
    force_live_voices: bool = False
    proxy: str = ""
    # Устаревшие поля — оставлены для совместимости со старыми config.yaml,
    # больше не читаются кодом. Удалить после перехода всех серверов.
    #
    # api_token: у vot-cli-live нет ни одного флага для токена или авторизации
    # (проверено по --help версии 1.7.5). Аутентифицироваться форк не умеет,
    # поэтому живые голоса он получает только там, где Яндекс отдаёт их
    # анонимно. Флаг --api-token есть у foswly/vot-cli, но та реализация
    # нерабочая — см. flavor.
    api_token: str = ""
    vot_host: str = ""
    worker_host: str = ""
    use_preview: bool = True
    attempt_timeout_sec: float = Field(default=180, gt=0)
    total_timeout_sec: float = Field(default=3600, gt=0)
    backoff_start_sec: float = Field(default=20, gt=0)
    backoff_factor: float = Field(default=1.5, ge=1.0)
    backoff_max_sec: float = Field(default=120, gt=0)
    backoff_jitter_sec: float = Field(default=5, ge=0)
    subs_format: Literal["srt", "vtt", "json"] = "srt"
    extra_args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_flavor_deprecated(self) -> "VotSettings":
        """foswly/vot-cli 2.x неработоспособен: домен vot.toil.cc — NXDOMAIN.

        Используется только fantomcheg/vot-cli-live (flavor: live).
        Явное указание flavor: foswly — ошибка конфигурации.
        """
        if self.flavor == "foswly":
            raise ValueError(
                "vot.flavor: foswly больше не поддерживается. "
                "Домен vot.toil.cc, через который foswly/vot-cli ходил к Яндексу, "
                "не резолвится (authoritative NXDOMAIN). "
                "Используй flavor: live — fantomcheg/vot-cli-live, "
                "который обращается к api.browser.yandex.ru напрямую. "
                "Если строка уже была flavor: live, просто удали flavour из конфига."
            )
        return self

    @model_validator(mode="after")
    def _deprecation_notice(self) -> "VotSettings":
        """Предупреждает о полях, которые больше не читаются кодом.

        Эти поля оставлены для совместимости со старыми config.yaml.
        После обновления всех серверов их можно удалить вместе с этим валидатором.
        """
        import warnings
        if self.vot_host:
            warnings.warn(
                "vot.vot_host is deprecated — the live fork connects to "
                "api.browser.yandex.ru directly and does not use a proxy host. "
                "Remove from config.yaml.",
                FutureWarning, stacklevel=2,
            )
        if self.api_token:
            warnings.warn(
                "vot.api_token is deprecated — vot-cli-live has no flag for a "
                "token or any other authentication (checked against --help of "
                "1.7.5), so the value is never sent anywhere. Live voices are "
                "granted only where Yandex hands them out anonymously. "
                "Remove from config.yaml.",
                FutureWarning, stacklevel=2,
            )
        if self.worker_host:
            warnings.warn(
                "vot.worker_host is deprecated — the live fork does not use it. "
                "Remove from config.yaml.",
                FutureWarning, stacklevel=2,
            )
        return self

    @model_validator(mode="after")
    def _resolve_binary(self) -> "VotSettings":
        if self.binary == "vot-cli":
            self.__dict__["binary"] = "vot-cli-live"
        return self

    @model_validator(mode="after")
    def _check_budget(self) -> "VotSettings":
        if self.total_timeout_sec < self.attempt_timeout_sec:
            raise ValueError(
                "vot.total_timeout_sec меньше vot.attempt_timeout_sec — "
                "тогда не успеет пройти даже одна попытка перевода."
            )
        if self.backoff_max_sec < self.backoff_start_sec:
            raise ValueError("vot.backoff_max_sec не может быть меньше vot.backoff_start_sec")
        return self


class YtdlpSettings(_Base):
    binary: str = "yt-dlp"
    max_height: int = Field(default=1080, gt=0)
    low_height: int = Field(default=720, gt=0)
    format_template: str = (
        "bestvideo[height<=?{height}]+bestaudio/best[height<=?{height}]/best"
    )
    merge_output_format: str = "mp4"
    metadata_timeout_sec: float = Field(default=120, gt=0)
    download_timeout_sec: float = Field(default=3600, gt=0)
    concurrent_fragments: int = Field(default=4, ge=1)
    retries: int = Field(default=10, ge=0)
    fragment_retries: int = Field(default=10, ge=0)
    rate_limit: str = ""
    proxy: str = ""
    max_duration_minutes: int = Field(default=240, ge=0)
    max_filesize_bytes: int = Field(default=0, ge=0)
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("format_template")
    @classmethod
    def _check_placeholder(cls, value: str) -> str:
        if "{height}" not in value:
            raise ValueError(
                "ytdlp.format_template должен содержать подстановку {height} — "
                "иначе не сработают ни лимит качества, ни команда /q720."
            )
        return value


class FfmpegSettings(_Base):
    binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    original_volume: float = Field(default=0.10, ge=0.0, le=4.0)
    translation_volume: float = Field(default=1.0, ge=0.0, le=4.0)
    dynaudnorm: bool = True
    dynaudnorm_filter: str = "dynaudnorm=f=250:g=15:p=0.9:m=10"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_channels: int = Field(default=2, ge=1, le=8)
    allow_video_transcode: bool = True
    video_transcode_args: list[str] = Field(
        default_factory=lambda: [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    mux_timeout_sec: float = Field(default=3600, gt=0)
    transcode_timeout_sec: float = Field(default=14400, gt=0)
    probe_timeout_sec: float = Field(default=120, gt=0)
    sync_tolerance_sec: float = Field(default=10, ge=0)
    threads: int = Field(default=0, ge=0)

    @field_validator("dynaudnorm_filter")
    @classmethod
    def _check_filter(cls, value: str) -> str:
        if not value.strip().startswith("dynaudnorm"):
            raise ValueError("ffmpeg.dynaudnorm_filter должен начинаться с 'dynaudnorm'")
        return value.strip()


class QueueSettings(_Base):
    concurrency: int = Field(default=1, ge=1, le=8)
    max_pending: int = Field(default=20, ge=1)
    shutdown_grace_sec: float = Field(default=1800, ge=0)
    history_ttl_days: int = Field(default=14, ge=1)


class PrioritySettings(_Base):
    """Насколько бот уступает остальным процессам сервера.

    Актуально, когда машина делится с сервисами, которым нельзя мешать:
    VPN, прокси, чужой бот. Понижение приоритета применяется к тяжёлым
    подпроцессам — vot-cli, yt-dlp и особенно ffmpeg, который иначе
    занимает ядро целиком.
    """

    nice_level: int = Field(default=15, ge=0, le=19)
    idle_io: bool = True


class DiskSettings(_Base):
    """Контроль свободного места. Критично на небольших VPS."""

    min_free_gb: float = Field(default=3.0, ge=0)
    reserve_gb: float = Field(default=1.5, ge=0)
    abort_free_gb: float = Field(default=0.7, ge=0)
    estimate_multiplier: float = Field(default=2.5, ge=1.0)
    check_before_job: bool = True
    monitor_enabled: bool = True
    monitor_interval_sec: float = Field(default=20, ge=5)
    limit_download_size: bool = True

    @model_validator(mode="after")
    def _check_thresholds(self) -> "DiskSettings":
        if self.abort_free_gb > self.reserve_gb:
            raise ValueError(
                "disk.abort_free_gb должен быть не больше disk.reserve_gb: "
                "аварийный порог обязан срабатывать раньше, чем исчерпан запас."
            )
        if self.reserve_gb > self.min_free_gb:
            raise ValueError(
                "disk.reserve_gb должен быть не больше disk.min_free_gb, иначе "
                "задача не пройдёт стартовую проверку никогда."
            )
        return self


class CacheSettings(_Base):
    enabled: bool = True
    ttl_hours: float = Field(default=48, gt=0)
    max_size_gb: float = Field(default=3, ge=0)
    cleanup_interval_min: float = Field(default=30, gt=0)
    keep_file_ids: bool = True
    keep_files_after_send: bool = False

    @property
    def ttl_seconds(self) -> float:
        return self.ttl_hours * 3600.0

    @property
    def max_size_bytes(self) -> int:
        return int(self.max_size_gb * 1024**3)


class LinksSettings(_Base):
    enabled: bool = True
    public_base_url: str = "http://example.com"
    one_time: bool = True
    one_time_grace_min: float = Field(default=60, ge=0)
    ttl_hours: float = Field(default=12, gt=0)
    delete_after_download: bool = True
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=1, le=65535)
    cleanup_interval_min: float = Field(default=15, gt=0)

    @field_validator("public_base_url")
    @classmethod
    def _normalize_base(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if _looks_like_placeholder(value):
            raise ValueError(
                "links.public_base_url не заполнен — там осталась заглушка из "
                "config.example.yaml. Укажи адрес, по которому этот сервер доступен "
                "снаружи, например http://<IP или домен>:<порт из HTTP_PORT>. "
                "Если раздача больших файлов не нужна — поставь links.enabled: false."
            )
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                "links.public_base_url должен начинаться с http:// или https:// "
                f"(получено: {value!r})"
            )
        return value

    @property
    def ttl_seconds(self) -> float:
        return self.ttl_hours * 3600.0


class LoggingSettings(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file: Path = Path("/data/logs/bot.log")
    max_bytes: int = Field(default=52_428_800, gt=0)
    backup_count: int = Field(default=5, ge=0)
    # В YAML ключ называется "json", но так поле назвать нельзя: оно затенило бы
    # метод BaseModel.json(). Отсюда алиас — в конфиге пишется по-прежнему "json".
    json_format: bool = Field(default=True, alias="json")
    console: bool = True

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class Settings(_Base):
    telegram: TelegramSettings
    paths: PathsSettings = Field(default_factory=PathsSettings)
    vot: VotSettings = Field(default_factory=VotSettings)
    ytdlp: YtdlpSettings = Field(default_factory=YtdlpSettings)
    ffmpeg: FfmpegSettings = Field(default_factory=FfmpegSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    priority: PrioritySettings = Field(default_factory=PrioritySettings)
    disk: DiskSettings = Field(default_factory=DiskSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    links: LinksSettings = Field(default_factory=LinksSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @model_validator(mode="after")
    def _cross_checks(self) -> "Settings":
        # Заглушка в public_base_url ловится валидатором самого поля —
        # здесь достаточно проверок, которым нужны сразу несколько секций.
        if self.queue.shutdown_grace_sec > 0:
            longest_stage = max(
                self.vot.total_timeout_sec,
                self.ytdlp.download_timeout_sec,
                self.ffmpeg.mux_timeout_sec,
            )
            if self.queue.shutdown_grace_sec < 60 and longest_stage > 600:
                raise ValueError(
                    "queue.shutdown_grace_sec слишком мал: при остановке контейнера текущая "
                    "задача гарантированно не успеет доработать."
                )
        return self


# --------------------------------------------------------------------------- #
#  Загрузка
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATHS: tuple[Path, ...] = (
    Path("/config/config.yaml"),
    Path("config.yaml"),
)


def _format_validation_error(error: ValidationError, path: Path) -> str:
    lines = [f"Конфиг {path} не прошёл проверку:"]
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<корень>"
        message = item["msg"]
        if item["type"] == "extra_forbidden":
            message = "неизвестный параметр (опечатка? сверься с config.example.yaml)"
        lines.append(f"  • {location}: {message}")
    return "\n".join(lines)


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Читает YAML, подставляет переменные окружения, валидирует.

    Порядок поиска файла, если путь не задан явно:
    ``$VIDEO_TG_CONFIG`` → ``/config/config.yaml`` → ``./config.yaml``.
    """
    candidates: tuple[Path, ...]
    if path is not None:
        candidates = (Path(path),)
    elif os.environ.get("VIDEO_TG_CONFIG"):
        candidates = (Path(os.environ["VIDEO_TG_CONFIG"]),)
    else:
        candidates = DEFAULT_CONFIG_PATHS

    config_path: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            config_path = candidate
            break

    if config_path is None:
        tried = ", ".join(str(candidate) for candidate in candidates)
        raise ConfigError(
            f"Файл конфигурации не найден (искал: {tried}). "
            "Скопируй config.example.yaml в config.yaml и заполни его."
        )

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {config_path}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} — некорректный YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{config_path} пуст.")
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} должен содержать YAML-словарь на верхнем уровне.")

    expanded = _expand_env(raw)

    try:
        settings = Settings.model_validate(expanded)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, config_path)) from exc

    return settings
