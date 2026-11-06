#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Сначала запустите ./start.sh — он создаёт окружение .venv." >&2
  exit 1
fi

echo "[modelseek] Установка дополнительных модулей…"
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-extra.txt

echo "[modelseek] Скачивание Chromium для режима browser (~150 МБ)…"
.venv/bin/python -m playwright install chromium

if command -v apt-get >/dev/null 2>&1; then
  echo
  echo "Если Chromium не запустится, поставьте системные библиотеки:"
  echo "  sudo .venv/bin/python -m playwright install-deps chromium"
elif command -v pacman >/dev/null 2>&1; then
  echo
  echo "Если Chromium не запустится, поставьте системные библиотеки:"
  echo "  sudo pacman -S --needed nss atk at-spi2-core libcups libdrm libxkbcommon libxcomposite libxdamage libxrandr mesa pango alsa-lib"
fi

echo
echo "Готово. Включите нужное в Настройки → Источники / Перевод и смысл (перезапуск не нужен)."
