"""Estrategias cuantitativas y ensemble ponderado.

Cada sub-estrategia devuelve una señal en [-1, 1]:
   +1 = comprar fuerte (alcista),  -1 = vender/corto fuerte (bajista), 0 = neutral.
Y una explicación en lenguaje sencillo para el panel.

El ensemble combina todas con pesos configurables -> puntuación final en [-1, 1].
"""
from __future__ import annotations

import math

import pandas as pd

from . import indicators as ind


def _trend(df: pd.DataFrame) -> dict:
    close = df["close"]
    ema_fast = ind.ema(close, 12)
    ema_slow = ind.ema(close, 50)
    price = float(close.iloc[-1])
    gap = (float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])) / price
    slope = (float(ema_fast.iloc[-1]) - float(ema_fast.iloc[-5])) / price
    score = ind.clamp(gap * 60 + slope * 40)
    if score > 0.15:
        txt = "La media rápida va por encima de la lenta y subiendo: tendencia alcista."
    elif score < -0.15:
        txt = "La media rápida va por debajo de la lenta y bajando: tendencia bajista."
    else:
        txt = "Las medias están cruzadas/planas: sin tendencia clara."
    return {"score": score, "text": txt,
            "detail": f"EMA12 {ema_fast.iloc[-1]:.2f} vs EMA50 {ema_slow.iloc[-1]:.2f}"}


def _macd_sig(df: pd.DataFrame) -> dict:
    close = df["close"]
    _, _, hist = ind.macd(close)
    price = float(close.iloc[-1])
    h = float(hist.iloc[-1])
    score = ind.clamp((h / price) * 400)
    if score > 0.1:
        txt = "El MACD gana impulso al alza: el movimiento alcista se acelera."
    elif score < -0.1:
        txt = "El MACD gana impulso a la baja: el movimiento bajista se acelera."
    else:
        txt = "El MACD está plano: impulso débil."
    return {"score": score, "text": txt, "detail": f"Histograma MACD {h:.4f}"}


def _rsi_sig(df: pd.DataFrame) -> dict:
    r = float(ind.rsi(df["close"]).iloc[-1])
    # Contrarian: sobreventa (<30) -> comprar; sobrecompra (>70) -> vender.
    if r <= 30:
        score = ind.clamp((30 - r) / 25)
        txt = f"RSI {r:.0f}: sobrevendido, suele rebotar al alza."
    elif r >= 70:
        score = -ind.clamp((r - 70) / 25)
        txt = f"RSI {r:.0f}: sobrecomprado, riesgo de corrección a la baja."
    else:
        score = (50 - r) / 100
        txt = f"RSI {r:.0f}: en zona neutral."
    return {"score": ind.clamp(score), "text": txt, "detail": f"RSI {r:.1f}"}


def _bollinger_sig(df: pd.DataFrame) -> dict:
    close = df["close"]
    lo, mid, up = ind.bollinger(close)
    price = float(close.iloc[-1])
    lo_v, up_v, mid_v = float(lo.iloc[-1]), float(up.iloc[-1]), float(mid.iloc[-1])
    width = max(up_v - lo_v, 1e-9)
    pos = (price - mid_v) / (width / 2)  # -1 banda inferior, +1 banda superior
    score = ind.clamp(-pos * 0.8)  # contrarian hacia la media
    if price <= lo_v:
        txt = "Precio pegado a la banda inferior de Bollinger: barato frente a su media."
    elif price >= up_v:
        txt = "Precio pegado a la banda superior de Bollinger: caro frente a su media."
    else:
        txt = "Precio dentro de las bandas de Bollinger: valoración normal."
    return {"score": score, "text": txt,
            "detail": f"Bandas [{lo_v:.2f}, {up_v:.2f}], precio {price:.2f}"}


def _momentum(df: pd.DataFrame) -> dict:
    r = float(ind.roc(df["close"], 10).iloc[-1])
    score = ind.clamp(r / 8.0)
    if score > 0.1:
        txt = f"Momentum +{r:.1f}%: el precio sube con fuerza en las últimas velas."
    elif score < -0.1:
        txt = f"Momentum {r:.1f}%: el precio cae en las últimas velas."
    else:
        txt = f"Momentum {r:.1f}%: movimiento lateral."
    return {"score": score, "text": txt, "detail": f"ROC(10) {r:.2f}%"}


def _breakout(df: pd.DataFrame) -> dict:
    lo, hi = ind.donchian(df, 20)
    price = float(df["close"].iloc[-1])
    hi_v, lo_v = float(hi.iloc[-2]), float(lo.iloc[-2])  # canal previo
    if price > hi_v:
        score = 0.8
        txt = "Rotura al alza del máximo de 20 velas: posible arranque de subida."
    elif price < lo_v:
        score = -0.8
        txt = "Rotura a la baja del mínimo de 20 velas: posible arranque de caída."
    else:
        rng = max(hi_v - lo_v, 1e-9)
        score = ind.clamp(((price - lo_v) / rng - 0.5) * 0.6)
        txt = "Precio dentro de su canal de las últimas 20 velas."
    return {"score": score, "text": txt, "detail": f"Canal [{lo_v:.2f}, {hi_v:.2f}]"}


_STRATS = {
    "trend": ("Tendencia (EMA)", _trend),
    "macd": ("Impulso (MACD)", _macd_sig),
    "rsi": ("RSI", _rsi_sig),
    "bollinger": ("Bollinger", _bollinger_sig),
    "momentum": ("Momentum", _momentum),
    "breakout": ("Rotura (Donchian)", _breakout),
}

DEFAULT_WEIGHTS = {
    "trend": 1.4, "macd": 1.1, "rsi": 1.0,
    "bollinger": 0.9, "momentum": 1.2, "breakout": 1.0,
}


def analyze(df: pd.DataFrame, weights: dict | None = None) -> dict:
    """Analiza un activo y devuelve la puntuación del ensemble + desglose."""
    weights = weights or DEFAULT_WEIGHTS
    parts = []
    total_w = 0.0
    weighted = 0.0
    for key, (name, fn) in _STRATS.items():
        try:
            res = fn(df)
        except Exception:  # noqa: BLE001
            res = {"score": 0.0, "text": "Sin datos suficientes.", "detail": ""}
        w = float(weights.get(key, 1.0))
        weighted += res["score"] * w
        total_w += w
        parts.append({
            "key": key, "name": name, "score": round(res["score"], 3),
            "weight": w, "text": res["text"], "detail": res["detail"],
        })
    score = ind.clamp(weighted / total_w if total_w else 0.0)
    atr_val = float(ind.atr(df).iloc[-1])
    price = float(df["close"].iloc[-1])
    confidence = min(1.0, abs(score) * 1.3)
    # --- filtro de régimen: tendencia de fondo (EMA200) ---
    ema200 = ind.ema(df["close"], 200)
    e_now = float(ema200.iloc[-1])
    e_prev = float(ema200.iloc[-10]) if len(ema200) >= 10 else e_now
    if price > e_now and e_now >= e_prev:
        regime = 1          # mercado alcista de fondo
    elif price < e_now and e_now <= e_prev:
        regime = -1         # mercado bajista de fondo
    else:
        regime = 0          # sin tendencia clara / transición
    spark = [round(float(v), 8) for v in df["close"].iloc[-32:]]
    change = ((price / float(df["close"].iloc[-32]) - 1) * 100) if len(df) >= 32 else 0.0
    return {
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "price": price,
        "atr": atr_val,
        "atr_pct": round(atr_val / price * 100, 2),
        "change_pct": round(change, 2),
        "regime": regime,
        "spark": spark,
        "parts": parts,
    }


def plain_decision(score: float, buy_th: float, allow_short: bool) -> str:
    if score >= buy_th:
        return "COMPRAR (largo)"
    if allow_short and score <= -buy_th:
        return "VENDER EN CORTO"
    return "ESPERAR"
