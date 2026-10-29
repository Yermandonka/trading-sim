"""Backtest FIEL al motor completo (engine.step) sobre datos históricos.

A diferencia de `backtest.py` (una posición por activo, solo entra/sale por score),
este replica la gestión real de cartera del bot:
  - caja compartida y límite de posiciones simultáneas (max_positions)
  - tamaño por riesgo y confianza (risk_per_trade · (0.55 + 0.45·conf))
  - filtro de régimen (EMA200): no rema contra la tendencia de fondo
  - stop-loss + trailing dinámico + modo "dejar correr" + take-profit
  - salida por giro de la señal del ensemble
  - comisiones al abrir y cerrar

El filtro de IA NO se simula (es una llamada externa en vivo) — se asume desactivado.
"""
from __future__ import annotations

from . import data, strategies, config, metrics
from . import indicators as ind

# velas por año según el tamaño de vela (para anualizar el Sharpe)
_BARS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "2h": 4380, "4h": 2190, "6h": 1460, "12h": 730,
    "1d": 365, "3d": 121.7, "1w": 52,
}

# Cesta FIJA para backtests reproducibles (no depende del top por volumen, que
# cambia a diario y hace bailar los resultados). 15 majors con historia larga.
FIXED_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
    "LTCUSDT", "TRXUSDT", "ATOMUSDT", "UNIUSDT", "XLMUSDT",
]


def _position_value(pos: dict, price: float) -> float:
    if pos["side"] == "long":
        return pos["qty"] * price
    return pos["margin"] + pos["qty"] * (pos["entry"] - price)


def _unrealized(pos: dict, price: float) -> float:
    if pos["side"] == "long":
        return pos["qty"] * (price - pos["entry"])
    return pos["qty"] * (pos["entry"] - price)


def run_portfolio(symbols: list[str], interval: str = "1d",
                  cfg: dict | None = None, limit: int = 1000,
                  warmup: int = 200) -> dict:
    cfg = cfg or config.load()
    buy_th = float(cfg["buy_threshold"])
    allow_short = bool(cfg["allow_short"])
    fee_pct = float(cfg["fee_pct"])
    max_pos = int(cfg["max_positions"])
    risk = float(cfg["risk_per_trade"])
    sm = float(cfg["stop_atr_mult"])
    tm = float(cfg["take_atr_mult"])
    use_trail = bool(cfg.get("use_trailing", True))
    trend_ride = bool(cfg.get("trend_ride", True))
    base_trail = float(cfg.get("trail_atr_mult", 3.0))
    ride_mult = float(cfg.get("ride_trail_mult", 2.0))
    regime_filter = bool(cfg.get("regime_filter", True))
    weights = cfg["weights"]
    initial = float(cfg["initial_points"])

    # 1) descargar histórico de cada activo e indexar por timestamp
    frames = {}
    skipped = []
    for s in symbols:
        try:
            df = data.get_ohlcv(s, interval, limit=limit, max_age=60)
        except Exception as e:  # noqa: BLE001
            skipped.append((s, str(e)))
            continue
        # descartamos la última vela (aún en formación) -> backtest reproducible
        if len(df) > 1:
            df = df.iloc[:-1]
        if len(df) < warmup + 20:
            skipped.append((s, f"solo {len(df)} velas"))
            continue
        # timestamps (epoch seg) desde el índice datetime, antes de resetear
        epochs = [int(t.timestamp()) for t in df.index]
        df = df.reset_index(drop=True)
        df["_epoch"] = epochs
        frames[s] = df

    if not frames:
        return {"error": "Sin datos suficientes para ninguna moneda", "skipped": skipped}

    # 2) línea temporal común (unión de timestamps)
    ts_index = {}  # symbol -> {ts: row_idx}
    all_ts = set()
    for s, df in frames.items():
        m = {int(t): i for i, t in enumerate(df["_epoch"])}
        ts_index[s] = m
        all_ts.update(m.keys())
    timeline = sorted(all_ts)

    # 3) estado de la cartera
    cash = initial
    realized = 0.0
    positions = []          # dicts como en el motor
    prices = {}             # último precio conocido por símbolo
    equity_curve = []
    trades = 0
    wins = 0
    exit_reasons = {}
    # --- follow_winner: curva "elegida" que sigue a la mejor (estrategia vs BTC) ---
    switch_margin = float(cfg.get("switch_margin", 0.005))
    chosen_meta = None
    chosen_curve = []
    switches = 0
    # buy & hold: cesta equiponderada comprada en el primer ts operable de cada activo
    bh_units = {}           # symbol -> unidades compradas con initial/len
    bh_started = False
    bh_curve = []           # valor de la cesta comprada-y-mantenida en cada paso

    def equity_now():
        total = cash
        for p in positions:
            total += _position_value(p, prices.get(p["symbol"], p["entry"]))
        return total

    def pos_for(sym):
        for p in positions:
            if p["symbol"] == sym:
                return p
        return None

    def do_open(sym, side, price, atr, score, conf):
        nonlocal cash
        eq = equity_now()
        alloc = eq * risk * (0.55 + 0.45 * conf)
        alloc = min(alloc, cash * 0.95)
        if alloc < 1:
            return
        fee = alloc * fee_pct
        qty = (alloc - fee) / price
        if side == "long":
            stop = price - atr * sm
            take = price + atr * tm
        else:
            stop = price + atr * sm
            take = price - atr * tm
        cash -= alloc
        positions.append({
            "symbol": sym, "side": side, "qty": qty, "entry": price,
            "margin": alloc - fee, "stop": stop, "take": take,
            "peak": price, "atr": atr,
        })

    def do_close(pos, price, reason):
        nonlocal cash, realized, trades, wins
        fee = pos["qty"] * price * fee_pct
        pnl = _unrealized(pos, price) - fee
        cash += _position_value(pos, price) - fee
        realized += pnl
        positions.remove(pos)
        trades += 1
        if pnl > 0:
            wins += 1
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # 4) recorrer la línea temporal (replica engine.step por cada timestamp)
    for t in timeline:
        # análisis de cada símbolo con vela en este ts y suficiente historia
        results = {}
        for s, df in frames.items():
            idx = ts_index[s].get(t)
            if idx is None or idx < warmup:
                continue
            window = df.iloc[: idx + 1]
            res = strategies.analyze(window, weights)
            results[s] = res

        if not results:
            continue

        # buy & hold: arranca en el primer ts donde hay activos operables
        if not bh_started:
            per = initial / len(frames)
            for s in frames:
                idx = ts_index[s].get(t)
                if idx is not None and idx >= warmup:
                    bh_units[s] = per / float(frames[s]["close"].iloc[idx])
            bh_started = True

        for s in frames:
            res = results.get(s)
            if not res:
                continue
            price = res["price"]; atr = res["atr"]; score = res["score"]
            regime = res.get("regime", 0)
            prices[s] = price
            pos = pos_for(s)
            if pos:
                riding = trend_ride and (
                    (pos["side"] == "long" and regime > 0) or
                    (pos["side"] == "short" and regime < 0))
                trail = base_trail * (ride_mult if riding else 1.0) * pos.get("atr", atr)
                if pos["side"] == "long":
                    if use_trail:
                        pos["peak"] = max(pos.get("peak", pos["entry"]), price)
                        pos["stop"] = max(pos["stop"], pos["peak"] - trail)
                    if price <= pos["stop"]:
                        do_close(pos, price, "Stop dinámico" if use_trail else "Stop-loss")
                    elif riding:
                        if regime < 0:
                            do_close(pos, price, "Fin de tendencia")
                    elif price >= pos["take"]:
                        do_close(pos, price, "Take-profit")
                    elif score <= -buy_th:
                        do_close(pos, price, "Giro de señal")
                else:
                    if use_trail:
                        pos["peak"] = min(pos.get("peak", pos["entry"]), price)
                        pos["stop"] = min(pos["stop"], pos["peak"] + trail)
                    if price >= pos["stop"]:
                        do_close(pos, price, "Stop dinámico" if use_trail else "Stop-loss")
                    elif riding:
                        if regime > 0:
                            do_close(pos, price, "Fin de tendencia")
                    elif price <= pos["take"]:
                        do_close(pos, price, "Take-profit")
                    elif score >= buy_th:
                        do_close(pos, price, "Giro de señal")
            else:
                if len(positions) >= max_pos:
                    continue
                side = None
                if score >= buy_th:
                    side = "long"
                elif allow_short and score <= -buy_th:
                    side = "short"
                if side:
                    if regime_filter:
                        if side == "long" and regime < 0:
                            continue
                        if side == "short" and regime > 0:
                            continue
                    do_open(s, side, price, atr, score, res.get("confidence", 0.5))

        eq = equity_now()
        equity_curve.append(eq)

        # follow_winner: replica engine._update_chosen
        btc = prices.get("BTCUSDT", 0.0)
        if btc > 0 and eq > 0:
            if chosen_meta is None:
                chosen_meta = {"btc_ref": btc, "chosen": eq, "mode": "strategy",
                               "prev_strat": eq, "prev_btc": btc}
            else:
                m = chosen_meta
                strat_ret = eq / m["prev_strat"] if m["prev_strat"] > 0 else 1.0
                btc_ret = btc / m["prev_btc"] if m["prev_btc"] > 0 else 1.0
                chosen = m["chosen"] * (strat_ret if m["mode"] == "strategy" else btc_ret)
                strat_mult = eq / initial
                bh_mult = btc / m["btc_ref"]
                margin = 1.0 + switch_margin
                mode = m["mode"]
                if mode == "strategy" and bh_mult > strat_mult * margin:
                    mode, chosen, switches = "btc", chosen * (1 - fee_pct), switches + 1
                elif mode == "btc" and strat_mult > bh_mult * margin:
                    mode, chosen, switches = "strategy", chosen * (1 - fee_pct), switches + 1
                chosen_meta = {"btc_ref": m["btc_ref"], "chosen": chosen, "mode": mode,
                               "prev_strat": eq, "prev_btc": btc}
            chosen_curve.append(chosen_meta["chosen"])

        # curva buy & hold (mismo instante que la de la estrategia)
        if bh_units:
            bh_curve.append(sum(u * prices.get(s, 0.0) for s, u in bh_units.items()))

    # 5) cerrar lo abierto al final (como close_all al apagar)
    for pos in list(positions):
        do_close(pos, prices.get(pos["symbol"], pos["entry"]), "Cierre final")
    final = cash

    # métricas
    peak = initial; max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    bh_final = sum(u * float(frames[s]["close"].iloc[-1]) for s, u in bh_units.items())
    chosen_final = chosen_curve[-1] if chosen_curve else initial
    ppy = _BARS_PER_YEAR.get(interval, 365)
    sharpe_strat = metrics.sharpe(equity_curve, ppy)
    sharpe_chosen = metrics.sharpe(chosen_curve, ppy)
    sharpe_bh = metrics.sharpe(bh_curve, ppy)

    return {
        "symbols": list(frames.keys()),
        "skipped": skipped,
        "interval": interval,
        "n_steps": len(equity_curve),
        "initial": round(initial, 2),
        "final_equity": round(final, 2),
        "return_pct": round((final / initial - 1) * 100, 2),
        "buyhold_equity": round(bh_final, 2),
        "buyhold_pct": round((bh_final / initial - 1) * 100, 2) if bh_final else 0.0,
        "buyhold_sharpe": round(sharpe_bh, 2) if sharpe_bh is not None else None,
        "trades": trades,
        "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe_strat, 2) if sharpe_strat is not None else None,
        "exit_reasons": exit_reasons,
        "chosen_equity": round(chosen_final, 2),
        "chosen_pct": round((chosen_final / initial - 1) * 100, 2),
        "chosen_sharpe": round(sharpe_chosen, 2) if sharpe_chosen is not None else None,
        "chosen_switches": switches,
        "equity_curve": [round(v, 2) for v in equity_curve],
    }
