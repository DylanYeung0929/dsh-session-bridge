#!/usr/bin/env bash
# Follow one session live, then Ctrl-C to stop.
#   ./examples/watch-live.sh "安装插件"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/../tools/dshctl.py" watch "${1:?usage: watch-live.sh <session>}" --lines 3
