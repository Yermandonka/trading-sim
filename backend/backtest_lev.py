"""Backtest de cartera con APALANCAMIENTO (futuros perpetuos tipo Kraken/OKX).

Replica la lógica del motor (ensemble, régimen, stops por ATR, trailing, giro de
señal) pero con la mecánica real de un perpetuo con margen AISLADO:

  - nominal = margen · apalancamiento; el tamaño se fija por RIESGO: la fracción
    del capital que se pierde si salta el stop inicial (`risk_per_trade`), no
    por "cuánto margen pongo". Así el apalancamiento solo decide cuánto margen
    hay que bloquear, no cuánto se arriesga.
  - comisión taker sobre el NOMINAL al abrir y cerrar + deslizamiento.
  - financiación cada 8 h sobre el nominal (largos pagan, cortos cobran, o
    ambos pagan si `funding_mode == "both_pay"`).
  - LIQUIDACIÓN: margen de mantenimiento = la mitad del inicial (como Kraken).
    Una posición se liquida si la pérdida latente llega al 50 % de su margen.
    Se comprueba con el máximo/mínimo de la vela, no solo con el cierre.
  - stops también comprobados con máximo/mínimo de la vela (más honesto).
  - límites de seguridad: uso máximo de margen total (`max_margin_use`), tope de
    nominal por posición, límite de pérdida diaria (cierra todo y para hasta el
    día siguiente) y cortacircuitos por caída desde máximos (para el bot).

Trabaja sobre señales precalculadas (dict símbolo -> DataFrame con columnas
ts, open, high, low, close, atr, score, regime, conf) para poder barrer muchas
configuraciones en segundos.
"""
from __future__ import annotations

import math

import pandas as pd

from . import metrics

_BARS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "2h": 4380, "4h": 2190, "6h": 1460, "12h": 730, "1d": 365,
}
_INTERVAL_SECS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                  "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}

LEV_DEFAULTS = {
    "leverage": 1.0,            # apalancamiento nominal/margen
    "risk_per_trade": 0.01,     # fracción del capital que se pierde si salta el stop inicial
    "max_margin_use": 0.5,      # margen total bloqueado / capital (resto = colchón)
    "max_notional_per_pos": 0.3,  # nominal de UNA posición / capital (tope absoluto)
    "max_positions": 8,
    "taker_fee": 0.0005,        # 0,05 % del nominal por lado (Kraken perps, taker)
    "slippage": 0.0002,         # 0,02 % adverso en cada ejecución
    "funding_8h": 0.0001,       # 0,01 % del nominal cada 8 h
    "funding_mode": "longs_pay",  # "longs_pay" (cortos cobran) | "both_pay"
    "maint_ratio": 0.5,         # margen de mantenimiento = 50 % del inicial
    "daily_loss_limit": 0.03,   # si el día pierde >3 % del capital: cerrar todo y parar hasta mañana
    "dd_halt": 0.20,            # si cae >20 % desde máximos: cortacircuitos (pausa)
    "halt_days": 14,            # días de pausa tras el cortacircuitos (0 = parar para siempre)
    "buy_threshold": 0.30,
    "allow_short": True,
    "stop_atr_mult": 2.2,
    "take_atr_mult": 8.0,
    "use_trailing": True,
    "trail_atr_mult": 3.0,
    "trend_ride": True,
    "ride_trail_mult": 2.0,
    "regime_filter": True,
    "cooldown_bars": 0,         # tras salir por stop, velas sin reentrar en ese activo (menos rotación)
    "sizing_mode": "risk",      # "risk" (por distancia al stop) | "margin" (antiguo: eq·risk como margen)
    "rank_by_score": False,     # abrir primero las señales más fuertes de cada vela (no en orden alfabético)
    "start_ts": 0,              # ignorar velas anteriores (p. ej. para replicar un periodo en vivo)
    "end_ts": 0,
    "blackouts": [],            # [(desde, hasta)] apagados: se cierra todo al empezar y no se opera dentro
    "initial_points": 10000.0,
}


def run(signals: dict[str, pd.DataFrame], interval: str = "15m",
        cfg: dict | None = None) -> dict:
    c = {**LEV_DEFAULTS, **(cfg or {})}
    L = float(c["leverage"])
    risk = float(c["risk_per_trade"])
    max_mu = float(c["max_margin_use"])
    max_np = float(c["max_notional_per_pos"])
    max_pos = int(c["max_positions"])
    fee = float(c["taker_fee"])
    slip = float(c["slippage"])
    fund = float(c["funding_8h"])
    both_pay = c["funding_mode"] == "both_pay"
    maint = float(c["maint_ratio"])
    liq_move = (1.0 / L) * (1.0 - maint)   # movimiento adverso que liquida (p.ej. 10x -> 5 %)
    dll = float(c["daily_loss_limit"])
    dd_halt = float(c["dd_halt"])
    halt_days = float(c.get("halt_days", 14))
    buy_th = float(c["buy_threshold"])
    allow_short = bool(c["allow_short"])
    sm, tm = float(c["stop_atr_mult"]), float(c["take_atr_mult"])
    use_trail = bool(c["use_trailing"])
    trend_ride = bool(c["trend_ride"])
    base_trail = float(c["trail_atr_mult"])
    ride_mult = float(c["ride_trail_mult"])
    regime_filter = bool(c["regime_filter"])
    cooldown = int(c.get("cooldown_bars", 0))
    initial = float(c["initial_points"])
    secs = _INTERVAL_SECS.get(interval, 900)
    bars_per_8h = max(1, int(28800 / secs))

    # índice: símbolo -> {ts: fila}
    idx = {}
    all_ts = set()
    for s, df in signals.items():
        m = {int(t): i for i, t in enumerate(df["ts"].values)}
        idx[s] = m
        all_ts.update(m.keys())
    timeline = sorted(all_ts)
    st, en = float(c.get("start_ts", 0) or 0), float(c.get("end_ts", 0) or 0)
    timeline = [t for t in timeline if t >= st and (not en or t <= en)]
    recs = {s: df.to_dict("records") for s, df in signals.items()}

    cash = initial          # caja libre (fuera de margen)
    positions = []
    prices = {}
    curve = []
    trades = wins = 0
    liquidations = 0
    exit_reasons = {}
    gross_profit = gross_loss = 0.0
    fees_paid = funding_paid = 0.0
    peak_eq = initial
    halted = False
    halted_until = 0
    halts = 0
    day_start_eq = initial
    cur_day = None
    paused_day = None
    bar_i = 0
    daily_stops = 0
    cooldown_until = {}     # símbolo -> vela hasta la que no se reentra
    trade_log = []          # operaciones cerradas (para análisis)

    def unreal(p, price):
        return p["qty"] * (price - p["entry"]) if p["side"] == "long" else p["qty"] * (p["entry"] - price)

    def equity():
        return cash + sum(p["margin"] + unreal(p, prices.get(p["symbol"], p["entry"])) for p in positions)

    def margin_used():
        return sum(p["margin"] for p in positions)

    def do_open(sym, side, price, atr, conf):
        nonlocal cash, fees_paid
        eq = equity()
        if eq <= 0:
            return
        stop_dist = atr * sm
        if stop_dist <= 0:
            return
        # tamaño por riesgo: pérdida al stop = eq·risk (escalado por confianza)
        if c.get("sizing_mode", "risk") == "margin":
            notional = eq * risk * (0.55 + 0.45 * conf) * L
        else:
            risk_amt = eq * risk * (0.55 + 0.45 * conf)
            notional = risk_amt / (stop_dist / price)
            notional = min(notional, eq * max_np)
        margin = notional / L
        free = eq * max_mu - margin_used()
        if margin > free:
            margin = free
            notional = margin * L
        if margin < 1 or notional < 5:
            return
        fill = price * (1 + slip) if side == "long" else price * (1 - slip)
        qty = notional / fill
        f = notional * fee
        cash -= margin + f
        fees_paid += f
        stop = fill - stop_dist if side == "long" else fill + stop_dist
        take = fill + atr * tm if side == "long" else fill - atr * tm
        liq = fill * (1 - liq_move) if side == "long" else fill * (1 + liq_move)
        positions.append({"symbol": sym, "side": side, "qty": qty, "entry": fill,
                          "margin": margin, "stop": stop, "take": take, "liq": liq,
                          "peak": fill, "atr": atr, "bar": bar_i, "entry_t": cur_t})

    def do_close(p, price, reason, liquidated=False):
        nonlocal cash, trades, wins, liquidations, fees_paid, gross_profit, gross_loss
        if liquidated:
            # se pierde todo el margen (la casa se queda el resto como comisión de liquidación)
            pnl = -p["margin"]
            liquidations += 1
        else:
            fill = price * (1 - slip) if p["side"] == "long" else price * (1 + slip)
            f = p["qty"] * fill * fee
            fees_paid += f
            pnl = unreal(p, fill) - f
            cash += p["margin"] + pnl
        positions.remove(p)
        trade_log.append({"symbol": p["symbol"], "side": p["side"], "entry_ts": p.get("entry_t"),
                          "exit_ts": cur_t, "entry": p["entry"], "exit": price, "pnl": round(pnl, 2), "reason": reason})
        if cooldown and reason in ("Stop dinámico", "Stop-loss", "LIQUIDACIÓN"):
            cooldown_until[p["symbol"]] = bar_i + cooldown
        trades += 1
        if pnl > 0:
            wins += 1; gross_profit += pnl
        else:
            gross_loss += -pnl
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    def close_all(reason):
        for p in list(positions):
            do_close(p, prices.get(p["symbol"], p["entry"]), reason)

    blackouts = [(float(a), float(b)) for a, b in (c.get("blackouts") or [])]
    cur_t = timeline[0] if timeline else 0
    for t in timeline:
        cur_t = t
        bar_i += 1
        day = t // 86400
        if any(a <= t < b for a, b in blackouts):
            # equipo apagado: el bot cerró todo al apagarse y no opera hasta volver
            for s_ in idx:
                i_ = idx[s_].get(t)
                if i_ is not None:
                    prices[s_] = recs[s_][i_]["close"]
            if positions:
                close_all("Apagado del equipo")
            curve.append(equity())
            continue
        if halted and halt_days > 0 and t >= halted_until:
            halted = False
            peak_eq = equity()      # reanuda y mide la caída desde aquí
        if cur_day is None:
            cur_day = day
        if day != cur_day:
            cur_day = day
            day_start_eq = equity()
        # financiación cada 8 h sobre el nominal de lo abierto
        if bar_i % bars_per_8h == 0 and fund > 0:
            for p in positions:
                notional = p["qty"] * prices.get(p["symbol"], p["entry"])
                if p["side"] == "long" or both_pay:
                    cash -= notional * fund; funding_paid += notional * fund
                else:
                    cash += notional * fund; funding_paid -= notional * fund

        order = list(idx.items())
        if c.get("rank_by_score"):
            order.sort(key=lambda kv: -abs(recs[kv[0]][kv[1][t]]["score"]) if t in kv[1] else 0)
        for s, m in order:
            i = m.get(t)
            if i is None:
                continue
            r = recs[s][i]
            price, hi, lo, atr, score = r["close"], r["high"], r["low"], r["atr"], r["score"]
            regime = int(r["regime"])
            prices[s] = price
            p = next((q for q in positions if q["symbol"] == s), None)
            if p:
                if p["bar"] == bar_i:
                    continue  # abierta en esta misma vela
                riding = trend_ride and ((p["side"] == "long" and regime > 0) or
                                         (p["side"] == "short" and regime < 0))
                trail = base_trail * (ride_mult if riding else 1.0) * p["atr"]
                if p["side"] == "long":
                    # 1) liquidación / stop con el mínimo de la vela
                    if lo <= p["liq"] and p["liq"] >= p["stop"]:
                        do_close(p, p["liq"], "LIQUIDACIÓN", liquidated=True); continue
                    if lo <= p["stop"]:
                        do_close(p, p["stop"], "Stop dinámico" if use_trail else "Stop-loss"); continue
                    if use_trail:
                        p["peak"] = max(p["peak"], hi)
                        p["stop"] = max(p["stop"], p["peak"] - trail)
                    if riding:
                        if regime < 0:
                            do_close(p, price, "Fin de tendencia")
                    elif hi >= p["take"]:
                        do_close(p, p["take"], "Take-profit")
                    elif score <= -buy_th:
                        do_close(p, price, "Giro de señal")
                else:
                    if hi >= p["liq"] and p["liq"] <= p["stop"]:
                        do_close(p, p["liq"], "LIQUIDACIÓN", liquidated=True); continue
                    if hi >= p["stop"]:
                        do_close(p, p["stop"], "Stop dinámico" if use_trail else "Stop-loss"); continue
                    if use_trail:
                        p["peak"] = min(p["peak"], lo)
                        p["stop"] = min(p["stop"], p["peak"] + trail)
                    if riding:
                        if regime > 0:
                            do_close(p, price, "Fin de tendencia")
                    elif lo <= p["take"]:
                        do_close(p, p["take"], "Take-profit")
                    elif score >= buy_th:
                        do_close(p, price, "Giro de señal")
            else:
                if halted or paused_day == day or len(positions) >= max_pos:
                    continue
                if cooldown_until.get(s, 0) > bar_i:
                    continue
                side = None
                if score >= buy_th:
                    side = "long"
                elif allow_short and score <= -buy_th:
                    side = "short"
                if side:
                    if regime_filter and ((side == "long" and regime < 0) or (side == "short" and regime > 0)):
                        continue
                    do_open(s, side, price, atr, float(r["conf"]))

        eq = equity()
        # seguridad: límite de pérdida diaria y cortacircuitos por drawdown
        if dll > 0 and paused_day != day and eq < day_start_eq * (1 - dll):
            close_all("Límite diario"); paused_day = day; daily_stops += 1
            eq = equity()
        peak_eq = max(peak_eq, eq)
        if dd_halt > 0 and not halted and eq < peak_eq * (1 - dd_halt):
            close_all("Cortacircuitos"); halted = True; halts += 1
            halted_until = t + halt_days * 86400
            eq = equity()
        curve.append(eq)

    close_all("Cierre final")
    final = cash
    peak = initial; max_dd = 0.0
    for v in curve:
        peak = max(peak, v); max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    n_days = (timeline[-1] - timeline[0]) / 86400 if len(timeline) > 1 else 1
    ret = final / initial - 1
    monthly = ((final / initial) ** (30 / n_days) - 1) if final > 0 and n_days > 0 else -1.0
    ppy = _BARS_PER_YEAR.get(interval, 35040)
    sh = metrics.sharpe(curve, ppy)
    return {
        "interval": interval, "days": round(n_days, 1), "leverage": L,
        "initial": initial, "final": round(final, 2),
        "return_pct": round(ret * 100, 2), "monthly_pct": round(monthly * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sh, 2) if sh is not None else None,
        "trades": trades, "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "liquidations": liquidations, "daily_stops": daily_stops, "halted": halted, "halts": halts,
        "fees": round(fees_paid, 2), "funding": round(funding_paid, 2),
        "exit_reasons": exit_reasons,
        "trade_log": trade_log,
        "equity_curve": [round(v, 2) for v in curve],
    }


def build_signals(symbols: list[str], interval: str = "1h", limit: int = 1000,
                  window: int = 400, weights: dict | None = None) -> dict[str, pd.DataFrame]:
    """Descarga velas y precalcula las señales del ensemble con ventana deslizante
    de `window` velas (igual que el motor en vivo). Lento: ~1 s por cada 100 velas
    y activo. Para barridos largos conviene cachear el resultado."""
    from . import data, strategies
    out = {}
    for s in symbols:
        try:
            df = data.get_ohlcv(s, interval, limit=limit, max_age=60)
        except Exception:  # noqa: BLE001
            continue
        if len(df) > 1:
            df = df.iloc[:-1]        # vela en formación fuera
        if len(df) < window + 10:
            continue
        rows = []
        for i in range(window - 1, len(df)):
            w = df.iloc[i - window + 1: i + 1]
            r = strategies.analyze(w, weights)
            rows.append((int(df.index[i].timestamp()), float(df["open"].iloc[i]),
                         float(df["high"].iloc[i]), float(df["low"].iloc[i]),
                         r["price"], r["atr"], r["score"], r["regime"], r["confidence"]))
        out[s] = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                             "atr", "score", "regime", "conf"])
    return out
