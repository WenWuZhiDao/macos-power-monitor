#!/usr/bin/env bash
# 停止充电功率监控
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/data/monitor.pid"

if [ ! -f "$PIDFILE" ]; then
  echo "未在运行（无 PID 文件）。"
  # 兜底：清理可能残留的进程
  if pkill -f "power_monitor.py" 2>/dev/null; then
    echo "已结束残留的 power_monitor.py 进程。"
  fi
  exit 0
fi

PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID" 2>/dev/null || true
  # 最多等待约 3 秒优雅退出
  for _ in $(seq 1 10); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.3
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "🛑 已停止 power-monitor (PID $PID)"
else
  echo "进程不存在 (PID $PID)，清理 PID 文件。"
fi

rm -f "$PIDFILE"
