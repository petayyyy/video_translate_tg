#!/usr/bin/env bash
#
# Точка входа контейнера бота.
#
# Запускается от root ради одного действия — привести права на /data в
# соответствие с пользователем botuser, — после чего немедленно сбрасывает
# привилегии через gosu. Сам бот от root не работает никогда.
#
# Bind-mount ./data с хоста приезжает с правами хозяина каталога, а внутри
# контейнера пишет botuser. Без этого шага первый же запуск падал бы на
# «Permission denied» при создании db.sqlite3.

set -euo pipefail

APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"
CONFIG_FILE="${VIDEO_TG_CONFIG:-/config/config.yaml}"

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

# --- Проверка конфига до всего остального ------------------------------------
if [ ! -f "${CONFIG_FILE}" ]; then
    log "ОШИБКА: не найден файл конфигурации ${CONFIG_FILE}"
    log "Скопируй config.example.yaml в config.yaml рядом с docker-compose.yml"
    log "и заполни token, allowed_user_ids и links.public_base_url."
    exit 1
fi

# --- Каталоги и права ----------------------------------------------------------
mkdir -p \
    "${DATA_DIR}" \
    "${DATA_DIR}/tmp" \
    "${DATA_DIR}/cache" \
    "${DATA_DIR}/files" \
    "${DATA_DIR}/logs"

if [ "$(id -u)" = "0" ]; then
    current_uid="$(stat -c '%u' "${DATA_DIR}")"

    if [ "${current_uid}" != "${APP_UID}" ]; then
        log "выставляю владельца ${DATA_DIR} → ${APP_UID}:${APP_GID}"
        # -R по большому кэшу может занять время, поэтому только при
        # несовпадении владельца верхнего каталога.
        chown -R "${APP_UID}:${APP_GID}" "${DATA_DIR}" || \
            log "ВНИМАНИЕ: chown не удался полностью, продолжаю"
    fi

    # Файлы должны быть читаемы для контейнеров telegram-bot-api и nginx,
    # которые монтируют /data только на чтение и работают под своими UID.
    umask 0022

    if [ "${APP_UID}" = "0" ]; then
        # Запуск от root — не то, чего мы хотим, но это осознанный выбор
        # того, кто выставил APP_UID=0. Работаем, но говорим об этом.
        log "ВНИМАНИЕ: APP_UID=0, бот будет работать от root."
        log "Безопаснее указать в .env непривилегированный UID, например 1000."
        log "запускаю: $*"
        exec "$@"
    fi

    # gosu по числовому UID:GID — имя пользователя может и не существовать,
    # если этот UID уже был занят системным пользователем образа.
    log "запускаю от ${APP_UID}:${APP_GID}: $*"
    exec gosu "${APP_UID}:${APP_GID}" "$@"
fi

log "запускаю: $*"
exec "$@"
