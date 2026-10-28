#!/usr/bin/env bash
# 极简「带菜单的跨会话聊天」循环（真正的 chat 子命令请用 dshctl.py chat）。
# 用途：演示如何在别的语言/脚本里复用同一套状态与菜单协议。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CTL="$HERE/../tools/dshctl.py"

SESSION="${1:?用法: chat-loop.sh <会话>}"
"$CTL" focus "$SESSION" >/dev/null

while true; do
  printf '1继续 2切换 3上下文 4退出\n你 > '
  read -r line || exit 0
  case "$line" in
    4|q|quit|exit|退出) "$CTL" focus - >/dev/null; echo "已退出"; exit 0 ;;
    3|上下文) target="$("$CTL" where | python3 -c 'import json,sys;print(json.load(sys.stdin)["sessionId"])')"
              "$CTL" tail "$target" 6 ;;
    2|切换)   "$CTL" list; printf '新会话（标题或 uuid）> '; read -r target
              "$CTL" focus "$target" ;;
    *)        target="$("$CTL" where | python3 -c 'import json,sys;print(json.load(sys.stdin)["sessionId"])')"
              "$CTL" prompt "$target" "${line:-继续}" --wait 120 >/dev/null
              "$CTL" tail "$target" 1 --json | python3 -c '
import json, sys
msgs = json.load(sys.stdin)["messages"]
print("它 > " + (msgs[-1]["text"][:800] if msgs else "(这一轮还没有文本)"))' ;;
  esac
done
