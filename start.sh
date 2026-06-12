#!/usr/bin/env bash
# 启动充电功率监控（后台运行）
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$DIR/data"
PIDFILE="$DATA_DIR/monitor.pid"
LOG="$DATA_DIR/monitor.log"
PORT="${PM_PORT:-8765}"
HOST="${PM_HOST:-127.0.0.1}"

mkdir -p "$DATA_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误: 未找到 python3。请先安装命令行工具: xcode-select --install" >&2
  exit 1
fi

# 已在运行则直接提示
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已在运行 (PID $(cat "$PIDFILE"))。打开 http://${HOST}:${PORT} 查看。"
  exit 0
fi

# 后台启动（PM_INTERVAL / PM_PORT 等环境变量会自动透传给程序）
nohup python3 "$DIR/power_monitor.py" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "✅ 已启动 power-monitor (PID $(cat "$PIDFILE"))"
  echo "   📈 实时曲线: http://${HOST}:${PORT}"
  echo "   📄 数据记录: $DATA_DIR/power.csv"
  echo "   🛑 停止命令: $DIR/stop.sh"
else
  echo "❌ 启动失败，最近日志如下：" >&2
  tail -n 20 "$LOG" >&2 || true
  rm -f "$PIDFILE"
  exit 1
fi
