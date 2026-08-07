"""Разбор ссылок: поиск URL в тексте, нормализация, определение площадки и ID.

Зачем нужен собственный разбор, если vot-cli всё равно сам определяет площадку:

* ключ кэша строится из ``(platform, video_id)``, поэтому одна и та же лекция,
  присланная как ``youtu.be/X``, ``youtube.com/watch?v=X&t=42`` и
  ``m.youtube.com/watch?v=X``, должна отдаваться из кэша, а не переводиться
  заново;
* по площадке можно заранее сказать пользователю «эта площадка не
  поддерживается», не тратя минуту на запрос к Яндексу;
* из ссылки нужно вырезать мусорные параметры трекинга, иначе они попадают
  в имена файлов и в лог.

Список площадок соответствует тому, что умеет vot.js. Незнакомая ссылка
не отвергается: она помечается как ``custom`` и всё равно уходит в vot-cli —
поддержка там появляется быстрее, чем обновляется эта таблица.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = [
    "VideoRef",
    "extract_first_url",
    "normalize_url",
    "parse_video_ref",
    "is_probably_url",
    "shorten_for_display",
]


# --------------------------------------------------------------------------- #
#  Поиск URL в тексте сообщения
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(
    r"""(?xi)
    \b
    (?:https?://|www\.)
    [^\s<>"'`\]\)]+
    """
)

# Параметры, которые не влияют на содержимое и только мешают.
_JUNK_QUERY_KEYS = frozenset(
    {
        "ab_channel",
        "app",
        "el",
        "feature",
        "fbclid",
        "gclid",
        "gclsrc",
        "index",
        "pp",
        "si",
        "source_ve_path",
        "start_radio",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_name",
        "utm_source",
        "utm_term",
        "yclid",
    }
)

# Параметры, определяющие сам ролик, — их трогать нельзя.
_MEANINGFUL_QUERY_KEYS = frozenset({"v", "video_id", "id", "list", "z", "clip", "oid"})


def is_probably_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def extract_first_url(text: str | None) -> str | None:
    """Возвращает первый URL из текста сообщения или None."""
    if not text:
        return None
    match = _URL_RE.search(text)
    if match is None:
        return None
    url = match.group(0).rstrip(".,;:!?")
    # Ссылку часто заворачивают в скобки: "(https://…)" — regex захватит ")".
    # Убираем закрывающие скобки, для которых в самом URL нет открывающей.
    pairs = {")": "(", "]": "[", "}": "{"}
    while url and url[-1] in pairs and url.count(url[-1]) > url.count(pairs[url[-1]]):
        url = url[:-1]
    if url.lower().startswith("www."):
        url = "https://" + url
    return url


def normalize_url(url: str) -> str:
    """Убирает мусорные query-параметры, фрагмент и лишний слеш."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("m."):
        netloc = netloc[2:]
    if netloc.startswith("www."):
        netloc = netloc[4:]

    kept: list[str] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        if key in _JUNK_QUERY_KEYS and key not in _MEANINGFUL_QUERY_KEYS:
            continue
        for value in values:
            kept.append(f"{key}={value}")

    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", "&".join(kept), ""))


def shorten_for_display(url: str, limit: int = 64) -> str:
    """Укорачивает ссылку для показа в сообщении о прогрессе."""
    if len(url) <= limit:
        return url
    head = limit // 2 - 2
    tail = limit - head - 1
    return f"{url[:head]}…{url[-tail:]}"


# --------------------------------------------------------------------------- #
#  Площадки
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VideoRef:
    """Разобранная ссылка на видео."""

    url: str
    """Нормализованная ссылка — именно она уходит в vot-cli и yt-dlp."""

    platform: str
    """Машинный идентификатор площадки: ``youtube``, ``vimeo``, ``custom``…"""

    platform_title: str
    """Человекочитаемое название для сообщений."""

    video_id: str
    """ID ролика внутри площадки либо хэш ссылки, если ID не выделяется."""

    supported: bool
    """Известно ли, что площадка поддерживается vot.js."""

    @property
    def cache_key(self) -> str:
        return f"{self.platform}:{self.video_id}"

    def __str__(self) -> str:  # для логов
        return self.cache_key


@dataclass(frozen=True, slots=True)
class _Platform:
    name: str
    title: str
    hosts: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    query_keys: tuple[str, ...] = ()
    supported: bool = True


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_PLATFORMS: tuple[_Platform, ...] = (
    _Platform(
        name="youtube",
        title="YouTube",
        hosts=("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"),
        patterns=_compile(
            r"^/watch/(?P<id>[\w-]{11})",
            r"^/(?:embed|v|e|shorts|live)/(?P<id>[\w-]{11})",
            r"^/(?P<id>[\w-]{11})$",
        ),
        query_keys=("v",),
    ),
    _Platform(
        name="vimeo",
        title="Vimeo",
        hosts=("vimeo.com", "player.vimeo.com"),
        patterns=_compile(r"^/(?:video/)?(?P<id>\d+)"),
    ),
    _Platform(
        name="vk",
        title="VK Видео",
        hosts=("vk.com", "vkvideo.ru", "vk.ru", "m.vk.com"),
        patterns=_compile(
            r"^/video(?P<id>-?\d+_\d+)",
            r"^/video_ext\.php",
            r"^/clip(?P<id>-?\d+_\d+)",
        ),
        query_keys=("z", "oid"),
    ),
    _Platform(
        name="rutube",
        title="Rutube",
        hosts=("rutube.ru",),
        patterns=_compile(r"^/(?:video|play/embed|shorts)/(?P<id>[0-9a-f]{32})"),
    ),
    _Platform(
        name="twitch",
        title="Twitch",
        hosts=("twitch.tv", "clips.twitch.tv"),
        patterns=_compile(r"^/videos/(?P<id>\d+)", r"^/[\w-]+/clip/(?P<id>[\w-]+)"),
    ),
    _Platform(
        name="coub",
        title="Coub",
        hosts=("coub.com",),
        patterns=_compile(r"^/view/(?P<id>\w+)", r"^/embed/(?P<id>\w+)"),
    ),
    _Platform(
        name="bilibili",
        title="Bilibili",
        hosts=("bilibili.com", "b23.tv"),
        patterns=_compile(r"^/video/(?P<id>[\w]+)", r"^/(?P<id>[\w]+)$"),
    ),
    _Platform(
        name="dailymotion",
        title="Dailymotion",
        hosts=("dailymotion.com", "dai.ly"),
        patterns=_compile(r"^/video/(?P<id>\w+)", r"^/(?P<id>\w+)$"),
    ),
    _Platform(
        name="ok",
        title="Одноклассники",
        hosts=("ok.ru", "odnoklassniki.ru"),
        patterns=_compile(r"^/video/(?P<id>\d+)", r"^/videoembed/(?P<id>\d+)"),
    ),
    _Platform(
        name="mailru",
        title="Mail.ru Видео",
        hosts=("my.mail.ru", "mail.ru"),
        patterns=_compile(r"^/v/[\w.@-]+/video/[\w/]*?(?P<id>\d+)\.html"),
    ),
    _Platform(
        name="rumble",
        title="Rumble",
        hosts=("rumble.com",),
        patterns=_compile(r"^/(?P<id>v[\w]+)"),
    ),
    _Platform(
        name="bitchute",
        title="BitChute",
        hosts=("bitchute.com",),
        patterns=_compile(r"^/video/(?P<id>[\w-]+)", r"^/embed/(?P<id>[\w-]+)"),
    ),
    _Platform(
        name="peertube",
        title="PeerTube",
        hosts=("peertube.tv", "peertube.1312.media", "tube.shanti.cafe"),
        patterns=_compile(r"^/w/(?P<id>[\w-]+)", r"^/videos/watch/(?P<id>[\w-]+)"),
    ),
    _Platform(
        name="nineanimetv",
        title="9AnimeTV",
        hosts=("9animetv.to", "aniwatch.to"),
        patterns=_compile(r"^/watch/(?P<id>[\w-]+)"),
    ),
    _Platform(
        name="odysee",
        title="Odysee",
        hosts=("odysee.com",),
        patterns=_compile(r"^/@[^/]+/(?P<id>[^/]+)", r"^/(?P<id>[^/]+)"),
    ),
    _Platform(
        name="sibnet",
        title="Sibnet",
        hosts=("video.sibnet.ru", "sibnet.ru"),
        patterns=_compile(r"^/(?:shell|video)\w*"),
        query_keys=("videoid",),
    ),
    _Platform(
        name="archive",
        title="Archive.org",
        hosts=("archive.org",),
        patterns=_compile(r"^/details/(?P<id>[\w.-]+)", r"^/embed/(?P<id>[\w.-]+)"),
    ),
    _Platform(
        name="kick",
        title="Kick",
        hosts=("kick.com",),
        patterns=_compile(r"^/video/(?P<id>[\w-]+)", r"^/[\w-]+/videos/(?P<id>[\w-]+)"),
    ),
    _Platform(
        name="reddit",
        title="Reddit",
        hosts=("reddit.com", "v.redd.it"),
        patterns=_compile(r"^/r/[^/]+/comments/(?P<id>\w+)", r"^/(?P<id>\w+)$"),
    ),
    _Platform(
        name="tiktok",
        title="TikTok",
        hosts=("tiktok.com", "vm.tiktok.com"),
        patterns=_compile(r"^/@[^/]+/video/(?P<id>\d+)", r"^/(?P<id>[\w]+)$"),
    ),
    _Platform(
        name="twitter",
        title="X (Twitter)",
        hosts=("twitter.com", "x.com"),
        patterns=_compile(r"^/[^/]+/status/(?P<id>\d+)"),
    ),
    _Platform(
        name="facebook",
        title="Facebook",
        hosts=("facebook.com", "fb.watch"),
        patterns=_compile(r"^/[^/]+/videos/(?P<id>\d+)", r"^/watch", r"^/(?P<id>\w+)$"),
        query_keys=("v",),
    ),
    _Platform(
        name="loom",
        title="Loom",
        hosts=("loom.com",),
        patterns=_compile(r"^/share/(?P<id>\w+)", r"^/embed/(?P<id>\w+)"),
    ),
    _Platform(
        name="udemy",
        title="Udemy",
        hosts=("udemy.com",),
        patterns=_compile(r"^/course/[^/]+/learn/lecture/(?P<id>\d+)"),
    ),
    _Platform(
        name="coursera",
        title="Coursera",
        hosts=("coursera.org",),
        patterns=_compile(r"^/learn/[^/]+/lecture/(?P<id>\w+)"),
    ),
    _Platform(
        name="yandexdisk",
        title="Яндекс.Диск",
        hosts=("disk.yandex.ru", "disk.yandex.com", "yadi.sk"),
        patterns=_compile(r"^/i/(?P<id>\w+)", r"^/d/(?P<id>\w+)"),
    ),
    _Platform(
        name="googledrive",
        title="Google Drive",
        hosts=("drive.google.com",),
        patterns=_compile(r"^/file/d/(?P<id>[\w-]+)"),
    ),
)

_HOST_INDEX: dict[str, _Platform] = {}
for _platform in _PLATFORMS:
    for _host in _platform.hosts:
        _HOST_INDEX[_host] = _platform


def _match_host(netloc: str) -> _Platform | None:
    host = netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host in _HOST_INDEX:
        return _HOST_INDEX[host]
    # поддомены: sport.rutube.ru → rutube.ru
    parts = host.split(".")
    for index in range(1, len(parts) - 1):
        candidate = ".".join(parts[index:])
        if candidate in _HOST_INDEX:
            return _HOST_INDEX[candidate]
    return None


def _first_query_value(query: str, keys: Iterable[str]) -> str | None:
    if not query:
        return None
    parsed = parse_qs(query, keep_blank_values=False)
    for key in keys:
        values = parsed.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def _hash_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def parse_video_ref(raw_url: str) -> VideoRef:
    """Нормализует ссылку и определяет площадку с ID ролика.

    Никогда не бросает исключение: для неизвестной площадки возвращается
    ``platform="custom"`` и ``supported=False``, решение о попытке перевода
    принимает вызывающий код.
    """
    url = normalize_url(raw_url)
    parsed = urlparse(url)
    platform = _match_host(parsed.netloc)

    if platform is None:
        return VideoRef(
            url=url,
            platform="custom",
            platform_title=parsed.netloc or "неизвестная площадка",
            video_id=_hash_id(url),
            supported=False,
        )

    video_id: str | None = None

    for pattern in platform.patterns:
        match = pattern.search(parsed.path)
        if match is None:
            continue
        candidate = match.groupdict().get("id")
        if candidate:
            video_id = candidate
            break

    if video_id is None and platform.query_keys:
        video_id = _first_query_value(parsed.query, platform.query_keys)

    if video_id is None:
        video_id = _hash_id(url)

    return VideoRef(
        url=url,
        platform=platform.name,
        platform_title=platform.title,
        video_id=video_id,
        supported=platform.supported,
    )
