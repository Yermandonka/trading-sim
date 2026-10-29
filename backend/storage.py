"""Persistencia: estado del portfolio (JSON) + histórico en SQLite."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
DB_PATH = os.path.join(DATA_DIR, "history.db")

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, symbol TEXT, side TEXT, action TEXT,
        qty REAL, price REAL, pnl REAL, reason TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS equity(
        ts REAL PRIMARY KEY, value REAL, cash REAL)""")
    # curva "elegida": sigue en cada momento a la mejor entre tu estrategia y
    # mantener BTC. mode = 'strategy' | 'btc'. bh = valor de mantener BTC.
    c.execute("""CREATE TABLE IF NOT EXISTS chosen(
        ts REAL PRIMARY KEY, value REAL, mode TEXT, bh REAL)""")
    # carteras sombra: otras configuraciones operando en vivo en paralelo
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_equity(
        ts REAL, name TEXT, value REAL, PRIMARY KEY(ts, name))""")
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, symbol TEXT, side TEXT,
        action TEXT, qty REAL, price REAL, pnl REAL, reason TEXT)""")
    return c


def load_state() -> dict | None:
    with _lock:
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                return None
    return None


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)


def log_trade(ts, symbol, side, action, qty, price, pnl, reason, portfolio=None) -> None:
    with _lock:
        c = _conn()
        if portfolio:
            c.execute("INSERT INTO shadow_trades(ts,name,symbol,side,action,qty,price,pnl,reason) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (ts, portfolio, symbol, side, action, qty, price, pnl, reason))
        else:
            c.execute("INSERT INTO trades(ts,symbol,side,action,qty,price,pnl,reason) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (ts, symbol, side, action, qty, price, pnl, reason))
        c.commit()
        c.close()


def log_shadow_equity(ts, name, value) -> None:
    with _lock:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO shadow_equity(ts,name,value) VALUES(?,?,?)", (ts, name, value))
        c.commit()
        c.close()


def shadow_curves(limit: int = 1000) -> dict[str, list[dict]]:
    """{nombre: [{ts, value}, ...]} de cada cartera sombra (últimos `limit` puntos)."""
    with _lock:
        c = _conn()
        names = [r[0] for r in c.execute("SELECT DISTINCT name FROM shadow_equity")]
        out = {}
        for n in names:
            rows = c.execute("SELECT ts,value FROM shadow_equity WHERE name=? ORDER BY ts DESC LIMIT ?",
                             (n, limit)).fetchall()[::-1]
            out[n] = [{"ts": r[0], "value": r[1]} for r in rows]
        c.close()
    return out


def shadow_trades(name: str, limit: int = 100) -> list[dict]:
    with _lock:
        c = _conn()
        rows = c.execute("SELECT ts,symbol,side,action,qty,price,pnl,reason FROM shadow_trades "
                         "WHERE name=? ORDER BY ts DESC LIMIT ?", (name, limit)).fetchall()
        c.close()
    keys = ["ts", "symbol", "side", "action", "qty", "price", "pnl", "reason"]
    return [dict(zip(keys, r)) for r in rows]


def log_veto(ts, symbol, side, price, score, reason) -> None:
    """Entradas que los algoritmos querían abrir y la IA vetó (para poder medir
    después qué habría pasado sin el filtro)."""
    with _lock:
        conn = _conn()
        conn.execute("CREATE TABLE IF NOT EXISTS vetoes (ts REAL, symbol TEXT, side TEXT, "
                     "price REAL, score REAL, reason TEXT)")
        conn.execute("INSERT INTO vetoes VALUES (?,?,?,?,?,?)", (ts, symbol, side, price, score, reason))
        conn.commit(); conn.close()


def log_equity(ts, value, cash) -> None:
    with _lock:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO equity(ts,value,cash) VALUES(?,?,?)",
                  (ts, value, cash))
        c.commit()
        c.close()


def log_chosen(ts, value, mode, bh) -> None:
    with _lock:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO chosen(ts,value,mode,bh) VALUES(?,?,?,?)",
                  (ts, value, mode, bh))
        c.commit()
        c.close()


def chosen_curve(limit: int = 5000) -> list[dict]:
    with _lock:
        c = _conn()
        rows = c.execute("SELECT ts,value,mode,bh FROM chosen ORDER BY ts DESC LIMIT ?",
                         (limit,)).fetchall()
        c.close()
    rows = rows[::-1]
    return [{"ts": r[0], "value": r[1], "mode": r[2], "bh": r[3]} for r in rows]


def recent_trades(limit: int = 100) -> list[dict]:
    with _lock:
        c = _conn()
        rows = c.execute("SELECT ts,symbol,side,action,qty,price,pnl,reason "
                         "FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
    cols = ["ts", "symbol", "side", "action", "qty", "price", "pnl", "reason"]
    return [dict(zip(cols, r)) for r in rows]


def equity_curve(limit: int = 1000) -> list[dict]:
    with _lock:
        c = _conn()
        rows = c.execute("SELECT ts,value,cash FROM equity ORDER BY ts DESC LIMIT ?",
                         (limit,)).fetchall()
        c.close()
    rows = rows[::-1]
    return [{"ts": r[0], "value": r[1], "cash": r[2]} for r in rows]


def reset_history() -> None:
    with _lock:
        c = _conn()
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM equity")
        c.execute("DELETE FROM chosen")
        c.execute("DELETE FROM shadow_equity")
        c.execute("DELETE FROM shadow_trades")
        c.commit()
        c.close()
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)


# ---------- archivo de simulaciones anteriores (para compararlas en la gráfica) ----------
RUNS_DIR = os.path.join(DATA_DIR, "runs")


def archive_run(label: str, db_path: str | None = None, min_points: int = 30) -> str | None:
    """Guarda la curva de capital actual (o la de otra base de datos) como una
    simulación archivada en data/runs/<n>.json. Devuelve el nombre o None si
    no había datos suficientes."""
    import json as _json
    import time as _time
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return None
    with _lock:
        c = sqlite3.connect(path)
        rows = c.execute("SELECT ts,value FROM equity ORDER BY ts").fetchall()
        trades = c.execute("SELECT COUNT(*) FROM trades WHERE action<>'ABRIR'").fetchone()[0]
        c.close()
    if len(rows) < min_points:
        return None
    os.makedirs(RUNS_DIR, exist_ok=True)
    name = _time.strftime("%Y%m%d-%H%M%S", _time.gmtime(rows[0][0]))
    data = {
        "name": name, "label": label, "created": _time.time(),
        "start_ts": rows[0][0], "end_ts": rows[-1][0],
        "initial": rows[0][1], "final": rows[-1][1], "trades": trades,
        "curve": [[round(t, 1), round(v, 2)] for t, v in rows],
    }
    with open(os.path.join(RUNS_DIR, name + ".json"), "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False)
    return name


def list_runs() -> list[dict]:
    import json as _json
    if not os.path.isdir(RUNS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(RUNS_DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(RUNS_DIR, fn), encoding="utf-8") as f:
                    out.append(_json.load(f))
            except Exception:  # noqa: BLE001
                continue
    return out


def delete_run(name: str) -> bool:
    fp = os.path.join(RUNS_DIR, os.path.basename(name) + ".json")
    if os.path.exists(fp):
        os.remove(fp)
        return True
    return False
