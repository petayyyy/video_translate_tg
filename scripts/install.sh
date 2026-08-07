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

# Владельцем данных не должен становиться root: контейнер тогда работал бы
# от root, а сборка образа падала бы на попытке создать группу с GID 0,
# которая уже существует. Подставляем непривилегированный UID.
UNPRIVILEGED_UID=1000
UNPRIVILEGED_GID=1000
RAN_AS_ROOT=0
if [ "${TARGET_UID}" -eq 0 ]; then
    RAN_AS_ROOT=1
    TARGET_UID="${UNPRIVILEGED_UID}"
    TARGET_GID="${UNPRIVILEGED_GID}"
fi

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
if [ "${RAN_AS_ROOT}" -eq 1 ]; then
    say "Владелец данных: ${TARGET_UID}:${TARGET_GID} (непривилегированный)"
    warn "Скрипт запущен от root напрямую. Данные и контейнер намеренно"
    warn "переводятся на UID ${TARGET_UID}: контейнер не должен работать от root,"
    warn "а сборка образа с APP_UID=0 упала бы на создании группы."
    warn "Root на хосте по-прежнему имеет полный доступ к ./data."
else
    say "Владелец данных: ${TARGET_USER} (${TARGET_UID}:${TARGET_GID})"
fi

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
    say "Порты, занятые прямо сейчас (выбирай HTTP_PORT из свободных)"
    ss -tuln 2>/dev/null | awk 'NR>1 {print $5}' | grep -oE '[0-9]+$' \
        | sort -un | tr '\n' ' ' | sed 's/^/    занято: /'
    echo

    cat <<'EOF'

────────────────────────────────────────────────────────────────────────
  Осталось заполнить два файла, потом запустить бот.

  1) .env
       BOT_TOKEN          — токен от @BotFather
       TELEGRAM_API_ID    — с https://my.telegram.org → API development tools
       TELEGRAM_API_HASH  — оттуда же
       HTTP_PORT          — свободный порт для раздачи больших файлов
                            (значения по умолчанию нет: на занятом сервере
                             порт 80 обычно уже кем-то используется)

  2) config.yaml
       telegram.allowed_user_ids  — твой user_id (узнать: @userinfobot)
       links.public_base_url      — http://<адрес сервера>:<тот же HTTP_PORT>

  3) Открыть выбранный порт в файрволе:
       sudo ufw allow <HTTP_PORT>/tcp

  Затем:
       docker compose build
       docker compose up -d
       docker compose logs -f bot
────────────────────────────────────────────────────────────────────────

EOF
    exit 0
fi

# --- Проверка, что обязательные переменные заполнены ---------------------------
missing=""
for var in BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH HTTP_PORT; do
    value="$(grep -E "^${var}=" .env 2>/dev/null | head -1 | cut -d= -f2-)"
    case "${value}" in
        ""|123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA|1234567|0123456789abcdef0123456789abcdef)
            missing="${missing} ${var}" ;;
    esac
done

if [ -n "${missing}" ]; then
    die "в .env не заполнено:${missing}
     Открой .env, подставь значения и запусти скрипт снова."
fi
ok ".env заполнен"

HTTP_PORT_VALUE="$(grep -E '^HTTP_PORT=' .env | head -1 | cut -d= -f2-)"
if ss -tuln 2>/dev/null | grep -qE ":${HTTP_PORT_VALUE}\b"; then
    die "порт ${HTTP_PORT_VALUE} уже занят другим сервисом.
     Посмотри свободные (ss -tuln), выбери другой и поправь HTTP_PORT в .env
     и links.public_base_url в config.yaml."
fi
ok "порт ${HTTP_PORT_VALUE} свободен"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    if ufw status 2>/dev/null | grep -qE "^${HTTP_PORT_VALUE}[/ ]"; then
        ok "порт ${HTTP_PORT_VALUE} открыт в ufw"
    else
        warn "порт ${HTTP_PORT_VALUE} НЕ открыт в ufw — ссылки на большие файлы"
        warn "не будут работать снаружи. Открыть: sudo ufw allow ${HTTP_PORT_VALUE}/tcp"
    fi
fi

# --- Swap: без него сборка на слабой машине может упасть -------------------------
total_ram_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
swap_mb="$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)"
if [ "${total_ram_mb}" -lt 2048 ] && [ "${swap_mb}" -lt 512 ]; then
    warn "RAM ${total_ram_mb} МБ, swap ${swap_mb} МБ — сборка образа может упасть по нехватке памяти."
    warn "Настоятельно рекомендую добавить swap:"
    warn "  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile \\"
    warn "    && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab"
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
