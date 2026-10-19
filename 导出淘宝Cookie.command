#!/bin/zsh
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null; then
  PYTHON="$(command -v python3)"
else
  print '未找到 Python 3，请先安装 Python 3.10 或更高版本。'
  read '?按 Enter 关闭此窗口。'
  exit 1
fi
"$PYTHON" "$ROOT/tools/export_taobao_cookie.py" "$@"
status=$?
if (( status == 0 )); then
  print ""
  read '?已完成，按 Enter 关闭此窗口。'
else
  print ""
  read '?导出未完成，按 Enter 关闭此窗口。'
fi
exit $status
