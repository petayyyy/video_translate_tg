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

APP_USER="${APP_USER:-botuser}"
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
    target_uid="$(id -u "${APP_USER}")"
    target_gid="$(id -g "${APP_USER}")"
    current_uid="$(stat -c '%u' "${DATA_DIR}")"

    if [ "${current_uid}" != "${target_uid}" ]; then
        log "выставляю владельца ${DATA_DIR} → ${APP_USER} (${target_uid}:${target_gid})"
        # -R по большому кэшу может занять время, поэтому только при
        # несовпадении владельца верхнего каталога.
        chown -R "${target_uid}:${target_gid}" "${DATA_DIR}" || \
            log "ВНИМАНИЕ: chown не удался полностью, продолжаю"
    fi

    # Файлы должны быть читаемы для контейнеров telegram-bot-api и nginx,
    # которые монтируют /data только на чтение и работают под своими UID.
    umask 0022

    log "запускаю от пользователя ${APP_USER}: $*"
    exec gosu "${APP_USER}" "$@"
fi

log "запускаю: $*"
exec "$@"
