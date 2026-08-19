#!/bin/sh
# niceclaude container entrypoint: start the poller, then run the container's
# real command, then shut the poller down cleanly on SIGTERM.
#
#   COPY deploy/docker-entrypoint.sh /usr/local/bin/
#   ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
#   CMD ["claude", "..."]
#
# `niceclaude install` registers the hook in ~/.claude/settings.json, so the
# command needs no --settings. Pass --settings <config>/settings.json instead if
# you want only this invocation hooked and nothing else in the image.
#
# POSIX sh only -- no bashisms, so it runs under dash/busybox ash.

set -eu

NICECLAUDE_BIN=${NICECLAUDE_BIN:-niceclaude}
NICECLAUDE_INTERVAL=${NICECLAUDE_INTERVAL:-60}
NICECLAUDE_DATA=${NICECLAUDE_DIR:-$HOME/.local/share/niceclaude}

daemon_pid=""
app_pid=""
status=0

log() { echo "entrypoint: $*" >&2; }

# Signal the PID directly rather than shelling out to `niceclaude stop`: this is
# our own child, so there is no process-name matching involved, and `stop` exits
# 0 whether or not it actually stopped anything (it reports "no daemon running"
# and succeeds), which makes its status useless as a fallback trigger.
stop_daemon() {
    [ -n "$daemon_pid" ] || return 0
    kill -TERM "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
    daemon_pid=""
}

# `docker stop` signals PID 1 only. Forward it to both children by hand -- this
# is exactly why the real command is NOT `exec`ed: exec would replace this shell
# and leave nothing to pass SIGTERM to the daemon, which would then be SIGKILLed
# mid-write at the end of the stop timeout.
on_term() {
    if [ -n "$app_pid" ]; then
        _pid=$app_pid
        kill -TERM "$_pid" 2>/dev/null || true
        # Collect the app's real exit status here: the interrupted `wait` in the
        # main loop can only report 128+SIGTERM, which would mask it.
        _rc=0
        wait "$_pid" || _rc=$?
        status=$_rc
        app_pid=""
    fi
    stop_daemon
}
trap on_term TERM INT HUP

[ "$#" -gt 0 ] || { log "no command given (set CMD, or pass one)"; exit 2; }

# A fresh container has a fresh PID namespace, so any PID recorded in the
# pidfile by a previous container is meaningless -- and if it happens to match a
# live PID here, `watch` refuses to start. Only clear it when we really are the
# container's init ($$ = 1); under `--pid=host` or a shell wrapper, leave it be.
if [ "$$" = "1" ] && [ -f "$NICECLAUDE_DATA/daemon.pid" ]; then
    rm -f "$NICECLAUDE_DATA/daemon.pid"
fi

"$NICECLAUDE_BIN" watch --interval "$NICECLAUDE_INTERVAL" &
daemon_pid=$!
log "niceclaude watch started (pid $daemon_pid, interval ${NICECLAUDE_INTERVAL}s)"

"$@" &
app_pid=$!

# A trapped signal interrupts `wait`, so loop until the app is really gone.
# on_term clears app_pid once it has the real status; don't overwrite it.
while [ -n "$app_pid" ] && kill -0 "$app_pid" 2>/dev/null; do
    rc=0
    wait "$app_pid" || rc=$?
    if [ -n "$app_pid" ]; then status=$rc; fi
done

stop_daemon
exit "$status"
