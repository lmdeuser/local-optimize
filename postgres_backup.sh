#!/bin/bash
# ==============================================================================
# Динамический скрипт резервного копирования баз данных PostgreSQL
# Защищен от гонки процессов (flock) и ограничен недельной глубиной (7 копий)
# ==============================================================================
set -euo pipefail

# Защита от параллельного запуска нескольких инстансов бэкапа (flock)
LOCKFILE="${LOCKFILE:-/var/run/postgres-backup.lock}"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "⚠️ Другой процесс бэкапа уже выполняется. Завершение работы."
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${1:-${BACKUP_DIR:-/home/backups/postgres}}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
MAX_BACKUPS_PER_DB="${MAX_BACKUPS_PER_DB:-7}"
DATE=$(date +%Y-%m-%d_%H%M%S)

# Проверка готовности PostgreSQL к приему подключений
if ! pg_isready -q; then
    echo "❌ Ошибка: PostgreSQL недоступен для создания резервной копии."
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

# Получение списка всех пользовательских баз данных
DATABASES=$(sudo -u postgres psql -tAc "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1') AND datistemplate = false;")

for DB in ${DATABASES}; do
    BACKUP_FILE="${BACKUP_DIR}/${DB}_${DATE}.dump"
    TEMP_FILE=$(mktemp "${BACKUP_DIR}/.${DB}_${DATE}.dump.tmp.XXXXXX")
    chmod 0600 "${TEMP_FILE}"
    cleanup_temp() {
        rm -f -- "${TEMP_FILE}"
    }
    trap cleanup_temp EXIT INT TERM
    echo "📦 Резервное копирование БД: ${DB} -> ${BACKUP_FILE}"
    sudo -u postgres pg_dump -Fc "${DB}" > "${TEMP_FILE}"
    if [[ ! -s "${TEMP_FILE}" ]] || ! pg_restore --list "${TEMP_FILE}" >/dev/null; then
        echo "❌ Ошибка: проверка резервной копии БД ${DB} не пройдена."
        exit 1
    fi
    sync -f "${TEMP_FILE}"
    mv -f -- "${TEMP_FILE}" "${BACKUP_FILE}"
    sync -f "${BACKUP_DIR}"
    trap - EXIT INT TERM

    # Ограничение по количеству: оставляем строго не более 7 последних бэкапов для каждой базы
    ls -t "${BACKUP_DIR}/${DB}_"*.dump 2>/dev/null | tail -n +$((MAX_BACKUPS_PER_DB + 1)) | xargs -r rm -f
done

# Очистка по возрасту (старше 7 дней)
find "${BACKUP_DIR}" -type f -name "*.dump" -mtime +"${RETENTION_DAYS}" -delete
echo "✅ Резервное копирование успешно завершено (сохранено до ${MAX_BACKUPS_PER_DB} свежих бэкапов на каждую БД). Хранилище: ${BACKUP_DIR}"
