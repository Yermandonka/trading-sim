"""Backtest sencillo del ensemble sobre datos históricos de un activo.

Simula largos y cortos con la misma lógica que el motor, para que puedas ver
cómo se habría comportado la estrategia ANTES de confiar en ella.
"""
from __future__ import annotations

from . import data
from . import strategies
from . import indicators as ind
from . import metrics

# velas por año según el tamaño de vela (para anualizar el Sharpe)
_BARS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "2h": 4380, "4h": 2190, "6h": 1460, "12h": 730,
    "1d": 365, "3d": 121.7, "1w": 52,
}


def run(symbol: str, interval: str = "1h", buy_th: float = 0.25,
        allow_short: bool = True, fee_pct: float = 0.001,
        weights: dict | None = None) -> dict:
    df = data.get_ohlcv(symbol, interval, limit=1000, max_age=60)
    if len(df) < 120:
        return {"error": "Datos insuficientes"}

    cash = 1000.0
    initial = cash
    pos = None  # {'side','qty','entry'}
    equity_curve = []
    trades = 0
    wins = 0

    atr_series = ind.atr(df)
    closes = df["close"].values
    start = 60
    for i in range(start, len(df)):
        window = df.iloc[: i + 1]
        res = strategies.analyze(window, weights)
        score = res["score"]
        price = float(closes[i])

        # valor actual
        if pos is None:
            eq = cash
        elif pos["side"] == "long":
            eq = cash + pos["qty"] * price
        else:
            eq = cash + pos["margin"] + pos["qty"] * (pos["entry"] - price)
        equity_curve.append(eq)

        if pos is None:
            if score >= buy_th or (allow_short and score <= -buy_th):
                side = "long" if score >= buy_th else "short"
                alloc = cash * 0.95
                fee = alloc * fee_pct
                qty = (alloc - fee) / price
                cash -= alloc
                pos = {"side": side, "qty": qty, "entry": price, "margin": alloc - fee}
        else:
            exit_now = (pos["side"] == "long" and score <= -buy_th) or \
                       (pos["side"] == "short" and score >= buy_th)
            if exit_now:
                if pos["side"] == "long":
                    pnl = pos["qty"] * (price - pos["entry"])
                    cash += pos["qty"] * price
                else:
                    pnl = pos["qty"] * (pos["entry"] - price)
                    cash += pos["margin"] + pnl
                cash -= pos["qty"] * price * fee_pct
                trades += 1
                if pnl > 0:
                    wins += 1
                pos = None

    final = equity_curve[-1] if equity_curve else initial
    peak = initial
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    # buy & hold de referencia
    bh = (closes[-1] / closes[start] - 1) * 100
    sharpe = metrics.sharpe(equity_curve, _BARS_PER_YEAR.get(interval, 365))
    return {
        "symbol": symbol,
        "interval": interval,
        "return_pct": round((final / initial - 1) * 100, 2),
        "buy_hold_pct": round(float(bh), 2),
        "trades": trades,
        "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "equity_curve": [round(v, 2) for v in equity_curve],
        "n_candles": len(equity_curve),
    }
