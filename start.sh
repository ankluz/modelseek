#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MODELSEEK_PORT:-8088}"
export MODELSEEK_PORT="$PORT"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Нужен Python 3.10 или новее. Debian/Ubuntu: sudo apt install python3 python3-venv; Arch/CachyOS: sudo pacman -S python" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[modelseek] Создаю окружение .venv ($PY)…"
  if ! "$PY" -m venv .venv; then
    echo "Не удалось создать .venv. Debian/Ubuntu: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

echo "[modelseek] Проверяю зависимости…"
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

URL="http://127.0.0.1:${PORT}"
echo "[modelseek] ${URL}   (Ctrl+C — остановить)"
if [ "${MODELSEEK_NO_BROWSER:-0}" != "1" ] && command -v xdg-open >/dev/null 2>&1; then
  (sleep 3 && xdg-open "$URL" >/dev/null 2>&1 || true) &
fi
exec .venv/bin/python -m app.serve
