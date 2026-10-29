#!/usr/bin/env bash
# Instalación de Trading Sim en una Raspberry Pi (Raspberry Pi OS Lite 64 bits).
# Se ejecuta EN LA RASPBERRY, desde la carpeta del proyecto:  bash pi/install.sh
# Es idempotente: se puede repetir sin romper nada (actualiza dependencias y servicio).
set -euo pipefail
PROJ="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
PORT="${PORT:-8777}"

echo "== 1/5 Paquetes del sistema =="
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip libopenblas0 git rsync avahi-daemon > /dev/null
# avahi: permite entrar desde el portátil como http://<nombre>.local:8777

echo "== 2/5 Entorno de Python =="
if [ ! -x "$PROJ/.venv/bin/python" ]; then
  python3 -m venv "$PROJ/.venv"
fi
"$PROJ/.venv/bin/pip" install --quiet --upgrade pip
# numpy/pandas traen ruedas precompiladas para arm64; no hace falta compilar
"$PROJ/.venv/bin/pip" install --quiet -r "$PROJ/requirements.txt"

echo "== 3/5 Comprobación =="
cd "$PROJ"
"$PROJ/.venv/bin/python" -c "import backend.app; print('   backend importa correctamente')"

echo "== 4/5 Servicio del sistema (arranca solo al encender) =="
sudo tee /etc/systemd/system/trading-sim.service > /dev/null <<UNIT
[Unit]
Description=Trading Sim - paper-trading cuantitativo (Raspberry)
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJ
Environment=PYTHONUNBUFFERED=1
# 0.0.0.0: accesible desde la red local (el panel NO tiene contraseña: no abrir el puerto al exterior)
ExecStart=$PROJ/.venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
# apagado limpio: el bot cierra la cartera (close_on_shutdown) antes de morir
KillSignal=SIGINT
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable trading-sim.service > /dev/null
sudo systemctl restart trading-sim.service

echo "== 5/5 Copia diaria de seguridad de los datos (data/ -> ~/trading-sim-backups) =="
mkdir -p "$HOME/trading-sim-backups"
( crontab -l 2>/dev/null | grep -v trading-sim-backup ; echo "17 4 * * * tar czf \$HOME/trading-sim-backups/data-\$(date +\\%F).tgz -C $PROJ data && find \$HOME/trading-sim-backups -name 'data-*.tgz' -mtime +30 -delete # trading-sim-backup" ) | crontab -

sleep 3
if systemctl is-active --quiet trading-sim.service; then
  IP=$(hostname -I | awk '{print $1}')
  echo
  echo "✅ Trading Sim en marcha."
  echo "   Panel:   http://$IP:$PORT   o   http://$(hostname).local:$PORT"
  echo "   Estado:  systemctl status trading-sim"
  echo "   Logs:    journalctl -u trading-sim -f"
else
  echo "❌ El servicio no arrancó. Mira: journalctl -u trading-sim -n 50"
  exit 1
fi
