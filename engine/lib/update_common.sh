# shellcheck shell=bash
# Steps shared by engine/update.sh and engine/adopt.sh: the first half of an
# update, done by the version that is installed now. Sourced before the code
# is switched; never executed.
# shellcheck source=sysfiles.sh
source "$(dirname "${BASH_SOURCE[0]}")/sysfiles.sh"
# shellcheck source=names.sh
source "$(dirname "${BASH_SOURCE[0]}")/names.sh"

# cb_find_home   Print the company home (the environment, then the env file).
cb_find_home() {
    local home
    home=$(printenv "$CB_HOME_VAR" 2>/dev/null || true)
    if [ -z "$home" ] && [ -r "$CB_ENV_FILE" ]; then
        home=$(sed -n "s/^$CB_HOME_VAR=//p" "$CB_ENV_FILE" | sed -n '1p')
    fi
    case $home in
        /*) ;;
        *) echo "error: $CB_HOME_VAR is not set to an absolute path" >&2; return 1 ;;
    esac
    if ! [[ $home =~ ^/[A-Za-z0-9._/-]+$ ]]; then
        echo "error: the company home may only use letters, digits, . _ - and /" >&2
        return 1
    fi
    echo "$home"
}

# cb_company_name DB
cb_company_name() {
    python3 -I -c '
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
row = conn.execute("SELECT value FROM meta WHERE key = ?", ("company",)).fetchone()
print(row[0] if row else "")
' "$1"
}

# cb_backup_db DB BACKUP   The database runs in WAL mode, so a plain copy
# could miss recent writes; SQLite's own backup is used.
cb_backup_db() {
    local db=$1 backup=$2
    mkdir -p "$(dirname "$backup")"
    chmod 700 "$(dirname "$backup")"
    python3 -I -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
' "$db" "$backup"
    chmod 600 "$backup"
    if [ ! -s "$backup" ]; then
        echo "error: the backup at $backup is empty" >&2
        return 1
    fi
}

# cb_restore_db BACKUP DB
cb_restore_db() {
    local backup=$1 db=$2
    [ -s "$backup" ] || return 1
    rm -f -- "$db-wal" "$db-shm"
    cp -- "$backup" "$db.restore"
    chmod 600 "$db.restore"
    mv -f -- "$db.restore" "$db"
}

# cb_wait_idle CB FORCE   Wait for bot runs to finish. Every call has a time
# limit, so a `cb` that hangs ends the wait with a message instead of wedging
# the job. Returns 1 when runs are still active and FORCE is 0.
cb_wait_idle() {
    local cb=$1 force=$2
    local limit=${CB_UPD_IDLE_SECONDS:-1800}
    local deadline=$((SECONDS + limit)) status
    echo "Waiting for bot runs to finish (up to $((limit / 60)) minutes) ..."
    while :; do
        status=0
        cb_timeout 60 "$cb" dispatcher idle || status=$?
        case $status in
            0) return 0 ;;
            1) ;;
            2) echo "this version cannot report idle; continuing"; return 0 ;;
            *) echo "warning: the idle check did not answer (exit $status)" >&2 ;;
        esac
        [ "$SECONDS" -lt "$deadline" ] || break
        sleep "${CB_UPD_IDLE_POLL:-10}"
    done
    echo "A bot run is still active:" >&2
    cb_timeout 60 "$cb" status >&2 || true
    if [ "$force" -eq 0 ]; then
        return 1
    fi
    echo "warning: continuing because --force was given" >&2
}

# cb_finish_status STATUS   What the second half's exit code means.
#   0 done, 10 failed and rolled back, 11 failed and the rollback failed.
# Anything else means the new version could not even run its second half,
# and the caller puts the old version back itself.
cb_finish_status() {
    case $1 in
        0) return 0 ;;
        10) return 10 ;;
        11) return 11 ;;
        *) return 99 ;;
    esac
}
