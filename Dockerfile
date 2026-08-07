# syntax=docker/dockerfile:1
#
# Образ бота: Python 3.12 + Node 22 (для vot-cli) + ffmpeg + yt-dlp.
#
# Почему один образ, а не несколько: vot-cli, yt-dlp и ffmpeg запускаются как
# подпроцессы одного питоновского процесса и работают с общим временным
# каталогом. Растащить их по контейнерам можно, но тогда пришлось бы гонять
# гигабайты через сеть или общий том — выигрыша нет, сложности много.

FROM python:3.12-slim-bookworm

# Версии внешних утилит вынесены в аргументы: обновить можно, не трогая
# остальной Dockerfile (docker compose build --build-arg VOT_CLI_VERSION=2.1.0).
ARG NODE_MAJOR=22
ARG VOT_CLI_VERSION=2.0.1
ARG APP_UID=1000
ARG APP_GID=1000

# NODE_OPTIONS=--max-old-space-size задан ниже намеренно скромно (192 МБ):
# образ рассчитан в том числе на VPS с 1 ГБ памяти, где значение больше
# объёма всей машины привело бы к тому, что Node запрашивает память,
# которой нет, и его убивает OOM-killer. vot-cli — тонкий HTTP-клиент,
# ему этого хватает с запасом.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow \
    NODE_OPTIONS=--max-old-space-size=192

# --- Системные пакеты --------------------------------------------------------
# ffmpeg  — склейка и нормализация звука
# gosu    — сброс привилегий в entrypoint после chown на /data
# curl/ca-certificates — healthcheck и загрузка ключа NodeSource
# tini    — корректная передача сигналов и сбор зомби-процессов
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        gnupg \
        gosu \
        tini \
        tzdata \
 && rm -rf /var/lib/apt/lists/*

# --- Node.js (нужен для vot-cli) ---------------------------------------------
RUN set -eux; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*; \
    node --version; \
    npm --version

# --- vot-cli ------------------------------------------------------------------
# Ставим точную версию: «latest» через полгода означает другую утилиту.
RUN npm install -g "vot-cli@${VOT_CLI_VERSION}" \
 && npm cache clean --force \
 && vot-cli --version

# --- Python-зависимости --------------------------------------------------------
WORKDIR /opt/video_tg
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && yt-dlp --version

# --- Пользователь ---------------------------------------------------------------
# Бот работает не от root. UID/GID совпадают с владельцем ./data на хосте,
# иначе результаты сборки окажутся недоступны для правки снаружи.
#
# Создание идёт «мягко»: если такой UID или GID в образе уже занят системным
# пользователем, ничего не пересоздаём. Иначе сборка падала бы, например, на
# APP_UID=0 — а его легко получить, просто запустив установку от root.
# Дальше в entrypoint gosu вызывается по числовому UID:GID, поэтому имя
# пользователя роли не играет.
RUN set -eux; \
    if ! getent group "${APP_GID}" >/dev/null 2>&1; then \
        groupadd --gid "${APP_GID}" botuser; \
    fi; \
    if ! getent passwd "${APP_UID}" >/dev/null 2>&1; then \
        useradd --uid "${APP_UID}" --gid "${APP_GID}" \
                --create-home --shell /bin/bash botuser; \
    fi; \
    getent passwd "${APP_UID}"

# Entrypoint читает их из окружения, чтобы сбросить привилегии по числам.
ENV APP_UID=${APP_UID} \
    APP_GID=${APP_GID}

# --- Код ------------------------------------------------------------------------
COPY app/ ./app/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

# Проверка живости: внутренний HTTP-эндпоинт бота.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# tini как PID 1: он корректно пробрасывает SIGTERM в питон и хоронит зомби,
# которых иначе оставляли бы за собой ffmpeg и yt-dlp.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app"]
