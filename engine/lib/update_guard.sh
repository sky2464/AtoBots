#!/usr/bin/env bash
# The update job's safety net. systemd runs it as the job's ExecStopPost, so
# it runs however the job ends: finished, failed, killed or out of time.
#
#   update_guard.sh PREFIX JOB [LOG]
#
# The job copies this file next to its log before it starts, so the guard
# does not depend on which engine version is checked out when it runs.
#
# What it does: any of the Admin service and the backup and health-check
# timers that is enabled but not running is started again, so the Owner can
# still reach Admin on Telegram. It does not start the dispatcher: when an
# update stops half way the code and the database may not match, and bots
# should not run on that. It says so on Telegram instead, with the log.
# When everything is up (the normal end of a job) it does nothing.
set -u

prefix=${1:?usage: update_guard.sh PREFIX JOB [LOG]}
job=${2:?usage: update_guard.sh PREFIX JOB [LOG]}
log=${3:-unknown}
result=${SERVICE_RESULT:-unknown}
systemctl=${CB_SYSTEMCTL:-systemctl}
cb=${CB_GUARD_CB:-${CB_USR_BIN_DIR:-/usr/local/bin}/cb}
priv=(sudo -n)
[ -z "${CB_NO_SUDO:-}" ] || priv=()

enabled() { "$systemctl" is-enabled --quiet "$1" >/dev/null 2>&1; }
active() { "$systemctl" is-active --quiet "$1" >/dev/null 2>&1; }

started=()
failed=()
for role in admin.service backup.timer doctor.timer; do
    unit=$prefix-$role
    enabled "$unit" || continue
    active "$unit" && continue
    if ${priv[@]+"${priv[@]}"} "$systemctl" start "$unit" >/dev/null 2>&1; then
        started+=("$unit")
    else
        failed+=("$unit")
    fi
done

dispatcher=$prefix-dispatcher.service
dispatcher_down=0
if enabled "$dispatcher" && ! active "$dispatcher"; then
    dispatcher_down=1
fi

if [ ${#started[@]} -eq 0 ] && [ ${#failed[@]} -eq 0 ] && [ "$dispatcher_down" -eq 0 ]; then
    echo "guard: the $job job ended ($result); every service is running"
    exit 0
fi

msg="AtoBots: the $job job ended ($result) and left services stopped."
if [ ${#started[@]} -gt 0 ]; then
    msg="$msg Started again: ${started[*]}."
fi
if [ ${#failed[@]} -gt 0 ]; then
    msg="$msg Could not start: ${failed[*]}."
fi
if [ "$dispatcher_down" -eq 1 ]; then
    msg="$msg The dispatcher is still stopped, so no bot runs until it is checked: ask Admin to read the end of the log and run cb doctor, or follow the steps there by hand."
fi
msg="$msg Log: $log"
echo "guard: $msg"
if [ -x "$cb" ]; then
    timeout 90 "$cb" admin notify "$msg" >/dev/null 2>&1 \
        || echo "guard: the Telegram notice was not sent"
else
    echo "guard: $cb is missing; the Telegram notice was not sent"
fi
exit 0
