"""Métricas de riesgo compartidas por el panel y los backtests.

Sharpe = rentabilidad media por unidad de riesgo (volatilidad). Cuanto más alto,
mejor relación ganancia/sufrimiento. Referencia orientativa: <1 flojo, 1-2 bueno,
>2 excelente. Se anualiza para poder comparar periodos de distinta duración.
"""
from __future__ import annotations

import math


def sharpe(values: list[float], periods_per_year: float, rf: float = 0.0) -> float | None:
    """Sharpe anualizado a partir de una serie de valores de capital.

    `periods_per_year`: cuántos pasos de la serie equivalen a un año
    (p. ej. 365 para velas diarias, o 525600/loop_seg para la curva en vivo).
    `rf`: tasa libre de riesgo anual (0 por defecto; aquí operamos con puntos).
    """
    if not values or len(values) < 3 or periods_per_year <= 0:
        return None
    rets = []
    for a, b in zip(values[:-1], values[1:]):
        if a > 0:
            rets.append(b / a - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    rf_step = rf / periods_per_year
    return (mean - rf_step) / std * math.sqrt(periods_per_year)


def sharpe_from_timed_curve(curve: list[dict], rf: float = 0.0) -> float | None:
    """Sharpe a partir de una curva [{'ts':epoch,'value':x}, ...] con espaciado
    irregular: deduce los pasos por año del intervalo mediano entre puntos."""
    if not curve or len(curve) < 3:
        return None
    ts = [p["ts"] for p in curve]
    dts = sorted(t2 - t1 for t1, t2 in zip(ts[:-1], ts[1:]) if t2 > t1)
    if not dts:
        return None
    med = dts[len(dts) // 2]
    if med <= 0:
        return None
    ppy = 365.25 * 86400.0 / med
    return sharpe([p["value"] for p in curve], ppy, rf)
