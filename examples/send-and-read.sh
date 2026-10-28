#!/usr/bin/env bash
# 最小示例：给某个会话发一句话，等它跑完，再把最后一条回复打出来。
# 用法: ./send-and-read.sh "安装插件" "用三行汇报进度"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CTL="$HERE/../tools/dshctl.py"

SESSION="${1:?用法: send-and-read.sh <会话标题或 session-uuid> <要发的话>}"
MESSAGE="${2:?缺少消息内容}"

"$CTL" prompt "$SESSION" "$MESSAGE" --wait 120
echo "—— 最新回复 ——"
"$CTL" tail "$SESSION" 1
