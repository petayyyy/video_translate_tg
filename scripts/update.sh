#!/usr/bin/env bash
#
# Обновление video_tg.
#
# Три режима:
#   ./scripts/update.sh            — обновить только yt-dlp (быстро, ~30 секунд)
#   ./scripts/update.sh --full     — пересобрать образ целиком и перезапустить
#   ./scripts/update.sh --vot X.Y.Z — поставить конкретную версию vot-cli
#
# Про yt-dlp отдельно: площадки ломают парсинг заметно чаще, чем выходят
# релизы всего остального. Девять из десяти проблем «видео не качается»
# лечатся именно обновлением yt-dlp, поэтому это режим по умолчанию.
#
# Скрипт дожидается завершения текущей задачи: перезапуск идёт через
# docker compose up -d, а у сервиса bot выставлен stop_grace_period.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; OFF='\033[0m'
say()  { printf "${BOLD}==>${OFF} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✓${OFF} %s\n" "$*"; }
warn() { printf "${YELLOW}  !${OFF} %s\n" "$*"; }
die()  { printf "${RED}  ✗ %s${OFF}\n" "$*" >&2; exit 1; }

MODE="ytdlp"
VOT_VERSION=""

while [ $# -gt 0 ]; do
    case "$1" in
        --full)   MODE="full"; shift ;;
        --ytdlp)  MODE="ytdlp"; shift ;;
        --vot)    MODE="full"; VOT_VERSION="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "Неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

command -v docker >/dev/null 2>&1 || die "docker не найден"
[ -f docker-compose.yml ] || die "docker-compose.yml не найден"

# --- Показываем, что сейчас в работе ---------------------------------------
if docker compose ps --status running --services 2>/dev/null | grep -qx bot; then
    say "Текущее состояние очереди"
    docker compose exec -T bot python -c "
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5) as response:
        data = json.load(response)
    print(f\"  в очереди задач: {data.get('queue', '?')}\")
except Exception as error:
    print(f'  не удалось опросить бота: {error}')
" || warn "не смог опросить бота — продолжаю"
fi

case "${MODE}" in

    ytdlp)
        say "Обновляю yt-dlp внутри работающего контейнера"
        BEFORE="$(docker compose exec -T bot yt-dlp --version 2>/dev/null || echo '?')"
        docker compose exec -T bot pip install --no-cache-dir --upgrade yt-dlp \
            || die "не удалось обновить yt-dlp (контейнер запущен?)"
        AFTER="$(docker compose exec -T bot yt-dlp --version 2>/dev/null || echo '?')"

        if [ "${BEFORE}" = "${AFTER}" ]; then
            ok "yt-dlp уже последней версии: ${AFTER}"
            exit 0
        fi

        ok "yt-dlp: ${BEFORE} → ${AFTER}"
        warn "Обновление внутри контейнера пропадёт при пересборке образа."
        warn "Чтобы закрепить: впиши yt-dlp==${AFTER} в requirements.txt"
        warn "и выполни ./scripts/update.sh --full"

        say "Перезапускаю бот, чтобы новая версия точно подхватилась"
        docker compose restart bot
        ok "готово"
        ;;

    full)
        if [ -n "${VOT_VERSION}" ]; then
            say "Ставлю vot-cli ${VOT_VERSION}"
            if grep -q '^VOT_CLI_VERSION=' .env 2>/dev/null; then
                sed -i "s/^VOT_CLI_VERSION=.*/VOT_CLI_VERSION=${VOT_VERSION}/" .env
            else
                echo "VOT_CLI_VERSION=${VOT_VERSION}" >> .env
            fi
            ok "VOT_CLI_VERSION=${VOT_VERSION} записан в .env"
        fi

        say "Обновляю базовые образы (telegram-bot-api, nginx)"
        docker compose pull --ignore-buildable || warn "часть образов подтянуть не удалось"

        say "Пересобираю образ бота"
        docker compose build --pull bot

        say "Перезапускаю (текущая задача будет доведена до конца)"
        docker compose up -d

        say "Версии в новом образе"
        sleep 5
        docker compose exec -T bot sh -c '
            printf "  python : "; python --version 2>&1 | cut -d" " -f2
            printf "  yt-dlp : "; yt-dlp --version
            printf "  ffmpeg : "; ffmpeg -version 2>/dev/null | head -1 | cut -d" " -f3
            printf "  vot-cli: "; vot-cli --version 2>&1 | head -1
        ' || warn "контейнер ещё поднимается, проверь позже"

        say "Убираю старые образы"
        docker image prune -f >/dev/null
        ok "готово"
        ;;
esac

cat <<'EOF'

Логи:      docker compose logs -f bot
Состояние: docker compose ps
EOF
