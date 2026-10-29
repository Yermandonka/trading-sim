"""Descarga de datos de mercado (velas OHLCV).

Fuente principal: Binance (cripto, 24/7, sin API key). Respaldo: Coinbase.
Devuelve siempre un DataFrame de pandas con columnas: open, high, low, close, volume
indexado por tiempo (UTC).
"""
from __future__ import annotations

import time
import threading

import httpx
import pandas as pd

UA = "Mozilla/5.0 (X11; Linux x86_64) trading-sim/1.0"

# Binance usa, p.ej., "15m", "1h". Mapa a segundos para la caché y a Coinbase.
_INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}
_COINBASE_GRANULARITY = {60: 60, 300: 300, 900: 900, 3600: 3600, 14400: 21600, 86400: 86400}

# Caché simple en memoria: (symbol, interval) -> (timestamp_fetch, DataFrame)
_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_lock = threading.Lock()


def interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(interval, 900)


def _binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    # Probamos dos hosts: el principal y el espejo de datos.
    hosts = ["https://api.binance.com", "https://data-api.binance.vision"]
    last_err = None
    for host in hosts:
        try:
            r = httpx.get(
                f"{host}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                headers={"User-Agent": UA},
                timeout=20,
            )
            if r.status_code != 200:
                last_err = Exception(f"binance {host} {r.status_code}")
                continue
            rows = r.json()
            df = pd.DataFrame(rows, columns=[
                "openTime", "open", "high", "low", "close", "volume",
                "closeTime", "qav", "trades", "tbav", "tqav", "ignore",
            ])
            df["time"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
            df = df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise last_err or Exception("binance sin datos")


def _coinbase(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    # BTCUSDT -> BTC-USD
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    product = f"{base}-USD"
    gran = _COINBASE_GRANULARITY.get(interval_seconds(interval), 3600)
    r = httpx.get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": gran},
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()  # [time, low, high, open, close, volume], descendente
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df["time"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df = df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)
    df = df.sort_index()
    return df.tail(limit)


def get_ohlcv(symbol: str, interval: str = "15m", limit: int = 400,
              max_age: float = 45.0) -> pd.DataFrame:
    """Devuelve velas OHLCV con caché de `max_age` segundos."""
    key = (symbol, interval)
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < max_age:
            return cached[1]
    try:
        df = _binance(symbol, interval, limit)
    except Exception:
        df = _coinbase(symbol, interval, limit)
    with _lock:
        _cache[key] = (now, df)
    return df


def last_price(symbol: str, interval: str = "15m") -> float:
    df = get_ohlcv(symbol, interval, limit=2, max_age=10.0)
    return float(df["close"].iloc[-1])


# Stablecoins y tokens apalancados que NO queremos operar como activo.
_SKIP_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "GBP",
               "AEUR", "USDT", "USD1", "USDE", "USDD", "XUSD", "EURI",
               "PAXG", "WBTC", "WBETH"}
_SKIP_SUFFIX = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

_universe_cache: tuple[float, list[str]] | None = None


def top_symbols(n: int = 30, quote: str = "USDT", max_age: float = 1800.0) -> list[str]:
    """Devuelve los `n` pares *quote más líquidos de Binance (por volumen 24h),
    excluyendo stablecoins y tokens apalancados. Caché de 30 min."""
    global _universe_cache
    now = time.time()
    if _universe_cache and (now - _universe_cache[0]) < max_age:
        return _universe_cache[1][:n]
    for host in ("https://api.binance.com", "https://data-api.binance.vision"):
        try:
            r = httpx.get(f"{host}/api/v3/ticker/24hr", headers={"User-Agent": UA}, timeout=25)
            if r.status_code != 200:
                continue
            rows = r.json()
            cand = []
            for t in rows:
                sym = t.get("symbol", "")
                if not sym.endswith(quote):
                    continue
                if sym.endswith(_SKIP_SUFFIX):
                    continue
                base = sym[: -len(quote)]
                if base in _SKIP_BASES:
                    continue
                try:
                    vol = float(t.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    vol = 0.0
                cand.append((vol, sym))
            cand.sort(reverse=True)
            ordered = [s for _, s in cand]
            _universe_cache = (now, ordered)
            return ordered[:n]
        except Exception:  # noqa: BLE001
            continue
    # si falla, devolvemos lo último conocido (caché real aún válida como respaldo).
    # Si ni siquiera hay caché, SEÑALAMOS el fallo lanzando: así el motor no congela
    # un universo de emergencia durante 30 min, sino que reintenta en el siguiente ciclo.
    if _universe_cache:
        return _universe_cache[1][:n]
    raise RuntimeError("no se pudo obtener el universo de Binance (sin caché previa)")


# ---------- financiación de perpetuos (para cash-and-carry) ----------
_funding_cache: tuple[float, dict] = (0.0, {})


def funding_rates(symbols: list[str], max_age: float = 300.0) -> dict[str, dict]:
    """{símbolo: {"rate": tasa por periodo de 8 h, "apr": anualizada, "next_ts": epoch}}.
    Fuente: Binance Futures (premiumIndex, todos los pares en una llamada); respaldo
    Kraken Futures (tasa horaria relativa × 8). Caché de `max_age` segundos."""
    global _funding_cache
    now = time.time()
    if now - _funding_cache[0] < max_age and all(s in _funding_cache[1] for s in symbols):
        return {s: _funding_cache[1][s] for s in symbols if s in _funding_cache[1]}
    out: dict[str, dict] = {}
    try:
        r = httpx.get("https://fapi.binance.com/fapi/v1/premiumIndex", headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 200:
            for row in r.json():
                s = row.get("symbol")
                if s in symbols:
                    rate = float(row.get("lastFundingRate", 0.0))
                    out[s] = {"rate": rate, "apr": rate * 3 * 365,
                              "next_ts": float(row.get("nextFundingTime", 0)) / 1000.0, "src": "binance"}
    except Exception:  # noqa: BLE001
        pass
    missing = [s for s in symbols if s not in out]
    for s in missing:
        base = s[:-4] if s.endswith("USDT") else s
        ksym = "PF_XBTUSD" if base == "BTC" else f"PF_{base}USD"
        try:
            r = httpx.get("https://futures.kraken.com/derivatives/api/v4/historicalfundingrates",
                          params={"symbol": ksym}, headers={"User-Agent": UA}, timeout=15)
            rates = r.json().get("rates", []) if r.status_code == 200 else []
            if rates:
                hourly = float(rates[-1].get("relativeFundingRate", 0.0))
                rate = hourly * 8
                nxt = (int(now // 28800) + 1) * 28800
                out[s] = {"rate": rate, "apr": rate * 3 * 365, "next_ts": float(nxt), "src": "kraken"}
        except Exception:  # noqa: BLE001
            continue
    if out:
        _funding_cache = (now, {**_funding_cache[1], **out})
    return out
