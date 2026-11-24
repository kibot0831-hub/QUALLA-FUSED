"""Acceso a BingX (futuros perpetuos USDT) con ccxt asíncrono. Solo datos públicos: no hacen falta API keys."""
import asyncio
import logging

import ccxt.async_support as ccxt
import numpy as np

log = logging.getLogger("bingx")

def timeframe_ms(tf):
    return int(tf[:-1]) * {"m": 60, "h": 3600, "d": 86400}[tf[-1]] * 1000


RETRY_ERRORS = (ccxt.NetworkError, ccxt.DDoSProtection, ccxt.RateLimitExceeded,
                ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)


class BingX:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ex = ccxt.bingx({
            "enableRateLimit": True,
            "rateLimit": cfg.rate_limit_ms,
            "timeout": 25000,
            "options": {"defaultType": "swap"},
        })
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self.symbols = []

    async def _retry(self, fn, *args, tries=4):
        delay = 2.0
        for k in range(tries):
            try:
                return await fn(*args)
            except RETRY_ERRORS as e:
                if k == tries - 1:
                    raise
                log.debug("Reintento %s/%s tras %s", k + 1, tries, type(e).__name__)
                await asyncio.sleep(delay * (3 if isinstance(e, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)) else 1))
                delay *= 2

    async def load_universe(self):
        """Todos los perpetuos lineales USDT activos de BingX (menos blacklist / solo whitelist si se define)."""
        await self._retry(self.ex.load_markets, True)
        syms = []
        for sym, m in self.ex.markets.items():
            if not m.get("swap") or m.get("quote") != "USDT" or m.get("inverse"):
                continue
            if m.get("active") is False:
                continue
            base = str(m.get("base", "")).upper()
            if self.cfg.whitelist and base not in self.cfg.whitelist:
                continue
            if base in self.cfg.blacklist:
                continue
            syms.append(sym)
        self.symbols = sorted(syms)
        log.info("Universo BingX: %d perpetuos USDT", len(self.symbols))
        return self.symbols

    async def fetch_tickers(self):
        return await self._retry(self.ex.fetch_tickers)

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        """Devuelve ndarray [ts, o, h, l, c, v] ordenado y sin duplicados (incluye la vela en formación)."""
        async with self._sem:
            raw = await self._retry(self.ex.fetch_ohlcv, symbol, timeframe, None, limit)
        if not raw:
            return None
        arr = np.asarray(raw, dtype=float)
        arr = arr[~np.isnan(arr).any(axis=1)]
        if len(arr) == 0:
            return None
        arr = arr[np.argsort(arr[:, 0], kind="stable")]
        _, idx = np.unique(arr[:, 0], return_index=True)
        return arr[np.sort(idx)]

    async def fetch_ohlcv_range(self, symbol, timeframe, since_ms, until_ms, page=1000):
        """Histórico paginado [since_ms, until_ms] (para backtest). Incluye protección contra bucles."""
        tf = timeframe_ms(timeframe)
        rows, cursor = [], int(since_ms)
        while cursor < until_ms:
            async with self._sem:
                raw = await self._retry(self.ex.fetch_ohlcv, symbol, timeframe, cursor, page,
                                        {"until": cursor + page * tf})
            if raw:
                rows.extend(raw)
                last = int(raw[-1][0])
                cursor = max(cursor + tf, last + tf) if last >= cursor else cursor + page * tf
            else:
                cursor += page * tf  # hueco sin velas: saltar
        if not rows:
            return None
        arr = np.asarray(rows, dtype=float)
        arr = arr[~np.isnan(arr).any(axis=1)]
        arr = arr[np.argsort(arr[:, 0], kind="stable")]
        _, idx = np.unique(arr[:, 0], return_index=True)
        arr = arr[np.sort(idx)]
        return arr[(arr[:, 0] >= since_ms) & (arr[:, 0] < until_ms)]

    async def close(self):
        try:
            await self.ex.close()
        except Exception:
            pass
