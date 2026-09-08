#!/usr/bin/env bash
# evaldctl.sh — evald 的守护启停（板上运行；setsid+nohup+pidfile，无交互终端依赖，适合 @reboot）
# 用法: evaldctl.sh start|start-ai|stop|restart|status|logs
#   start-ai: A100 测量簇迁移（ADR-0012）——包装 shell 先把自己的 TID 写
#   /proc/set_ai_thread 再 exec evald（execve 前注册是唯一安全姿势，运行中
#   跨簇迁移有 segfault 实证），evald 进程树整簇绑定 cpu8-15。
#   回退: stop 后用 start（X100 默认调度）即可。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/evald.pid"
LOG="$DIR/evald.log"
PORT="$(python3 -c "import json;print(json.load(open('$DIR/config.json'))['port'])")"

pid_alive() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_ai() {
  start_common \
    "sh -c \"echo \$\$ > /proc/set_ai_thread && exec python3 '$DIR/evald.py' --config '$DIR/config.json'\"" \
    "ai"
}

# start_common <inner-cmd> <tag>: setsid nohup 拉起 <inner-cmd>（原 start 的公共骨架）。
# A100 模式下 $! 即包装 sh 的 PID：setsid 无 fork（脚本后台子进程非进程组长），
# $$ 注册后 exec python3 保持同 PID，pidfile/stop 语义与原 start 完全一致。
start_common() {
  local inner="$1"
  if pid_alive; then
    echo "evald 已在运行 (pid $(cat "$PID_FILE"))"
    return 0
  fi
  rm -f "$PID_FILE"
  setsid nohup bash -c "$inner" >> "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  status
  if pid_alive; then
    awk '/Cpus_allowed_list/{print "affinity: " $2}' "/proc/$(cat "$PID_FILE")/status"
  fi
}

start() {
  if pid_alive; then
    echo "evald 已在运行 (pid $(cat "$PID_FILE"))"
    return 0
  fi
  rm -f "$PID_FILE"
  setsid nohup python3 "$DIR/evald.py" --config "$DIR/config.json" >> "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  status
}

stop() {
  if pid_alive; then
    kill "$(cat "$PID_FILE")" && echo "evald 已停止"
    rm -f "$PID_FILE"
  else
    echo "evald 未在运行"
    rm -f "$PID_FILE"
  fi
}

status() {
  if pid_alive; then
    echo "process: running (pid $(cat "$PID_FILE"))"
    curl -s --max-time 3 "http://127.0.0.1:$PORT/healthz" || echo "(healthz 无响应)"
    echo
  else
    echo "process: stopped"
  fi
}

logs() { tail -n 200 -f "$LOG"; }

case "${1:-}" in
  start) start ;;
  start-ai) start_ai ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) logs ;;
  *) echo "用法: $0 start|start-ai|stop|restart|status|logs"; exit 1 ;;
esac
