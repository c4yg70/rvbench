#!/usr/bin/env bash
# Rust evald control. Python belongs to the tensor bridge selected by run_env.sh.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/evald.pid"
LOG="$DIR/evald.log"
[ ! -f "$DIR/evald.env" ] || . "$DIR/evald.env"
BIN="${EVALD_BIN:-$DIR/rvbench-evald}"
CONFIG="${EVALD_CONFIG:-$DIR/config.json}"
PORT="$("$BIN" --config "$CONFIG" --print-port)"

pid_alive() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
  if pid_alive; then
    echo "evald already running (pid $(cat "$PID_FILE"))"
    return 0
  fi
  if [ "${1:-normal}" = ai ]; then
    setsid nohup sh -c 'echo "$$" > /proc/set_ai_thread && exec "$1" --config "$2"' sh "$BIN" "$CONFIG" >> "$LOG" 2>&1 &
  else
    setsid nohup "$BIN" --config "$CONFIG" >> "$LOG" 2>&1 &
  fi
  echo "$!" > "$PID_FILE"
  sleep 1
  status
}

stop() {
  if ! pid_alive; then
    rm -f "$PID_FILE"
    echo "evald stopped"
    return 0
  fi
  kill "$(cat "$PID_FILE")"
  # Rust stops admitting work and waits for the active benchmark to restore.
  # Keep the pidfile when a long benchmark has not finished shutting down.
  for _ in $(seq 1 60); do
    if ! pid_alive; then
      rm -f "$PID_FILE"
      echo "evald stopped"
      return 0
    fi
    sleep 1
  done
  echo "evald is still completing cleanup; pidfile retained" >&2
  return 1
}

status() {
  if pid_alive; then
    echo "process: running (pid $(cat "$PID_FILE"))"
    curl -s --max-time 3 "http://127.0.0.1:$PORT/healthz" || echo "healthz unavailable"
    echo
  else
    echo "process: stopped"
  fi
}

case "${1:-}" in
  start) start ;;
  start-ai) start ai ;;
  stop) stop ;;
  restart) stop && start ;;
  restart-ai) stop && start ai ;;
  status) status ;;
  logs) tail -n 200 -f "$LOG" ;;
  *) echo "usage: $0 start|start-ai|stop|restart|restart-ai|status|logs"; exit 1 ;;
esac
