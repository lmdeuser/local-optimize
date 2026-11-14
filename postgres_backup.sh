#!/bin/bash
# ==============================================================================
# Динамический скрипт резервного копирования баз данных PostgreSQL
# Защищен от гонки процессов (flock) и ограничен недельной глубиной (7 копий)
# ==============================================================================
set -euo pipefail
shopt -s failglob

CURRENT_TEMP_FILE=""

resolve_lock_file() {
    if [[ -n "${LOCKFILE:-}" ]]; then
        printf '%s\n' "${LOCKFILE}"
    elif [[ -n "${LOCK_FILE:-}" ]]; then
        printf '%s\n' "${LOCK_FILE}"
    elif [[ -n "${RUNTIME_DIRECTORY:-}" ]]; then
        printf '%s/postgres-backup.lock\n' "${RUNTIME_DIRECTORY}"
    elif [[ -d /run/lock && -w /run/lock ]]; then
        printf '%s\n' "/run/lock/local-optimize-postgres-backup.lock"
    else
        printf '%s/local-optimize-postgres-backup-%s.lock\n' "${TMPDIR:-/tmp}" "$(id -u)"
    fi
}

cleanup_temp() {
    if [[ -n "${CURRENT_TEMP_FILE:-}" ]]; then
        rm -f -- "${CURRENT_TEMP_FILE}"
    fi
}

rotate_db_backups() {
    local backup_dir="$1"
    local db_name="$2"
    local max_backups="$3"
    local count=0
    local _timestamp=""
    local old_file=""

    while IFS=$'\t' read -r -d '' _timestamp old_file; do
        count=$((count + 1))
        if (( count > max_backups )); then
            rm -f -- "${old_file}"
        fi
    done < <(
        find "${backup_dir}" -maxdepth 1 -type f -name "${db_name}_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_*.dump" -printf '%T@\t%p\0' \
            | sort -z -rn -k1,1
    )
}

main() {
    export LC_ALL=C
    umask 077

    local backup_dir="${1:-${BACKUP_DIR:-/var/backups/local-optimize/postgres}}"
    local retention_days="${RETENTION_DAYS:-7}"
    local max_backups_per_db="${MAX_BACKUPS_PER_DB:-7}"
    local lock_file=""
    local backup_date=""
    local -a databases=()

    if [[ ! "${retention_days}" =~ ^[0-9]+$ ]] || [[ ! "${max_backups_per_db}" =~ ^[0-9]+$ ]] || (( retention_days < 1 || max_backups_per_db < 1 )); then
        echo "Ошибка: RETENTION_DAYS и MAX_BACKUPS_PER_DB должны быть целыми положительными числами (>= 1)." >&2
        exit 2
    fi

    if [[ -L "${backup_dir}" ]]; then
        echo "Ошибка: директория резервных копий не должна быть символической ссылкой: ${backup_dir}" >&2
        exit 1
    fi

    lock_file="$(resolve_lock_file)"
    exec 200>"${lock_file}"
    if ! flock -n 200; then
        echo "Другой процесс бэкапа уже выполняется. Завершение работы." >&2
        exit 0
    fi

    backup_date="$(date +%Y-%m-%d_%H%M%S)"

    if ! pg_isready -q; then
        echo "Ошибка: PostgreSQL недоступен для создания резервной копии." >&2
        exit 1
    fi

    mkdir -p "${backup_dir}"
    chmod 0700 "${backup_dir}"

    mapfile -t databases < <(
        psql -w -tAc "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1') AND datistemplate = false;"
    )

    if (( ${#databases[@]} == 0 )); then
        echo "Не найдено пользовательских баз данных для резервного копирования." >&2
        exit 1
    fi

    trap cleanup_temp EXIT INT TERM

    local db=""
    local backup_file=""
    local temp_file=""
    for db in "${databases[@]}"; do
        [[ -n "${db}" ]] || continue
        if [[ "${db}" == */* ]]; then
            echo "Ошибка: недопустимое имя базы данных: ${db}" >&2
            exit 1
        fi
        backup_file="${backup_dir}/${db}_${backup_date}.dump"
        temp_file="$(mktemp "${backup_dir}/.${db}_${backup_date}.dump.tmp.XXXXXX")"
        CURRENT_TEMP_FILE="${temp_file}"
        chmod 0600 "${temp_file}"

        echo "Резервное копирование БД: ${db} -> ${backup_file}" >&2
        pg_dump -w -Fc -- "${db}" > "${temp_file}"
        if [[ ! -s "${temp_file}" ]] || ! pg_restore --list "${temp_file}" >/dev/null; then
            echo "Ошибка: проверка резервной копии БД ${db} не пройдена." >&2
            exit 1
        fi
        sync -f "${temp_file}"
        mv -f -- "${temp_file}" "${backup_file}"
        CURRENT_TEMP_FILE=""
        chmod 0600 "${backup_file}"
        sync -f "${backup_dir}"

        rotate_db_backups "${backup_dir}" "${db}" "${max_backups_per_db}"
    done

    find "${backup_dir}" -maxdepth 1 -type f -name "*.dump" -mtime +"${retention_days}" -delete
    echo "Резервное копирование успешно завершено (сохранено до ${max_backups_per_db} свежих бэкапов на каждую БД). Хранилище: ${backup_dir}" >&2
}

main "$@"
