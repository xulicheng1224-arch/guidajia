#!/bin/zsh
set -e
cd "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v node >/dev/null; then
  node_bins=("$HOME"/.nvm/versions/node/*/bin(N))
  if (( ${#node_bins} )); then export PATH="${node_bins[-1]}:$PATH"; fi
fi
if ! command -v python3 >/dev/null; then
  print '请先安装 Python 3.10+：https://www.python.org/downloads/macos/'
  read '?按 Enter 退出'; exit 1
fi
if ! command -v npm >/dev/null; then
  print '请先安装 Node.js 20.19+：https://nodejs.org/'
  read '?按 Enter 退出'; exit 1
fi
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if [[ ! -f .venv/.monitor-installed ]] || [[ requirements.txt -nt .venv/.monitor-installed ]]; then
  .venv/bin/python -m pip install --index-url https://pypi.org/simple -r requirements.txt
  touch .venv/.monitor-installed
fi
if [[ ! -d node_modules ]] || [[ ! -f node_modules/.monitor-installed ]] || [[ package-lock.json -nt node_modules/.monitor-installed ]]; then
  npm ci --no-audit --no-fund
  touch node_modules/.monitor-installed
fi
if [[ ! -f dist/index.html ]] || [[ package-lock.json -nt dist/index.html ]] || [[ -n "$(find frontend -type f -newer dist/index.html -print -quit 2>/dev/null)" ]]; then npm run build; fi
.venv/bin/python run.py
