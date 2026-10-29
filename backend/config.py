"""Configuración persistente de la simulación."""
from __future__ import annotations

import json
import os
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.Lock()

DEFAULTS = {
    "initial_points": 10000.0,       # puntos ficticios de partida
    "interval": "1h",                # tamaño de vela para el análisis (1h: la única validada por tramos)
    "loop_seconds": 60,              # cada cuánto re-evalúa el bot
    "auto_universe": True,           # elegir solos los activos más líquidos
    "universe_size": 40,             # cuántos activos vigilar
    "watchlist": [                   # usado solo si auto_universe = False
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
        "MATICUSDT", "LTCUSDT", "TRXUSDT", "ATOMUSDT", "UNIUSDT",
    ],
    "allow_short": True,             # permitir ventas en corto
    "buy_threshold": 0.50,           # |puntuación| mínima para operar (0,5 = muy selectivo, validado)
    "max_positions": 8,              # menos posiciones pero más grandes (más convicción)
    "risk_per_trade": 0.01,          # modo "risk": fracción del capital que se PIERDE si salta el stop inicial
    "sizing_mode": "risk",           # "risk" (tamaño por distancia al stop) | "margin" (antiguo: fracción como margen)
    "leverage": 1.0,                 # apalancamiento (nominal / margen); 1 = sin apalancar
    "max_notional_per_pos": 0.3,     # nominal máximo de UNA posición / capital
    "max_margin_use": 0.5,           # margen total bloqueado / capital (el resto es colchón)
    "maint_ratio": 0.5,              # margen de mantenimiento = 50 % del inicial (liquidación)
    "funding_8h": 0.0001,            # financiación 0,01 % del nominal cada 8 h (largos pagan, cortos cobran)
    "dd_halt": 0.25,                 # cortacircuitos: caída desde máximos que cierra todo y pausa
    "halt_days": 14,                 # días de pausa tras el cortacircuitos
    "stop_atr_mult": 5.0,            # stop-loss inicial = entrada ± ATR*mult (ancho: validado en 1h)
    "take_atr_mult": 8.0,            # objetivo lejano: deja correr con el trailing
    "use_trailing": True,            # stop dinámico que sube con el beneficio
    "trail_atr_mult": 3.0,           # distancia del stop dinámico (ATR*mult)
    "trend_ride": True,              # "dejar correr" los ganadores en tendencia confirmada
    "ride_trail_mult": 2.0,          # cuánto se ensancha el trailing en modo dejar-correr
    "regime_filter": True,           # no operar contra la tendencia de fondo (EMA200)
    "fee_pct": 0.001,                # comisión simulada por operación (0.1%)
    # --- seguidor del ganador (estrategia vs comprar-y-mantener BTC) ---
    "follow_winner": True,           # el capital "elegido" sigue a la mejor de las dos
    "switch_margin": 0.003,          # cambia solo si la otra supera por este margen (0,3%)
    "close_on_shutdown": True,       # cerrar toda la cartera al apagar (evita riesgo sin vigilar)
    "weights": {
        "trend": 1.4, "macd": 1.1, "rsi": 1.0,
        "bollinger": 0.9, "momentum": 1.2, "breakout": 1.0,
    },
    # --- carteras sombra: otras configuraciones operando en vivo en paralelo ---
    # cada una: {"name": "...", "overrides": {claves que cambian respecto a la principal}}
    "shadows": [],
    # --- capa de IA (filtro/veto de entradas) ---
    "ai_enabled": False,             # activar el filtro de IA
    "ai_api_key": "",                # clave de Google AI Studio (o env GEMINI_API_KEY)
}


def load() -> dict:
    with _lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                merged = {**DEFAULTS, **cfg}
                merged["weights"] = {**DEFAULTS["weights"], **cfg.get("weights", {})}
                return merged
            except Exception:  # noqa: BLE001
                pass
        return json.loads(json.dumps(DEFAULTS))


def save(cfg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
