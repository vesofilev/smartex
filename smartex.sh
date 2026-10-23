#!/usr/bin/env bash
# Start, stop or inspect the smartex server.
#   ./smartex.sh start DIR [DIR ...]   serve these LaTeX project folders (default port 8765, or $SMARTEX_PORT)
#   ./smartex.sh stop | status | url
# Settings can live in ~/.smartex.env (shell syntax), so restarts keep them:
#   SMARTEX_PORT=8765                      the loopback port (SSH tunnel, CLI, MCP)
#   SMARTEX_PUBLIC=0.0.0.0:5005            optional second listener for a reverse proxy such as nginx
#   SMARTEX_ALLOW_FROM="192.0.2.10"        addresses or networks that listener accepts (space separated)
#   SMARTEX_PUBLIC_URL=https://host/smartex/   where the proxy serves it, for `url` to print
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HOME/.smartex.env" ] && . "$HOME/.smartex.env"
PORT="${SMARTEX_PORT:-8765}"
PIDFILE="$HOME/.smartex.pid"
LOG="$HOME/.smartex.log"
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
case "${1:-}" in
  start)
    shift; [ $# -ge 1 ] || { echo "usage: $0 start DIR [DIR ...]"; exit 1; }
    running && { echo "already running (pid $(cat "$PIDFILE"))"; exit 0; }
    args=(); for d in "$@"; do args+=(--root "$d"); done
    if [ -n "${SMARTEX_PUBLIC:-}" ]; then
      args+=(--public "$SMARTEX_PUBLIC"); for a in ${SMARTEX_ALLOW_FROM:-}; do args+=(--allow-from "$a"); done
    fi
    [ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"          # keep the previous run's log
    nohup python3 "$HERE/server.py" "${args[@]}" --port "$PORT" > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"; sleep 1
    running && grep -m1 '^open:' "$LOG" | sed "s/^open: /started; open (through your SSH tunnel): /" || { echo "failed to start:"; cat "$LOG"; exit 1; } ;;
  stop)
    if running; then
      pid="$(cat "$PIDFILE")"; kill "$pid"
      for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done   # wait until the port is free
      kill -0 "$pid" 2>/dev/null && { echo "still stopping (pid $pid); try again in a moment"; exit 1; }
      rm -f "$PIDFILE"; echo stopped
    else echo "not running"; fi ;;
  status) running && echo "running (pid $(cat "$PIDFILE")) on 127.0.0.1:$PORT${SMARTEX_PUBLIC:+ and $SMARTEX_PUBLIC (proxy)}" || echo "not running" ;;
  url)    echo "http://localhost:$PORT/?token=$(cat "$HOME/.smartex_token")    (through your SSH tunnel)"
          [ -n "${SMARTEX_PUBLIC_URL:-}" ] && echo "${SMARTEX_PUBLIC_URL}    (sign in with the token in ~/.smartex_token)"
          true ;;
  *)      sed -n '2,5p' "$0"; exit 1 ;;
esac
