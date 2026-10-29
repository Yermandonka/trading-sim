"""Capa de IA (opcional): actúa como FILTRO / VETO de las operaciones.

Los algoritmos cuantitativos deciden la operación; antes de abrirla, se le pide
a un LLM (Google AI Studio, gratis) que la apruebe o la vete desde un punto de
vista de riesgo/contexto. La IA NUNCA puede bloquear una salida de protección
(stop-loss / take-profit); solo influye en las ENTRADAS.

Si no hay clave o la IA falla, se deja pasar la operación (fail-open) y se anota.
Clave: config['ai_api_key'] o variable de entorno GEMINI_API_KEY.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx

UA = "Mozilla/5.0 trading-sim/1.0"
# modelos flash rápidos (el más ligero primero); si uno falla/satura, el siguiente
MODELS = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-flash-latest"]

# caché: (symbol, side) -> (ts, verdict)
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_TTL = 600.0  # 10 min: no re-preguntamos lo mismo una y otra vez

# timeout por modelo: si uno tarda, pasamos al siguiente en vez de quedarnos colgados
_TIMEOUT = httpx.Timeout(connect=8.0, read=18.0, write=8.0, pool=8.0)

# códigos de Google que son transitorios (sobrecarga / límite): merece reintentar.
# 503 = modelo saturado, 429 = rate limit, 5xx = fallo temporal del servidor.
_RETRY_STATUS = {429, 500, 502, 503, 504}
# esperas (seg) ENTRE rondas completas sobre la lista de modelos. Presupuesto total
# de espera ~2s: suficiente para que pase un pico de 503 sin colgar la apertura.
_BACKOFFS = (0.6, 1.4)

# estadísticas en vivo para el panel
STATS = {"calls": 0, "approves": 0, "vetoes": 0, "errors": 0, "retries": 0,
         "last_error": "", "last_ok_ts": 0.0, "last_model": ""}


class _AIError(Exception):
    """Error de la llamada a la IA, con marca de si conviene reintentarlo."""

    def __init__(self, msg: str, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def api_key(cfg: dict) -> str:
    return (cfg.get("ai_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("ai_enabled")) and bool(api_key(cfg))


def _build_prompt(symbol: str, side: str, a: dict) -> str:
    name = symbol.replace("USDT", "")
    partes = "\n".join(
        f"- {p['name']}: señal {p['score']:+.2f} → {p['text']}" for p in a.get("parts", [])
    )
    accion = "COMPRA en largo" if side == "long" else "VENTA en corto"
    return f"""Eres un gestor de riesgo prudente de un fondo cuantitativo. El sistema
algorítmico quiere abrir una {accion} sobre {name} (par {symbol}, cripto).

Datos del análisis técnico actual:
- Puntuación combinada del ensemble: {a.get('score'):+.2f} (rango -1 a +1)
- Confianza: {a.get('confidence')}
- Precio: {a.get('price')}
- Volatilidad ATR: {a.get('atr_pct')}%
Desglose por algoritmo:
{partes}

Tu trabajo es FILTRAR: aprueba la operación solo si es razonable según coherencia
de las señales, volatilidad y riesgo. Veta si las señales se contradicen demasiado,
la volatilidad es extrema, o la confianza es baja para el tamaño del movimiento.
NO tienes noticias en tiempo real: básate en los datos dados y el sentido común.

Responde SOLO con un JSON válido, sin texto extra, con este formato exacto:
{{"approve": true/false, "confidence": 0.0-1.0, "reason": "motivo en una frase, en español"}}"""


def _call_model(prompt: str, key: str, model: str) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={key}")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if model.startswith("gemini"):
        # salida pequeña y rápida; el veredicto son 3 campos
        body["generationConfig"] = {"responseMimeType": "application/json",
                                    "temperature": 0.2, "maxOutputTokens": 200}
    try:
        r = httpx.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=_TIMEOUT)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        # fallo de red/timeout: transitorio, merece reintento
        raise _AIError(f"{type(e).__name__}", retryable=True) from e
    if r.status_code != 200:
        raise _AIError(f"HTTP {r.status_code}", status=r.status_code,
                       retryable=r.status_code in _RETRY_STATUS)
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _parse(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def vet(symbol: str, side: str, analysis: dict, cfg: dict) -> dict:
    """Devuelve {'approve':bool,'reason':str,'confidence':float,'source':str}."""
    key = api_key(cfg)
    ck = (symbol, side)
    now = time.time()
    hit = _cache.get(ck)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]

    prompt = _build_prompt(symbol, side, analysis)
    STATS["calls"] += 1
    last_err = ""
    # Estrategia: rondas sobre la lista de modelos. Un modelo que da error
    # transitorio (503/429/timeout) vuelve a intentarse en la siguiente ronda tras
    # un backoff; un error permanente (404 modelo inexistente, 400/403 clave) se
    # descarta y no se reintenta. Así un pico de sobrecarga no cae al fallback.
    candidates = list(MODELS)
    for attempt in range(len(_BACKOFFS) + 1):
        retry_next = []
        for model in candidates:
            try:
                raw = _call_model(prompt, key, model)
                v = _parse(raw)
                verdict = {
                    "approve": bool(v.get("approve", True)),
                    "reason": str(v.get("reason", ""))[:200],
                    "confidence": round(float(v.get("confidence", 0.5)), 2),
                    "source": model,
                    "ts": now,
                }
                STATS["approves" if verdict["approve"] else "vetoes"] += 1
                STATS["last_ok_ts"] = now
                STATS["last_model"] = model
                _cache[ck] = (now, verdict)
                return verdict
            except _AIError as e:
                last_err = f"{model} {e}"
                if e.retryable:
                    retry_next.append(model)  # sobrecarga: otra oportunidad
            except Exception as e:  # noqa: BLE001  (parseo u otros: no reintentar)
                last_err = f"{model}: {type(e).__name__}: {e}"
        candidates = retry_next
        if not candidates:
            break  # nada reintentable pendiente
        if attempt < len(_BACKOFFS):
            STATS["retries"] += 1
            time.sleep(_BACKOFFS[attempt])
    # fail-open: si la IA no responde, dejamos pasar pero lo marcamos.
    # NO se cachea (para reintentar en la próxima oportunidad, no quedarse "fallando").
    STATS["errors"] += 1
    STATS["last_error"] = last_err[:120]
    return {"approve": True, "reason": f"IA no disponible ({last_err[:60]}): se deja pasar",
            "confidence": 0.0, "source": "fallback", "ts": now}


def get_stats(cfg: dict) -> dict:
    ok = STATS["calls"] - STATS["errors"]
    return {
        "enabled": enabled(cfg),
        "has_key": bool(api_key(cfg)),
        "calls": STATS["calls"],
        "ok": ok,
        "approves": STATS["approves"],
        "vetoes": STATS["vetoes"],
        "errors": STATS["errors"],
        "retries": STATS["retries"],
        "last_error": STATS["last_error"],
        "last_model": STATS["last_model"],
        "healthy": ok > 0 and STATS["errors"] <= ok,
    }


def clear_cache():
    _cache.clear()
