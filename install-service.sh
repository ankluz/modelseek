#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

if [ ! -x .venv/bin/python ]; then
  echo "Сначала запустите ./start.sh — он создаёт окружение .venv." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd не найден — запускайте ./start.sh вручную." >&2
  exit 1
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/modelseek.service" <<EOF
[Unit]
Description=modelseek — локальный поиск 3D-моделей
After=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=-$DIR/.env
Environment=MODELSEEK_PORT=${MODELSEEK_PORT:-8088}
ExecStart=$DIR/.venv/bin/python -m app.serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now modelseek.service
echo "Готово: modelseek запускается при входе в систему. http://127.0.0.1:${MODELSEEK_PORT:-8088}"
echo "Статус: systemctl --user status modelseek   Логи: journalctl --user -u modelseek -f"
echo "Чтобы работал и без входа в систему: sudo loginctl enable-linger $USER"
