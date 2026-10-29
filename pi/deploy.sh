#!/usr/bin/env bash
# Se ejecuta EN EL PORTÁTIL. Envía el proyecto a la Raspberry y la administra por SSH.
#   pi/deploy.sh push       copia el proyecto (código + data) e instala/reinicia en la Pi
#   pi/deploy.sh cutover    traslado definitivo: para el bot aquí, copia TODO y arranca en la Pi
#   pi/deploy.sh pull-data  trae data/ de la Pi a ./data-pi (para analizar aquí)
#   pi/deploy.sh status | logs | restart | stop | start
# Configura el destino con la variable PI (por defecto pi@raspberrypi.local):
#   PI=german@192.168.1.50 pi/deploy.sh push
set -euo pipefail
PI="${PI:-pi@raspberrypi.local}"
REMOTE_DIR="${REMOTE_DIR:-trading-sim}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cmd="${1:-help}"

sync_code() {
  rsync -az --delete --info=progress2 \
    --exclude '.venv' --exclude '__pycache__' --exclude 'data/' --exclude 'data-pi/' \
    "$HERE/" "$PI:$REMOTE_DIR/"
}
sync_data() {   # config (con tu clave de IA), estado y base de datos
  rsync -az --info=progress2 --exclude 'backup-*' "$HERE/data/" "$PI:$REMOTE_DIR/data/"
}
case "$cmd" in
  push)
    sync_code
    ssh "$PI" "[ -d $REMOTE_DIR/data ] || mkdir -p $REMOTE_DIR/data"
    sync_data
    ssh -t "$PI" "cd $REMOTE_DIR && bash pi/install.sh"
    ;;
  cutover)
    echo "1) Parando el bot en este portátil (cierra la cartera por seguridad)…"
    systemctl --user stop trading-sim.service 2>/dev/null || true
    systemctl --user disable trading-sim.service 2>/dev/null || true
    echo "2) Copiando código y datos a la Pi…"
    sync_code; ssh "$PI" "mkdir -p $REMOTE_DIR/data"; sync_data
    echo "3) Instalando y arrancando en la Pi…"
    ssh -t "$PI" "cd $REMOTE_DIR && bash pi/install.sh"
    echo "✅ A partir de ahora el bot vive en la Pi. El servicio local queda desactivado."
    ;;
  pull-data)
    mkdir -p "$HERE/data-pi"
    rsync -az --info=progress2 "$PI:$REMOTE_DIR/data/" "$HERE/data-pi/"
    echo "Datos de la Pi en $HERE/data-pi"
    ;;
  status)  ssh "$PI" "systemctl status trading-sim --no-pager | head -n 12; echo; curl -s http://127.0.0.1:8777/api/state | python3 -c 'import json,sys; s=json.load(sys.stdin); print(\"equity\", round(s[\"equity\"],2), \"| posiciones\", len(s[\"open_positions\"]), \"| sombras\", [(x[\"name\"], round(x[\"pnl_pct\"],2)) for x in s[\"shadows\"]], \"| error\", repr(s[\"last_error\"]))'" ;;
  logs)    ssh -t "$PI" "journalctl -u trading-sim -f -n 50" ;;
  restart) ssh "$PI" "sudo systemctl restart trading-sim && echo reiniciado" ;;
  stop)    ssh "$PI" "sudo systemctl stop trading-sim && echo parado" ;;
  start)   ssh "$PI" "sudo systemctl start trading-sim && echo arrancado" ;;
  *) sed -n '2,10p' "$0" ;;
esac
