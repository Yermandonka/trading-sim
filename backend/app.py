"""API FastAPI + servidor del panel. Lanza el bucle del motor al arrancar."""
from __future__ import annotations

import os

from fastapi import FastAPI, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import backtest
from . import backtest_lev
from . import data
from . import indicators as ind
from . import metrics
from . import storage
from .engine import engine


def _annualized_sharpe(curve):
    # El Sharpe solo es significativo con historia suficiente: con muy pocos
    # puntos o menos de ~1 día, la anualización dispara cifras sin sentido.
    if len(curve) < 30:
        return None
    span = (curve[-1]["ts"] - curve[0]["ts"]) / 86400.0
    if span < 1.0:
        return None
    return metrics.sharpe_from_timed_curve(curve)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

app = FastAPI(title="Trading Sim")


@app.on_event("startup")
def _startup():
    engine.start_loop()


@app.on_event("shutdown")
def _shutdown():
    # al apagar / parar el servicio: salir a efectivo para no dejar riesgo sin vigilar
    engine.stop_flag = True
    if engine.cfg.get("close_on_shutdown", True):
        try:
            engine.close_all("Cierre automático por apagado del equipo")
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/state")
def state():
    return engine.snapshot()


@app.get("/api/equity")
def equity(limit: int = 1000):
    return storage.equity_curve(limit)


@app.get("/api/trades")
def trades(limit: int = 100):
    return storage.recent_trades(limit)


@app.post("/api/bot/start")
def bot_start():
    engine.start_bot()
    return {"running": engine.running}


@app.post("/api/bot/stop")
def bot_stop():
    engine.stop_bot()
    return {"running": engine.running}


@app.post("/api/reset")
def reset():
    engine.reset()
    return {"ok": True}


@app.post("/api/config")
def set_config(patch: dict = Body(...)):
    engine.update_config(patch)
    return engine._safe_config()


@app.get("/api/candles")
def candles(symbol: str, interval: str | None = None, limit: int = 150):
    iv = interval or engine.cfg["interval"]
    df = data.get_ohlcv(symbol, iv, limit=max(limit + 60, 200))
    close = df["close"]
    ema_f = ind.ema(close, 12)
    ema_s = ind.ema(close, 50)
    lo, mid, up = ind.bollinger(close)
    df = df.tail(limit)
    sl = slice(-limit, None)

    def ser(s):
        return [None if (v != v) else round(float(v), 8) for v in s.iloc[sl]]

    return {
        "symbol": symbol,
        "interval": iv,
        "t": [int(ts.timestamp()) for ts in df.index],
        "o": ser(df["open"]), "h": ser(df["high"]),
        "l": ser(df["low"]), "c": ser(df["close"]),
        "ema_fast": ser(ema_f), "ema_slow": ser(ema_s),
        "bb_low": ser(lo), "bb_up": ser(up),
    }


@app.get("/api/benchmark")
def benchmark(symbol: str = "BTCUSDT"):
    """Curva de 'comprar y mantener': qué valdría el capital inicial si se hubiera
    invertido entero en `symbol` al principio y no se hubiera tocado. Misma escala
    (puntos) y alineada a los mismos instantes que la curva de capital del bot."""
    import bisect
    curve = storage.equity_curve(5000)
    if len(curve) < 2:
        return []
    t0, t1 = curve[0]["ts"], curve[-1]["ts"]
    span_h = (t1 - t0) / 3600
    interval = "5m" if span_h <= 72 else ("1h" if span_h <= 960 else "1d")
    try:
        df = data.get_ohlcv(symbol, interval, limit=1000, max_age=60)
    except Exception:  # noqa: BLE001
        return []
    times = [ts.timestamp() for ts in df.index]
    closes = [float(c) for c in df["close"]]
    if not closes:
        return []

    def price_at(t):
        i = bisect.bisect_right(times, t) - 1
        return closes[max(0, min(i, len(closes) - 1))]

    p0 = price_at(t0)
    init = float(engine.cfg["initial_points"])
    if p0 <= 0:
        return []
    return [{"ts": pt["ts"], "value": init * price_at(pt["ts"]) / p0} for pt in curve]


@app.get("/api/chosen")
def chosen():
    """Curva de la cartera 'elegida': en cada ciclo sigue a la mejor entre tu
    estrategia y mantener BTC (cambia con margen de histéresis). Incluye el modo."""
    return storage.chosen_curve(5000)


@app.get("/api/stats")
def stats():
    trades = storage.recent_trades(5000)
    closed = [t for t in trades if t["action"] == "CERRAR"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    curve = storage.equity_curve(5000)
    vals = [p["value"] for p in curve]
    peak = vals[0] if vals else 0.0
    maxdd = 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak * 100)
    span_days = 0.0
    if len(curve) >= 2:
        span_days = (curve[-1]["ts"] - curve[0]["ts"]) / 86400.0
    init = float(engine.cfg["initial_points"])
    total_ret = (vals[-1] / init - 1) * 100 if vals and init else 0.0
    daily = (total_ret / span_days) if span_days > 0.25 else None
    # Sharpe anualizado: rendimiento por unidad de riesgo (volatilidad)
    sharpe = _annualized_sharpe(curve)
    return {
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "best": round(max((t["pnl"] for t in closed), default=0.0), 2),
        "worst": round(min((t["pnl"] for t in closed), default=0.0), 2),
        "total_realized": round(sum(t["pnl"] for t in closed), 2),
        "max_drawdown": round(maxdd, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "span_days": round(span_days, 2),
        "daily_avg": round(daily, 3) if daily is not None else None,
        "n_equity": len(curve),
    }


@app.get("/api/backtest")
def run_backtest(symbol: str, interval: str = "1h"):
    cfg = engine.cfg
    return backtest.run(
        symbol, interval,
        buy_th=float(cfg["buy_threshold"]),
        allow_short=bool(cfg["allow_short"]),
        fee_pct=float(cfg["fee_pct"]),
        weights=cfg["weights"],
    )


@app.get("/api/shadows")
def shadows(limit: int = 5000):
    """Curvas de capital de las carteras sombra (configuraciones en paralelo)."""
    return storage.shadow_curves(limit)


@app.get("/api/shadow_trades")
def shadow_trades(name: str, limit: int = 100):
    return storage.shadow_trades(name, limit)


@app.get("/api/runs")
def runs():
    """Simulaciones archivadas (curvas de capital de configuraciones anteriores)."""
    return storage.list_runs()


@app.post("/api/runs/delete")
def runs_delete(body: dict = Body(...)):
    return {"ok": storage.delete_run(str(body.get("name", "")))}


@app.get("/api/backtest_lev")
def run_backtest_lev(interval: str = "1h", leverage: float = 1.0,
                     risk_per_trade: float = 0.01, max_margin_use: float = 0.5,
                     max_notional_per_pos: float = 0.3, daily_loss_limit: float = 0.03,
                     dd_halt: float = 0.20, limit: int = 1000):
    """Backtest con apalancamiento (perpetuos, margen aislado, liquidación,
    comisiones taker y financiación). Lento (minutos): precalcula señales."""
    cfg = engine.cfg
    syms = list(cfg["watchlist"])
    sig = backtest_lev.build_signals(syms, interval, limit=limit, weights=cfg["weights"])
    if not sig:
        return JSONResponse({"error": "Sin datos"}, status_code=502)
    res = backtest_lev.run(sig, interval, {
        **{k: cfg[k] for k in ("buy_threshold", "allow_short", "stop_atr_mult",
                                "take_atr_mult", "use_trailing", "trail_atr_mult",
                                "trend_ride", "ride_trail_mult", "regime_filter",
                                "initial_points", "max_positions") if k in cfg},
        "leverage": leverage, "risk_per_trade": risk_per_trade,
        "max_margin_use": max_margin_use, "max_notional_per_pos": max_notional_per_pos,
        "daily_loss_limit": daily_loss_limit, "dd_halt": dd_halt,
    })
    res["symbols"] = list(sig.keys())
    return res


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
