"""Motor de paper-trading: bucle de análisis, decisión y ejecución simulada.

Mantiene una cartera PRINCIPAL (la configuración que estás probando) y, opcionalmente,
carteras SOMBRA: otras configuraciones que operan EN VIVO en paralelo, con los mismos
datos y en el mismo instante, cada una con su propio capital ficticio. Sirven para
comparar estrategias de verdad (mismo mercado, mismo momento), no con históricos.

Simula una cuenta de futuros perpetuos: apalancamiento, tamaño por riesgo, margen,
liquidación, financiación cada 8 h y cortacircuitos. Pensado para correr 24h.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import traceback

from . import ai_filter
from . import config as cfg_mod
from . import data
from . import storage
from . import strategies


class Portfolio:
    """Una cartera simulada con su configuración, caja, posiciones y seguridad."""

    def __init__(self, name: str, cfg: dict, state: dict | None = None, shadow: bool = False,
                 overrides: dict | None = None, color: str | None = None):
        self.name = name
        self.color = color
        self.cfg = cfg
        self.shadow = shadow
        self.overrides = overrides or {}
        self.prices: dict[str, float] = {}
        self.analysis: dict[str, dict] = {}
        self.universe: list[str] = []
        self._universe_ts = 0.0
        st = state or {}
        self.cash = float(st.get("cash", cfg["initial_points"]))
        self.realized = float(st.get("realized", 0.0))
        self.positions: list[dict] = st.get("positions", [])
        self.peak_eq = float(st.get("peak_eq", 0.0) or 0.0)
        self.paused_until = float(st.get("paused_until", 0.0) or 0.0)
        self.last_funding = float(st.get("last_funding", 0.0) or 0.0)

    # ---------- persistencia ----------
    def to_state(self) -> dict:
        return {"cash": self.cash, "realized": self.realized, "positions": self.positions,
                "peak_eq": self.peak_eq, "paused_until": self.paused_until,
                "last_funding": self.last_funding}

    def _log_trade(self, ts, symbol, side, action, qty, price, pnl, reason):
        storage.log_trade(ts, symbol, side, action, qty, price, pnl, reason,
                          portfolio=self.name if self.shadow else None)

    # ---------- valoración ----------
    def _unrealized(self, pos: dict, price: float) -> float:
        if pos["side"] == "long":
            return pos["qty"] * (price - pos["entry"])
        return pos["qty"] * (pos["entry"] - price)

    def _position_value(self, pos: dict, price: float) -> float:
        """Margen + P/L latente (vale para largos, cortos y cualquier apalancamiento)."""
        return pos["margin"] + self._unrealized(pos, price)

    def equity(self) -> float:
        total = self.cash
        for p in self.positions:
            total += self._position_value(p, self.prices.get(p["symbol"], p["entry"]))
        return total

    def pos_for(self, symbol):
        for p in self.positions:
            if p["symbol"] == symbol:
                return p
        return None

    # ---------- universo ----------
    def resolve_universe(self) -> list[str]:
        cfg = self.cfg
        now = time.time()
        if cfg.get("auto_universe"):
            if not self.universe or (now - self._universe_ts) > 1800:
                try:
                    self.universe = data.top_symbols(int(cfg.get("universe_size", 40)))
                    self._universe_ts = now
                except Exception:  # noqa: BLE001
                    if not self.universe:
                        self.universe = list(cfg["watchlist"])
        else:
            self.universe = list(cfg["watchlist"])
        open_syms = [p["symbol"] for p in self.positions]
        return list(dict.fromkeys(list(self.universe) + open_syms))

    # ---------- operaciones ----------
    def _open(self, symbol, side, price, atr, score, reason, confidence=0.5):
        cfg = self.cfg
        eq = self.equity()
        if eq <= 0 or price <= 0:
            return
        L = max(1.0, float(cfg.get("leverage", 1.0)))
        risk = float(cfg["risk_per_trade"])
        conf_mult = 0.55 + 0.45 * float(confidence)
        sm = float(cfg["stop_atr_mult"])
        tm = float(cfg["take_atr_mult"])
        stop_dist = atr * sm
        if cfg.get("sizing_mode", "risk") == "risk":
            if stop_dist <= 0:
                return
            notional = eq * risk * conf_mult / (stop_dist / price)
            notional = min(notional, eq * float(cfg.get("max_notional_per_pos", 0.3)))
        else:
            notional = eq * risk * conf_mult * L
        margin = notional / L
        used = sum(p["margin"] for p in self.positions)
        free = eq * float(cfg.get("max_margin_use", 1.0)) - used
        margin = min(margin, free)
        fee_pct = float(cfg["fee_pct"])
        margin = min(margin, self.cash * 0.98 / (1.0 + L * fee_pct))
        notional = margin * L
        if margin < 1 or notional < 5:
            return
        fee = notional * fee_pct
        qty = notional / price
        maint = float(cfg.get("maint_ratio", 0.5))
        if side == "long":
            stop, take = price - stop_dist, price + atr * tm
            liq = price * (1.0 - (1.0 / L) * (1.0 - maint))
        else:
            stop, take = price + stop_dist, price - atr * tm
            liq = price * (1.0 + (1.0 / L) * (1.0 - maint))
        self.cash -= margin + fee
        self.realized -= fee
        self.positions.append({
            "symbol": symbol, "side": side, "qty": qty, "entry": price,
            "margin": margin, "notional": notional, "leverage": L, "liq": liq,
            "stop": stop, "take": take, "peak": price, "atr": atr,
            "entry_ts": time.time(), "score": score, "reason": reason,
        })
        self._log_trade(time.time(), symbol, side, "ABRIR", qty, price, 0.0, reason)

    def _maybe_open(self, symbol, side, price, atr, score, res):
        """Los algoritmos deciden; régimen e IA (solo cartera principal) filtran."""
        if self.cfg.get("regime_filter", True):
            regime = res.get("regime", 0)
            if (side == "long" and regime < 0) or (side == "short" and regime > 0):
                return
        reason = f"Ensemble {score:+.2f}: señal " + ("alcista" if side == "long" else "bajista")
        if not self.shadow and ai_filter.enabled(self.cfg):
            verdict = ai_filter.vet(symbol, side, res, self.cfg)
            res["ai"] = verdict
            self.analysis[symbol] = res
            if not verdict.get("approve", True):
                storage.log_veto(time.time(), symbol, side, price, score, verdict.get("reason", ""))
                return
            reason += f" · IA ✓: {verdict.get('reason', '')}"
        self._open(symbol, side, price, atr, score, reason, res.get("confidence", 0.5))

    def _close(self, pos, price, reason, liquidated=False):
        if liquidated:
            pnl = -pos["margin"]
        else:
            fee = pos["qty"] * price * float(self.cfg["fee_pct"])
            pnl = self._unrealized(pos, price) - fee
            self.cash += pos["margin"] + pnl
        self.realized += pnl
        self.positions.remove(pos)
        self._log_trade(time.time(), pos["symbol"], pos["side"], "CERRAR",
                        pos["qty"], price, pnl, reason)

    def close_all(self, reason) -> int:
        n = len(self.positions)
        for pos in list(self.positions):
            self._close(pos, self.prices.get(pos["symbol"], pos["entry"]), reason)
        return n

    # ---------- un símbolo en un ciclo ----------
    def manage(self, symbol: str, res: dict, running: bool):
        cfg = self.cfg
        buy_th = float(cfg["buy_threshold"])
        allow_short = bool(cfg["allow_short"])
        price, atr, score = res["price"], res["atr"], res["score"]
        self.prices[symbol] = price
        if not running:
            return
        regime = res.get("regime", 0)
        pos = self.pos_for(symbol)
        if pos:
            liq = pos.get("liq")
            if liq and ((pos["side"] == "long" and price <= liq) or
                        (pos["side"] == "short" and price >= liq)):
                self._close(pos, liq, "LIQUIDACIÓN: se pierde el margen", liquidated=True)
                return
            use_trail = bool(cfg.get("use_trailing", True))
            trend_ride = bool(cfg.get("trend_ride", True))
            riding = trend_ride and ((pos["side"] == "long" and regime > 0)
                                     or (pos["side"] == "short" and regime < 0))
            trail = (float(cfg.get("trail_atr_mult", 3.0))
                     * (float(cfg.get("ride_trail_mult", 2.0)) if riding else 1.0) * pos.get("atr", atr))
            if pos["side"] == "long":
                if use_trail:
                    pos["peak"] = max(pos.get("peak", pos["entry"]), price)
                    pos["stop"] = max(pos["stop"], pos["peak"] - trail)
                if price <= pos["stop"]:
                    self._close(pos, price, "Stop dinámico" if use_trail else "Stop-loss")
                elif riding:
                    if regime < 0:
                        self._close(pos, price, "Fin de la tendencia alcista")
                elif price >= pos["take"]:
                    self._close(pos, price, "Take-profit")
                elif score <= -buy_th:
                    self._close(pos, price, "Señal se giró a bajista")
            else:
                if use_trail:
                    pos["peak"] = min(pos.get("peak", pos["entry"]), price)
                    pos["stop"] = min(pos["stop"], pos["peak"] + trail)
                if price >= pos["stop"]:
                    self._close(pos, price, "Stop dinámico" if use_trail else "Stop-loss")
                elif riding:
                    if regime > 0:
                        self._close(pos, price, "Fin de la tendencia bajista")
                elif price <= pos["take"]:
                    self._close(pos, price, "Take-profit")
                elif score >= buy_th:
                    self._close(pos, price, "Señal se giró a alcista")
        else:
            if self.paused_until or len(self.positions) >= int(cfg["max_positions"]):
                return
            side = None
            if score >= buy_th:
                side = "long"
            elif allow_short and score <= -buy_th:
                side = "short"
            if side:
                self._maybe_open(symbol, side, price, atr, score, res)

    # ---------- fin de ciclo: financiación, pausa, cortacircuitos ----------
    def begin_step(self, now: float):
        cfg = self.cfg
        fund = float(cfg.get("funding_8h", 0.0))
        if self.last_funding <= 0:
            self.last_funding = now
        elif fund > 0 and now - self.last_funding >= 28800 and self.positions:
            for p in self.positions:
                notional = p["qty"] * self.prices.get(p["symbol"], p["entry"])
                self.cash += -notional * fund if p["side"] == "long" else notional * fund
            self.last_funding = now
        if self.paused_until and now >= self.paused_until:
            self.paused_until = 0.0
            self.peak_eq = self.equity()

    def end_step(self, running: bool) -> float:
        cfg = self.cfg
        eq = self.equity()
        self.peak_eq = max(self.peak_eq, eq)
        dd_halt = float(cfg.get("dd_halt", 0.0))
        if running and dd_halt > 0 and not self.paused_until and eq < self.peak_eq * (1 - dd_halt):
            self.close_all(f"Cortacircuitos: caída del {dd_halt*100:.0f}% desde máximos")
            self.paused_until = time.time() + float(cfg.get("halt_days", 14)) * 86400
            eq = self.equity()
        return eq

    def reset(self):
        self.cash = float(self.cfg["initial_points"])
        self.realized = 0.0
        self.positions = []
        self.analysis = {}
        self.peak_eq = self.cash
        self.paused_until = 0.0
        self.last_funding = 0.0

    def label(self) -> str:
        c = self.cfg
        L = float(c.get("leverage", 1.0))
        risk = float(c["risk_per_trade"]) * 100
        modo = "riesgo" if c.get("sizing_mode", "risk") == "risk" else "margen"
        lab = (f"{c['interval']} · umbral {c['buy_threshold']} · {modo} {risk:g}% · "
               f"stop {c['stop_atr_mult']:g} ATR · {L:g}x")
        if not self.shadow and ai_filter.enabled(c):
            lab += " · IA"
        return lab


class CarryPortfolio(Portfolio):
    """Cash-and-carry: largo al contado + corto en el perpetuo por el mismo importe.
    El precio deja de importar; el ingreso es la financiación que pagan los largos
    cada 8 h. Entra cuando la financiación anualizada supera `carry_min_apr` y sale
    cuando lleva `carry_exit_periods` pagos seguidos sin cobrar (tasa <= 0).
    El corto va apalancado `carry_leverage`x con margen aislado; si el precio sube y
    el margen se agota, se repone desde la caja (reserva); sin reserva, se cierra."""
    kind = "carry"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.funding: dict[str, dict] = {}
        self._fund_ts = 0.0
        st = k.get("state") or (a[2] if len(a) > 2 else None) or {}
        self.neg_streak: dict[str, int] = dict(st.get("neg_streak", {}))

    def to_state(self) -> dict:
        return {**super().to_state(), "neg_streak": self.neg_streak}

    def reset(self):
        super().reset()
        self.neg_streak = {}

    def label(self) -> str:
        c = self.cfg
        return (f"cash&carry · {float(c.get('carry_leverage', 2)):g}x corto · entra ≥"
                f"{float(c.get('carry_min_apr', 0.05))*100:g} % anual")

    def resolve_universe(self) -> list[str]:
        syms = list(self.cfg.get("carry_symbols") or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"])
        return list(dict.fromkeys(syms + [p["symbol"] for p in self.positions]))

    # posición cubierta: vale lo que costó el contado + el margen del corto (el precio no afecta)
    def _unrealized(self, pos: dict, price: float) -> float:
        return 0.0

    def _position_value(self, pos: dict, price: float) -> float:
        return pos["spot_cost"] + pos["margin"]

    def begin_step(self, now: float):
        cfg = self.cfg
        if now - self._fund_ts > 300:
            try:
                self.funding = data.funding_rates(self.resolve_universe())
                self._fund_ts = now
            except Exception:  # noqa: BLE001
                pass
        exit_periods = int(cfg.get("carry_exit_periods", 3))
        # cobro/pago de financiación en cada frontera de 8 h
        for p in list(self.positions):
            while now >= p["next_funding_ts"]:
                f = self.funding.get(p["symbol"], {})
                rate = float(f.get("rate", p.get("rate", 0.0)))
                price = self.prices.get(p["symbol"], p["entry"])
                amount = p["qty"] * price * rate          # el corto COBRA si la tasa es positiva
                self.cash += amount
                self.realized += amount
                p["funding_income"] = p.get("funding_income", 0.0) + amount
                p["rate"] = rate
                p["next_funding_ts"] += 28800
                self.neg_streak[p["symbol"]] = (self.neg_streak.get(p["symbol"], 0) + 1) if rate <= 0 else 0
                if self.neg_streak[p["symbol"]] >= exit_periods:
                    self._close_carry(p, price, f"Carry: {exit_periods} pagos seguidos sin cobrar")
                    break
        if self.paused_until and now >= self.paused_until:
            self.paused_until = 0.0
            self.peak_eq = self.equity()

    def manage(self, symbol: str, res: dict, running: bool):
        cfg = self.cfg
        price = res["price"]
        self.prices[symbol] = price
        if not running:
            return
        f = self.funding.get(symbol)
        pos = self.pos_for(symbol)
        if pos:
            # el corto pierde si el precio sube: reponer margen desde la caja antes de la liquidación
            L = pos["leverage"]
            maint = float(cfg.get("maint_ratio", 0.5))
            perp_loss = pos["qty"] * (price - pos["entry"])     # >0 si el precio subió
            if perp_loss > pos["margin"] * (1 - maint) * 0.8:
                topup = pos["qty"] * price / L
                if self.cash >= topup:
                    self.cash -= topup
                    pos["margin"] += topup
                    pos["spot_cost"] = pos["qty"] * pos["entry"]  # sin cambios; solo margen
                    self._log_trade(time.time(), symbol, "carry", "MARGEN", pos["qty"], price, 0.0,
                                    f"Carry: aporte de margen {topup:.0f} al corto")
                else:
                    self._close_carry(pos, price, "Carry: sin caja para reponer margen")
            return
        if self.paused_until or len(self.positions) >= int(cfg.get("carry_max_positions", 3)):
            return
        if not f:
            return
        min_apr = float(cfg.get("carry_min_apr", 0.05))
        if f["apr"] >= min_apr and f["rate"] > 0:
            self._open_carry(symbol, price, f)

    def _open_carry(self, symbol, price, f):
        cfg = self.cfg
        eq = self.equity()
        L = max(1.0, float(cfg.get("carry_leverage", 2.0)))
        alloc = eq * float(cfg.get("carry_alloc", 0.85)) / int(cfg.get("carry_max_positions", 3))
        fee_spot = float(cfg.get("carry_fee_spot", 0.001))
        fee_perp = float(cfg.get("carry_fee_perp", 0.0005))
        # alloc = contado + margen del corto (= contado / L)
        spot = alloc * L / (L + 1)
        spot = min(spot, self.cash * 0.95 / (1 + 1 / L + fee_spot + fee_perp))
        if spot < 10:
            return
        margin = spot / L
        qty = spot / price
        fee = spot * fee_spot + spot * fee_perp
        self.cash -= spot + margin + fee
        self.realized -= fee
        maint = float(cfg.get("maint_ratio", 0.5))
        self.positions.append({
            "symbol": symbol, "side": "carry", "qty": qty, "entry": price,
            "spot_cost": spot, "margin": margin, "notional": spot, "leverage": L,
            "liq": price * (1 + (1 / L) * (1 - maint)),
            "stop": 0.0, "take": 0.0, "peak": price, "atr": 0.0,
            "rate": float(f["rate"]), "next_funding_ts": float(f.get("next_ts") or (int(time.time() // 28800) + 1) * 28800),
            "funding_income": 0.0, "fees": fee, "entry_ts": time.time(), "score": 0.0,
            "reason": f"Carry: financiación {f['apr']*100:.1f} % anual",
        })
        self._log_trade(time.time(), symbol, "carry", "ABRIR", qty, price, 0.0,
                        f"Carry: largo contado + corto perp · financiación {f['apr']*100:.1f} % anual")

    def _close_carry(self, pos, price, reason):
        cfg = self.cfg
        fee = pos["qty"] * price * (float(cfg.get("carry_fee_spot", 0.001)) + float(cfg.get("carry_fee_perp", 0.0005)))
        # contado + margen + P/L del corto - comisión  (= coste del contado + margen - comisión)
        self.cash += pos["qty"] * price + pos["margin"] + pos["qty"] * (pos["entry"] - price) - fee
        self.realized -= fee
        self.positions.remove(pos)
        net = pos.get("funding_income", 0.0) - pos.get("fees", 0.0) - fee
        self._log_trade(time.time(), pos["symbol"], "carry", "CERRAR", pos["qty"], price, net, reason)

    def _close(self, pos, price, reason, liquidated=False):
        self._close_carry(pos, price, reason)


class Engine:
    def __init__(self):
        self.cfg = cfg_mod.load()
        self.lock = threading.RLock()
        self.running = False
        self.thread: threading.Thread | None = None
        self.stop_flag = False
        self.last_step = 0.0
        self.last_error = ""
        state = storage.load_state() or {}
        self.main = Portfolio("principal", self.cfg, state if state else None)
        self.running = bool(state.get("running", False))
        self.chosen_meta = state.get("chosen_meta")
        self._btc_cache = (0.0, 0.0)
        self.shadows: list[Portfolio] = []
        self._build_shadows(state.get("shadows", {}))

    def _build_shadows(self, states: dict):
        self.shadows = []
        for sh in self.cfg.get("shadows", []) or []:
            name = sh.get("name") or f"sombra {len(self.shadows)+1}"
            ov = sh.get("overrides", {})
            cfg = {**self.cfg, **ov, "weights": {**self.cfg["weights"], **ov.get("weights", {})}}
            cls = CarryPortfolio if ov.get("kind") == "carry" else Portfolio
            self.shadows.append(cls(name, cfg, states.get(name), shadow=True, overrides=ov,
                                    color=sh.get("color")))

    def _refresh_shadow_cfgs(self):
        colors = {x.get("name"): x.get("color") for x in (self.cfg.get("shadows") or [])}
        for sh in self.shadows:
            sh.color = colors.get(sh.name, sh.color)
            ov = sh.overrides
            sh.cfg = {**self.cfg, **ov, "weights": {**self.cfg["weights"], **ov.get("weights", {})}}

    # atajos de compatibilidad (la API y el panel miran la cartera principal)
    @property
    def cash(self): return self.main.cash
    @property
    def realized(self): return self.main.realized
    @property
    def positions(self): return self.main.positions
    @property
    def prices(self): return self.main.prices
    @property
    def analysis(self): return self.main.analysis
    @property
    def universe(self): return self.main.universe

    def equity(self) -> float:
        return self.main.equity()

    def _persist(self):
        storage.save_state({
            **self.main.to_state(),
            "running": self.running,
            "chosen_meta": self.chosen_meta,
            "shadows": {sh.name: sh.to_state() for sh in self.shadows},
        })

    # ---------- datos ----------
    def _fetch_analyze(self, symbol: str, interval: str):
        df = data.get_ohlcv(symbol, interval, limit=400)
        return (symbol, interval), strategies.analyze(df, self.cfg["weights"])

    # ---------- cartera "elegida": sigue a la mejor estrategia ----------
    def _btc_price(self) -> float:
        now = time.time()
        if now - self._btc_cache[0] < 30 and self._btc_cache[1] > 0:
            return self._btc_cache[1]
        p = self.main.prices.get("BTCUSDT") or 0.0
        if p <= 0:
            try:
                df = data.get_ohlcv("BTCUSDT", self.cfg["interval"], limit=2, max_age=60)
                p = float(df["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                p = 0.0
        if p > 0:
            self._btc_cache = (now, p)
        return p

    def _update_chosen(self, strat_eq: float, ts: float):
        if not bool(self.cfg.get("follow_winner", True)):
            return
        btc = self._btc_price()
        if btc <= 0 or strat_eq <= 0:
            return
        init = float(self.cfg["initial_points"])
        m = self.chosen_meta
        if not m:
            self.chosen_meta = {"btc_ref": btc, "chosen": strat_eq, "mode": "strategy",
                                "prev_strat": strat_eq, "prev_btc": btc}
            storage.log_chosen(ts, strat_eq, "strategy", strat_eq)
            return
        strat_ret = strat_eq / m["prev_strat"] if m["prev_strat"] > 0 else 1.0
        btc_ret = btc / m["prev_btc"] if m["prev_btc"] > 0 else 1.0
        chosen = m["chosen"] * (strat_ret if m["mode"] == "strategy" else btc_ret)
        strat_mult = strat_eq / init
        bh_mult = btc / m["btc_ref"]
        margin = 1.0 + float(self.cfg.get("switch_margin", 0.005))
        fee = float(self.cfg.get("fee_pct", 0.001))
        mode = m["mode"]
        if mode == "strategy" and bh_mult > strat_mult * margin:
            mode, chosen = "btc", chosen * (1 - fee)
        elif mode == "btc" and strat_mult > bh_mult * margin:
            mode, chosen = "strategy", chosen * (1 - fee)
        self.chosen_meta = {"btc_ref": m["btc_ref"], "chosen": chosen, "mode": mode,
                            "prev_strat": strat_eq, "prev_btc": btc}
        storage.log_chosen(ts, chosen, mode, init * bh_mult)

    # ---------- un ciclo de evaluación ----------
    def step(self):
        self._refresh_shadow_cfgs()
        ports = [self.main] + self.shadows
        now = time.time()
        # 1) qué (símbolo, vela) necesita cada cartera; descarga y análisis en paralelo
        needs: dict[tuple[str, str], None] = {}
        universes = {}
        for p in ports:
            syms = p.resolve_universe()
            universes[p.name] = syms
            for s in syms:
                needs[(s, p.cfg["interval"])] = None
        results: dict[tuple[str, str], dict] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(self._fetch_analyze, s, iv): (s, iv) for (s, iv) in needs}
            for fut in concurrent.futures.as_completed(futs):
                key = futs[fut]
                try:
                    _, res = fut.result()
                    results[key] = res
                except Exception as e:  # noqa: BLE001
                    if key[1] == self.cfg["interval"]:
                        self.main.analysis.setdefault(key[0], {})["error"] = str(e)

        # 2) decisiones y ejecución, cartera a cartera (misma foto de mercado)
        for p in ports:
            p.begin_step(now)
            buy_th = float(p.cfg["buy_threshold"])
            for symbol in universes[p.name]:
                res = results.get((symbol, p.cfg["interval"]))
                if not res:
                    continue
                if p is self.main:
                    res["decision"] = strategies.plain_decision(res["score"], buy_th, bool(p.cfg["allow_short"]))
                    res.setdefault("ai", self.main.analysis.get(symbol, {}).get("ai"))
                    self.main.analysis[symbol] = res
                p.manage(symbol, res, self.running)

        self.last_step = time.time()
        eq = self.main.end_step(self.running)
        storage.log_equity(self.last_step, eq, self.main.cash)
        for sh in self.shadows:
            storage.log_shadow_equity(self.last_step, sh.name, sh.end_step(self.running))
        self._update_chosen(eq, self.last_step)
        self._persist()

    # ---------- hilo en segundo plano ----------
    def _run(self):
        while not self.stop_flag:
            try:
                with self.lock:
                    self.step()
                self.last_error = ""
            except Exception:  # noqa: BLE001
                self.last_error = traceback.format_exc().splitlines()[-1]
            for _ in range(int(self.cfg["loop_seconds"])):
                if self.stop_flag:
                    break
                time.sleep(1)

    def start_loop(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # ---------- controles ----------
    def start_bot(self):
        with self.lock:
            self.running = True
            self._persist()

    def stop_bot(self):
        with self.lock:
            self.running = False
            self._persist()

    def close_all(self, reason="Cierre automático por apagado"):
        """Cierra TODAS las posiciones (principal y sombras) y persiste."""
        with self.lock:
            n = self.main.close_all(reason)
            for sh in self.shadows:
                sh.close_all(reason)
            if n:
                storage.log_equity(time.time(), self.main.equity(), self.main.cash)
            self._persist()
            return n

    def run_label(self) -> str:
        return self.main.label()

    def reset(self, archive: bool = True):
        with self.lock:
            if archive:
                start = storage.equity_curve(1)
                when = time.strftime("%d %b", time.gmtime(start[0]["ts"])) if start else ""
                storage.archive_run(f"{when} · {self.run_label()}")
            storage.reset_history()
            self.main.reset()
            for sh in self.shadows:
                sh.reset()
            self.chosen_meta = None
            self._btc_cache = (0.0, 0.0)
            self._persist()

    def update_config(self, patch: dict):
        with self.lock:
            if "weights" in patch:
                self.cfg["weights"].update(patch.pop("weights"))
            # solo se reconstruyen las sombras si cambian nombres u overrides (no por el color)
            def _sig(lst): return [(x.get("name"), json.dumps(x.get("overrides", {}), sort_keys=True)) for x in (lst or [])]
            rebuild = "shadows" in patch and _sig(patch["shadows"]) != _sig(self.cfg.get("shadows"))
            self.cfg.update(patch)
            cfg_mod.save(self.cfg)
            if any(k in patch for k in ("auto_universe", "universe_size", "watchlist")):
                self.main._universe_ts = 0.0
            if any(k in patch for k in ("ai_enabled", "ai_api_key", "weights")):
                ai_filter.clear_cache()
            if rebuild:
                keep = {sh.name: sh.to_state() for sh in self.shadows
                        if (sh.name, json.dumps(sh.overrides, sort_keys=True)) in set(_sig(self.cfg.get("shadows")))}
                self._build_shadows(keep)
                self._persist()

    # ---------- resumen para el panel ----------
    def snapshot(self) -> dict:
        with self.lock:
            m = self.main
            eq = m.equity()
            init = float(self.cfg["initial_points"])
            invested = sum(m._position_value(p, m.prices.get(p["symbol"], p["entry"])) for p in m.positions)
            positions = []
            for p in m.positions:
                price = m.prices.get(p["symbol"], p["entry"])
                unreal = m._unrealized(p, price)
                base = p["margin"] if p["margin"] else p["qty"] * p["entry"]
                positions.append({**p, "price": price, "unreal": unreal,
                                  "unreal_pct": (unreal / base * 100) if base else 0.0})
            syms = m.universe or self.cfg["watchlist"]
            ordered = list(dict.fromkeys(list(syms) + [p["symbol"] for p in m.positions]))
            analyses = [{"symbol": s, **m.analysis[s]} for s in ordered
                        if m.analysis.get(s) and "score" in m.analysis[s]]
            analyses.sort(key=lambda x: -abs(x.get("score", 0)))
            shadows = []
            for sh in self.shadows:
                seq = sh.equity()
                sinit = float(sh.cfg["initial_points"])
                shadows.append({
                    "name": sh.name, "label": sh.label(), "equity": seq, "color": sh.color,
                    "initial": sinit,
                    "pnl_pct": (seq - sinit) / sinit * 100 if sinit else 0.0,
                    "open_positions": len(sh.positions), "realized": sh.realized,
                    "paused_until": sh.paused_until,
                })
            return {
                "running": self.running,
                "loop_running": bool(self.thread and self.thread.is_alive()),
                "universe_size": len(syms),
                "ai_active": ai_filter.enabled(self.cfg),
                "ai_stats": ai_filter.get_stats(self.cfg),
                "equity": eq, "cash": m.cash, "invested": invested, "realized": m.realized,
                "initial": init, "pnl_total": eq - init,
                "pnl_pct": (eq - init) / init * 100 if init else 0.0,   # % sobre lo aportado
                "leverage": float(self.cfg.get("leverage", 1.0)),
                "peak_equity": m.peak_eq, "paused_until": m.paused_until,
                "margin_used": sum(p["margin"] for p in m.positions),
                "label": m.label(),
                "shadows": shadows,
                "open_positions": positions,
                "analyses": analyses,
                "last_step": self.last_step,
                "last_error": self.last_error,
                "config": self._safe_config(),
                "follow": {
                    "enabled": bool(self.cfg.get("follow_winner", True)),
                    "mode": (self.chosen_meta or {}).get("mode"),
                    "chosen": (self.chosen_meta or {}).get("chosen"),
                },
            }

    def _safe_config(self) -> dict:
        c = dict(self.cfg)
        key = c.get("ai_api_key") or ""
        c["ai_has_key"] = bool(key)
        c["ai_api_key"] = ""
        return c


engine = Engine()
