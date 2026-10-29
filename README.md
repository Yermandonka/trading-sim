# Trading Sim 📈

Simulador de trading cuantitativo con **puntos ficticios**. Analiza criptoactivos
en tiempo real combinando 6 algoritmos, decide comprar / vender en corto / esperar,
gestiona el riesgo (stop-loss y take-profit automáticos) y lo enseña todo en un
panel web claro. Pensado para correr **24h en segundo plano**.

> ⚠️ Esto NO predice el futuro ni garantiza beneficios. Son técnicas cuantitativas
> estándar sobre datos reales, pero ningún algoritmo asegura ganar dinero. Úsalo
> para aprender y medir estrategias ANTES de arriesgar dinero real. El backtest
> incluido te muestra, sin adornos, cómo lo habría hecho en el pasado.

## Arrancar
```bash
cd ~/Projects/trading-sim
./run.sh
```
Abre http://127.0.0.1:8777 y pulsa **Arrancar bot**.

## Correr 24h en segundo plano (systemd de usuario)
```bash
mkdir -p ~/.config/systemd/user
cp ~/Projects/trading-sim/trading-sim.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trading-sim.service
loginctl enable-linger $USER      # sigue vivo aunque cierres sesión
systemctl --user status trading-sim.service
```
Parar: `systemctl --user stop trading-sim.service`

## Cómo decide
Para cada activo calcula 6 señales en el rango [−1, +1] y las pondera:

| Algoritmo        | Qué mide |
|------------------|----------|
| Tendencia (EMA)  | Cruce y pendiente de medias 12/50 |
| Impulso (MACD)   | Aceleración del movimiento |
| RSI              | Sobrecompra / sobreventa (contrario) |
| Bollinger        | Caro/barato frente a su media |
| Momentum (ROC)   | Velocidad reciente del precio |
| Rotura (Donchian)| Rupturas del rango de 20 velas |

La suma ponderada da una **puntuación final**. Si supera el *umbral* abre largo;
si cae por debajo del umbral negativo y los cortos están activados, abre corto.
Las posiciones se cierran por stop-loss, take-profit (ambos basados en ATR) o
cuando la señal se gira.

**Universo de activos**: con `auto_universe` activado el bot elige solo los
`universe_size` pares USDT más líquidos de Binance (por defecto 40), excluyendo
stablecoins y tokens apalancados. Se refresca cada 30 min.

**Mejoras del algoritmo (v2)**:
- **Filtro de régimen (EMA200)**: no se abren largos en tendencia de fondo bajista ni
  cortos en tendencia alcista — evita "remar contra la corriente".
- **Stop dinámico (trailing)**: el stop sube con el beneficio (ATR·`trail_atr_mult`),
  dejando correr las subidas en lugar de cortarlas con un objetivo fijo.
- **Tamaño por convicción**: las señales más fuertes reciben más capital (dentro del riesgo máx).

> ⚠️ Objetivos tipo "20%/mes garantizado" NO son realistas ni alcanzables de forma
> fiable; subir el riesgo por operación agranda tanto las ganancias como las pérdidas.
> Mide siempre con el backtest antes de creerte cualquier configuración.

**Filtro de IA (opcional)**: los algoritmos deciden la operación; si activas la IA,
antes de abrir una entrada se consulta a un LLM (Google AI Studio, gratis) que la
**aprueba o la veta** por coherencia/riesgo. La IA **nunca** bloquea una salida de
protección (stop-loss / take-profit), solo filtra entradas. Sin clave o si la IA
falla, la operación se deja pasar (*fail-open*) y se marca. Activa en ⚙ Ajustes
pegando tu clave de `aistudio.google.com/app/apikey` (o exporta `GEMINI_API_KEY`).

## Ajustes (desde el panel o `data/config.json`)
- `buy_threshold` — cuán fuerte debe ser la señal para operar (más alto = más selectivo)
- `risk_per_trade` — fracción del capital por operación
- `max_positions` — posiciones simultáneas
- `interval` / `loop_seconds` — vela de análisis y frecuencia del bot
- `allow_short` — permitir ventas en corto
- `weights` — peso de cada algoritmo

## Datos
Fuente: **Binance** (API pública, cripto 24/7, sin clave) con respaldo en **Coinbase**.
El motor es agnóstico: cambiando `data.py` puede apuntar a otros mercados/brokers.

## Estructura
```
backend/   data.py  indicators.py  strategies.py  engine.py
           portfolio (en engine)   backtest.py    storage.py   app.py (FastAPI)
web/       index.html  styles.css  app.js   (panel)
data/      config.json  state.json  history.db   (se crean solos)
```

## Apalancamiento (perpetuos) — backtest y conclusión
`backend/backtest_lev.py` simula la mecánica real de un perpetuo (Kraken/OKX):
margen aislado, nominal = margen × apalancamiento, **tamaño por riesgo** (fracción
del capital que se pierde si salta el stop), comisión taker 0,05 % sobre el nominal,
deslizamiento, financiación cada 8 h, **liquidación** al 50 % del margen (comprobada
con máximo/mínimo de vela), límite de pérdida diaria y cortacircuitos por caída.
Endpoint: `GET /api/backtest_lev?interval=1h&leverage=5&risk_per_trade=0.01` (lento).

> ⚠️ Medido (oct-2026, 15 majors): la ventaja BRUTA de la estrategia es mínima
> (profit factor ≈ 1,09 sin costes, ≈ 1,0-1,05 con costes de perpetuos) y NO es
> robusta (correlación entre parámetros buenos en entrenamiento y en test: 0,16).
> Apalancar eso no multiplica ganancias: multiplica varianza. Sin seguridad, 5x-10x
> acabó en -48 % / -60 % con caídas del 75-86 %. Con límites de seguridad, toda
> configuración apalancada llegó a una caída del 20 % en 120 días. Tablas en
> `data/sweep_15m.csv` y `data/robust_1h.csv`. Regla: apalancamiento = 1 hasta que
> la estrategia muestre PF > 1,3 estable fuera de muestra.

### Barrido de rotación con validación por tramos (2026-10-28)
512 configuraciones (umbral × stop × trailing × dejar-correr × régimen × enfriamiento),
sin apalancar, costes de perpetuos, evaluadas en 3-4 tramos consecutivos
(`data/wf_15m.csv`, `data/wf_4h.csv`, `data/wf_1h.csv`):

| Vela | Datos | Configs con PF>1 en TODOS los tramos | PF mediano |
|------|-------|--------------------------------------|-----------|
| 15m  | 116 d | 0 de 512 | 0,89 |
| 4h   | 663 d | 0 de 512 | 1,09 |
| 1h   | 713 d | **10 de 512** | 0,98 |

La única familia que aguanta: **vela 1h, umbral 0,5, stop 5 ATR, trailing 3 ATR,
dejar-correr ON, régimen ON** (PF por tramos 1,34 / 1,18 / 1,09 / 1,54; +26 %/año,
caída máx. 15 %, Sharpe 1,1, ~300 operaciones/año). Elegida entre 512 tras ver
los datos: en real espera la mitad. Con tamaño por riesgo, apalancar NO cambia el
resultado (solo el margen bloqueado); solo subir el riesgo por operación lo cambia
(3x con 2 %: +38 %/año pero caída 35 % y peor mes -15 %). Con stop de 5 ATR en 1h
(≈4,6 %), a partir de 5x la liquidación (10 %) está demasiado cerca; 10x (5 %) es
inviable. El límite de pérdida diaria del 3 % empeora todo (cierra en los vaivenes
normales); el cortacircuitos por caída como pausa de 14 días es inocuo.

### Modo apalancado en el bot (2026-10-28)
El motor en vivo ya simula una cuenta de perpetuos: `leverage`, tamaño por riesgo
(`sizing_mode: "risk"`, `risk_per_trade` = % del capital que se pierde si salta el
stop), `max_notional_per_pos`, `max_margin_use`, liquidación (`maint_ratio`),
financiación cada 8 h (`funding_8h`) y cortacircuitos (`dd_halt` + `halt_days`:
cierra todo y pausa). El panel muestra el beneficio **en % sobre lo aportado**
(el depósito inicial), el precio de liquidación de cada posición y la pausa.
Configuración en vivo desde 2026-10-28: la validada de 1h (umbral 0,5, stop 5 ATR,
trailing 3, dejar-correr y régimen ON, 15 majors fijos, riesgo 1 %, 1x).
Copia de seguridad de la simulación anterior (15m, 7 días, +3,3 %) en `data/backup-2026-10-28/`.

### Carteras sombra y mejora aplicada (2026-10-28)
`shadows` en `data/config.json`: otras configuraciones que operan **en vivo en paralelo**
(mismos datos, mismo instante, capital propio): `{"name", "color", "overrides"}`. Se dibujan
en la gráfica de capital y tienen tablas propias (`shadow_equity`, `shadow_trades`;
API `/api/shadows`, `/api/shadow_trades?name=`). Activas: "antigua" (config de la semana
del 21-28 oct, rosa) y "8 posiciones" (la principal con 8 posiciones, azul).
Mejora aplicada a la principal tras 28 variantes validadas por tramos: **máx. 4 posiciones**
(caída máx. 12 % vs 15 %, Sharpe 1,34 vs 1,10, retorno igual). Descartadas: ordenar por
fuerza de señal, enfriamiento, umbral 0,45/0,55, stop 4; "dejar correr" es imprescindible.

### Sombra "cash & carry" (2026-10-28)
`CarryPortfolio` (`kind: "carry"` en `shadows`): largo al contado + corto en el perpetuo
por el mismo importe; el precio no afecta y el ingreso es la **financiación real** que
pagan los largos cada 8 h (`data.funding_rates`: Binance Futures premiumIndex, respaldo
Kraken Futures). Entra si la financiación anualizada ≥ `carry_min_apr` (5 %), sale tras
`carry_exit_periods` (3) pagos seguidos sin cobrar. Corto a `carry_leverage` (2x) con
margen aislado: si el precio sube se repone margen desde la caja (acción `MARGEN` en
`shadow_trades`); sin caja, se cierra. Comisiones contado 0,1 % + perp 0,05 % por lado.
Riesgos reales no simulados: diferencia contado/perpetuo al cerrar (basis), broker.

## Raspberry Pi (24 h sin el portátil)
Guía completa en `pi/README-RASPBERRY.md`. Resumen: graba Raspberry Pi OS Lite 64 bits con
SSH activado, y desde el portátil `PI=pi@raspberrypi.local pi/deploy.sh push` (instala y
arranca) o `pi/deploy.sh cutover` (traslado definitivo). Panel en `http://raspberrypi.local:8777`.
