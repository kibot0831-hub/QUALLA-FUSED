"""bingx.py — Cliente REST público de BingX (perpetuos USDT-M). Solo datos de mercado:
no necesita API key porque el bot SOLO manda señales a Telegram (no opera)."""
from __future__ import annotations

import logging
import threading
import time

import requests

from strategy import Bar

log = logging.getLogger("qfused.bingx")

BASE_URL = "https://open-api.bingx.com"


class BingXError(Exception):
    pass


class RateLimiter:
    """Limitador global (todas las hebras): como mucho `rps` peticiones por segundo."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        delay = t - now
        if delay > 0:
            time.sleep(delay)


def _parse_kline(k):
    """Acepta dict ({time, open, high, low, close, volume}) o lista [t, o, h, l, c, v]."""
    if isinstance(k, dict):
        ts = k.get("time", k.get("openTime", k.get("t")))
        return Bar(int(ts), float(k["open"]), float(k["high"]), float(k["low"]),
                   float(k["close"]), float(k.get("volume", 0) or 0))
    return Bar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))


class BingXClient:
    def __init__(self, rps: float = 8.0, timeout: float = 15.0, base_url: str = BASE_URL):
        self.base_url = base_url
        self.timeout = timeout
        self.limiter = RateLimiter(rps)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "qfused-bot/1.0"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)

    # ---------------------------------------------------------------- HTTP
    def _get(self, path: str, params: dict | None = None, retries: int = 4):
        last = "sin respuesta"
        for attempt in range(retries):
            self.limiter.wait()
            try:
                r = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(min(2 ** (attempt + 1), 15))
                continue
            try:
                data = r.json()
            except ValueError:
                last = f"respuesta no JSON (HTTP {r.status_code})"
                time.sleep(min(2 ** attempt, 8))
                continue
            code = data.get("code", 0)
            if str(code) in ("0", "None", ""):
                return data.get("data")
            msg = str(data.get("msg", ""))
            if str(code) == "100410" or "rate limit" in msg.lower():
                last = f"rate limit ({code})"
                time.sleep(min(2 ** (attempt + 1), 15))
                continue
            raise BingXError(f"{path} code={code} msg={msg}")
        raise BingXError(f"{path}: reintentos agotados ({last})")

    # ------------------------------------------------------------ endpoints
    def contracts(self) -> list:
        data = self._get("/openApi/swap/v2/quote/contracts")
        return data or []

    def tickers(self) -> dict:
        """symbol → {'last': float, 'qv': volumen 24h en USDT (o None)}"""
        data = self._get("/openApi/swap/v2/quote/ticker") or []
        if isinstance(data, dict):
            data = [data]
        out = {}
        for t in data:
            sym = t.get("symbol")
            if not sym:
                continue
            try:
                qv = float(t["quoteVolume"]) if t.get("quoteVolume") not in (None, "") else None
            except (TypeError, ValueError):
                qv = None
            try:
                last = float(t.get("lastPrice"))
            except (TypeError, ValueError):
                last = None
            out[sym] = {"last": last, "qv": qv}
        return out

    def klines(self, symbol: str, interval: str, limit: int = 300) -> list:
        """Velas ordenadas de la más antigua a la más nueva (incluye la vela en formación)."""
        params = {"symbol": symbol, "interval": interval, "limit": int(limit)}
        try:
            data = self._get("/openApi/swap/v3/quote/klines", params)
        except BingXError as e:
            log.debug("v3 klines falló para %s (%s); pruebo v2", symbol, e)
            data = self._get("/openApi/swap/v2/quote/klines", params)
        bars = {}
        for k in data or []:
            try:
                b = _parse_kline(k)
            except (KeyError, TypeError, ValueError):
                continue
            bars[b.ts] = b
        return [bars[t] for t in sorted(bars)]
