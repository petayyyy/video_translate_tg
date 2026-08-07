#!/usr/bin/env bash
#
# Установка video_tg с нуля на чистой Ubuntu 24.04.
#
# Что делает:
#   1. Ставит Docker и плагин compose из официального репозитория Docker
#      (в репозиториях Ubuntu лежит старый docker.io без compose v2).
#   2. Создаёт каталоги данных с правильным владельцем.
#   3. Готовит .env и config.yaml из примеров, если их ещё нет.
#   4. Собирает образ и проверяет конфигурацию compose.
#
# Запуск:
#   sudo bash scripts/install.sh
#
# Скрипт идемпотентен: повторный запуск ничего не ломает и не перетирает
# уже заполненные .env и config.yaml.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

# Пользователь, от которого будет работать бот. Если скрипт запущен через
# sudo — берём того, кто вызвал sudo, а не root.
TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_UID="$(id -u "${TARGET_USER}")"
TARGET_GID="$(id -g "${TARGET_USER}")"

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; OFF='\033[0m'

say()  { printf "${BOLD}==>${OFF} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✓${OFF} %s\n" "$*"; }
warn() { printf "${YELLOW}  !${OFF} %s\n" "$*"; }
die()  { printf "${RED}  ✗ %s${OFF}\n" "$*" >&2; exit 1; }

# --- Проверки --------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "Запусти через sudo: sudo bash scripts/install.sh"
fi

if [ ! -f docker-compose.yml ]; then
    die "docker-compose.yml не найден. Запускай скрипт из каталога проекта."
fi

say "Проект: ${PROJECT_DIR}"
say "Владелец данных: ${TARGET_USER} (${TARGET_UID}:${TARGET_GID})"

# --- Docker ------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker и compose v2 уже установлены: $(docker --version)"
else
    say "Ставлю Docker из официального репозитория"

    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
    fi

    cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable
EOF

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
    ok "Docker установлен: $(docker --version)"
fi

# Чтобы можно было работать без sudo.
if [ "${TARGET_USER}" != "root" ]; then
    if ! id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -qx docker; then
        usermod -aG docker "${TARGET_USER}"
        warn "Пользователь ${TARGET_USER} добавлен в группу docker."
        warn "Чтобы это применилось, перелогинься или выполни: newgrp docker"
    else
        ok "Пользователь ${TARGET_USER} уже в группе docker"
    fi
fi

# --- Каталоги ------------------------------------------------------------------
say "Создаю каталоги данных"
mkdir -p data/tmp data/cache data/files data/logs nginx/certs
chown -R "${TARGET_UID}:${TARGET_GID}" data nginx
ok "data/{tmp,cache,files,logs} готовы"

# --- Конфигурация -----------------------------------------------------------------
NEEDS_EDIT=0

if [ ! -f .env ]; then
    cp .env.example .env
    chown "${TARGET_UID}:${TARGET_GID}" .env
    chmod 600 .env
    # Подставляем реальные UID/GID сразу.
    sed -i "s/^APP_UID=.*/APP_UID=${TARGET_UID}/" .env
    sed -i "s/^APP_GID=.*/APP_GID=${TARGET_GID}/" .env
    warn ".env создан из примера — заполни BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH"
    NEEDS_EDIT=1
else
    ok ".env уже существует, не трогаю"
fi

if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    chown "${TARGET_UID}:${TARGET_GID}" config.yaml
    warn "config.yaml создан из примера — заполни allowed_user_ids и links.public_base_url"
    NEEDS_EDIT=1
else
    ok "config.yaml уже существует, не трогаю"
fi

# --- Сборка --------------------------------------------------------------------------
if [ "${NEEDS_EDIT}" -eq 1 ]; then
    cat <<'EOF'

────────────────────────────────────────────────────────────────────────
  Осталось заполнить два файла, потом запустить бот.

  1) .env
       BOT_TOKEN          — токен от @BotFather
       TELEGRAM_API_ID    — с https://my.telegram.org → API development tools
       TELEGRAM_API_HASH  — оттуда же

  2) config.yaml
       telegram.allowed_user_ids  — твой user_id (узнать: @userinfobot)
       links.public_base_url      — http://<домен или IP этого сервера>

  Затем:
       docker compose build
       docker compose up -d
       docker compose logs -f bot
────────────────────────────────────────────────────────────────────────

EOF
    exit 0
fi

say "Собираю образ (первый раз это 3–7 минут)"
docker compose build

say "Проверяю итоговую конфигурацию compose"
docker compose config >/dev/null
ok "Конфигурация корректна"

cat <<'EOF'

────────────────────────────────────────────────────────────────────────
  Готово. Запуск:

      docker compose up -d
      docker compose logs -f bot

  Проверка: напиши боту /start, затем пришли ссылку на YouTube.
────────────────────────────────────────────────────────────────────────

EOF
