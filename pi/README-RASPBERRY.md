# Trading Sim en una Raspberry Pi

Objetivo: que el bot opere 24 h sin depender del portátil (que hiberna con el pomodoro).

## Qué comprar
- Raspberry Pi 5 de 4 GB (o Pi 4 de 2 GB si quieres gastar menos).
- Fuente oficial (27 W para la Pi 5).
- Caja con ventilador o "Active Cooler" (va a estar siempre encendida).
- MicroSD de 32 GB (clase A2) y un cable Ethernet al router.

## Preparar la tarjeta (en el portátil, 10 minutos)
1. Instala **Raspberry Pi Imager** (`sudo apt install rpi-imager` o desde raspberrypi.com/software).
2. Elige *Raspberry Pi OS Lite (64-bit)* (sin escritorio: no hace falta).
3. En el engranaje de ajustes del Imager:
   - nombre de equipo: `raspberrypi` (así entras por `raspberrypi.local`)
   - usuario y contraseña: p. ej. `pi` / la que quieras
   - **activa SSH** (con contraseña o con tu clave pública `~/.ssh/id_*.pub`)
   - wifi solo si no vas a usar cable
4. Graba, mete la tarjeta en la Pi, conecta Ethernet y corriente. Espera 2 minutos.

## Instalar el bot (desde el portátil, 1 comando)
```bash
cd ~/Projects/trading-sim
ssh pi@raspberrypi.local            # primera vez: acepta la huella y comprueba que entras; sal con exit
PI=pi@raspberrypi.local pi/deploy.sh push
```
`push` copia el proyecto, instala Python y dependencias en la Pi, crea el servicio que arranca
solo al encender y lo pone en marcha. Al final imprime la dirección del panel:
`http://raspberrypi.local:8777` (o la IP). Tarda unos 5-10 minutos la primera vez.

## Traslado definitivo (cuando veas que en la Pi funciona)
```bash
PI=pi@raspberrypi.local pi/deploy.sh cutover
```
Para el bot del portátil (cierra la cartera por seguridad), copia el estado y los datos
tal cual están y arranca en la Pi. Desde ese momento el bot vive allí.

## Día a día
```bash
pi/deploy.sh status      # capital, posiciones, sombras, errores
pi/deploy.sh logs        # registro en vivo (Ctrl+C para salir)
pi/deploy.sh pull-data   # trae data/ a ./data-pi para analizarlo en el portátil
pi/deploy.sh restart     # reinicia el servicio (cierra y reabre la cartera)
```
Para cambiar ajustes usa el panel desde el navegador del portátil, igual que ahora.
Si cambias código aquí, `pi/deploy.sh push` lo sube y reinicia (los datos no se tocan).

## Cosas a saber
- El panel **no tiene contraseña**: solo es accesible en tu red local. No abras el puerto 8777 en el router.
- La Pi hace una copia diaria de `data/` en `~/trading-sim-backups` (guarda 30 días).
- La hora se sincroniza sola (importante para la financiación cada 8 h).
- Si se va la luz, al volver arranca sola y el bot sigue (cierra todo al apagarse, reabre según señales).
- Las claves (IA) viajan dentro de `data/config.json`; la Pi es tuya, pero no compartas esa tarjeta.
