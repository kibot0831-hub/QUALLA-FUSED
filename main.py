"""Qullamaggie Fused Bot — escanea TODOS los perpetuos USDT de BingX y envía señales detalladas a Telegram.

Tareas concurrentes:
  • cycle (cada cierre de vela): pre-filtro de candidatos -> replay exacto de la estrategia -> eventos a Telegram.
  • live_loop (cada LIVE_POLL_SEC): vigila stop/TP1 de las posiciones virtuales abiertas con el precio en vivo.
  • comandos de Telegram: /status /posiciones /watchlist /analiza /config /pausa /reanudar.
"""
import asyncio
import json
import logging
import os
import signal
import time

import numpy as np

import messages as M
import strategy as S
from bingx import BingX
from config import cfg
from telegram_bot import Telegram

logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

DAY_MS = S.DAY_MS
FINAL_TYPES = ("STOP", "BE_STOP", "TRAIL", "TIME")


def tf_seconds(tf):
    unit, val = tf[-1], int(tf[:-1])
    return val * {"m": 60, "h": 3600, "d": 86400}[unit]


class Bot:
    def __init__(self):
        self.bx = BingX(cfg)
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, cfg.telegram_thread_id)
        self.tf_s = tf_seconds(cfg.timeframe)
        self.tf_ms = self.tf_s * 1000
        self.n_bars = min(1440, int(cfg.replay_days * 1440 / (self.tf_s / 60)) + 220)
        self.universe_at = 0.0
        self.daily = {}          # symbol -> series diarias
        self.daily_day = None    # día UTC de la última carga diaria
        self.prior_pot = set()   # símbolos con potencial en días previos de la ventana
        self.seen = {}           # clave de evento -> timestamp (dedupe)
        self.open_trades = {}
        self.diags = {}          # symbol -> diagnóstico de la última vela (candidatos)
        self.paused = False
        self.counters = {"day": None, "entries": 0, "exits": 0}
        self.last_stats = {"universe": 0, "daily_ok": 0, "cands": 0, "secs": 0.0, "entries": 0, "exits": 0}
        self.last_heartbeat = time.time()
        self.last_error_alert = 0.0
        self.stop = asyncio.Event()

    # ───────────── estado (dedupe) ─────────────
    def load_state(self):
        try:
            with open(cfg.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.seen = {k: int(v) for k, v in d.get("seen", {}).items()}
            log.info("Estado cargado: %d eventos vistos", len(self.seen))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("No se pudo leer el estado: %s", e)

    def save_state(self):
        try:
            cutoff = int(time.time()) - 5 * 86400
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
            folder = os.path.dirname(cfg.state_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = cfg.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seen": self.seen}, f)
            os.replace(tmp, cfg.state_path)
        except Exception as e:
            log.debug("No se pudo guardar el estado: %s", e)

    # ───────────── datos ─────────────
    async def refresh_daily(self, today, now_ms):
        log.info("Descargando velas diarias de %d pares...", len(self.bx.symbols))
        t0 = time.time()

        async def one(sym):
            try:
                raw = await self.bx.fetch_ohlcv(sym, "1d", 150)
            except Exception as e:
                log.debug("Diario %s falló: %s", sym, e)
                return sym, None
            if raw is None:
                return sym, None
            raw = raw[raw[:, 0] + DAY_MS <= now_ms]  # solo velas diarias cerradas
            return (sym, raw) if len(raw) >= 35 else (sym, None)

        res = await asyncio.gather(*(one(s) for s in self.bx.symbols))
        daily, pot, misaligned = {}, set(), 0
        for sym, raw in res:
            if raw is None:
                continue
            if raw[-1, 0] % DAY_MS != 0:
                misaligned += 1
            ds = S.daily_series(raw[:, 0], raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5], cfg.ps_lookback)
            daily[sym] = ds
            if S.prior_days_potential(ds, cfg, today):
                pot.add(sym)
        if daily and misaligned > len(daily) * 0.5:
            log.warning("Las velas diarias de BingX no parecen alineadas a 00:00 UTC (%d/%d). "
                        "Revisa el cálculo de contexto diario.", misaligned, len(daily))
        self.daily, self.prior_pot, self.daily_day = daily, pot, today
        log.info("Diario listo: %d pares con histórico (%d con potencial previo) en %.0fs",
                 len(daily), len(pot), time.time() - t0)

    def select_candidates(self, tickers, today):
        cands = []
        for sym in self.bx.symbols:
            ds = self.daily.get(sym)
            t = tickers.get(sym)
            if ds is None or not t:
                continue
            if cfg.min_quote_volume > 0 and (t.get("quoteVolume") or 0) < cfg.min_quote_volume:
                continue
            hi = t.get("high")
            if sym in self.prior_pot or (hi and S.could_signal(S.ctx_for_day(ds, today), float(hi), cfg)):
                cands.append(sym)
        return cands

    async def replay_symbol(self, sym, now_ms, ds=None):
        raw = await self.bx.fetch_ohlcv(sym, cfg.timeframe, self.n_bars)
        if raw is None:
            raise ValueError("sin velas")
        raw = raw[raw[:, 0] + self.tf_ms <= now_ms]  # descarta la vela en formación
        if len(raw) < 100:
            raise ValueError("pocas velas")
        bars = (raw[:, 0].astype(np.int64), raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5])
        return S.replay(sym, bars, ds if ds is not None else self.daily[sym], cfg, self.tf_ms)

    # ───────────── entrega ─────────────
    async def deliver(self, ev, now_ms, series=None):
        """Envía un evento (con gráfico en las entradas). True si el texto se entregó."""
        text = M.format_event(ev, cfg, now_ms)
        if ev["type"] == "ENTRY" and cfg.send_charts and series is not None:
            try:
                from charts import render_entry_chart
                png = await asyncio.to_thread(render_entry_chart, series, ev, cfg)
                await self.tg.send_photo(png, caption=text.split("\n")[0])
            except Exception as e:
                log.warning("No se pudo generar/enviar el gráfico: %s", e)
        return await self.tg.send(text)

    async def emit(self, ev, now_ms, series=None):
        """Dedupe + envío. La clave se marca ANTES de enviar (evita carreras) y se revierte si falla."""
        key = f"{ev['symbol']}|{ev['signal_ts']}|{ev['type']}"
        if key in self.seen:
            return False
        self.seen[key] = int(time.time())
        if now_ms - ev["event_ts"] > cfg.max_event_age_bars * self.tf_ms:
            return False  # antiguo: marcado como visto sin enviarlo
        if self.paused or (ev["type"] != "ENTRY" and not cfg.send_exit_updates):
            return False
        ok = await self.deliver(ev, now_ms, series)
        if not ok:
            self.seen.pop(key, None)  # se reintentará en el siguiente ciclo
            return False
        self.counters["entries" if ev["type"] == "ENTRY" else "exits"] += 1
        log.info("SEÑAL %s %s %s%s", ev["type"], ev["setup"], ev["symbol"], " (vivo)" if ev.get("live") else "")
        return True

    # ───────────── ciclo principal ─────────────
    async def cycle(self):
        t0 = time.time()
        now_ms = int(t0 * 1000)
        today = now_ms // DAY_MS
        if self.counters["day"] != today:
            self.counters.update(day=today, entries=0, exits=0)
        if not self.bx.symbols or t0 - self.universe_at > 6 * 3600:
            await self.bx.load_universe()
            self.universe_at = t0
        if self.daily_day != today:
            await self.refresh_daily(today, now_ms)

        tickers = await self.bx.fetch_tickers()
        cands = self.select_candidates(tickers, today)

        async def work(sym):
            try:
                return sym, await self.replay_symbol(sym, now_ms), None
            except Exception as e:
                return sym, None, f"{type(e).__name__}: {e}"

        results = await asyncio.gather(*(work(s) for s in cands))

        errors = [(s, e) for s, r, e in results if e]
        opens, diags, pending = {}, {}, []
        for sym, res, err in results:
            if res is None:
                continue
            if res["open_trade"]:
                opens[sym] = res["open_trade"]
            if "diag" in res:
                diags[sym] = res["diag"]
            for ev in res["events"]:
                pending.append((ev, res["series"]))
        self.open_trades, self.diags = opens, diags

        pending.sort(key=lambda x: x[0]["event_ts"])
        sent = 0
        for ev, series in pending:
            if await self.emit(ev, now_ms, series):
                sent += 1
        self.save_state()

        secs = time.time() - t0
        self.last_stats = {"universe": len(self.bx.symbols), "daily_ok": len(self.daily), "cands": len(cands),
                           "secs": secs, "entries": self.counters["entries"], "exits": self.counters["exits"]}
        log.info("Ciclo OK · universo=%d candidatos=%d abiertas=%d enviados=%d errores=%d · %.1fs",
                 len(self.bx.symbols), len(cands), len(opens), sent, len(errors), secs)
        if len(cands) >= 8 and len(errors) > len(cands) * 0.3:
            await self.alert(f"{len(errors)}/{len(cands)} pares fallaron. Ej.: {errors[0][0]} -> {errors[0][1]}")

        if cfg.heartbeat_hours > 0 and time.time() - self.last_heartbeat >= cfg.heartbeat_hours * 3600:
            self.last_heartbeat = time.time()
            await self.tg.send(M.heartbeat(self.last_stats, self.open_trades, cfg, self.paused))

    # ───────────── vigilancia casi en vivo ─────────────
    async def live_check(self, tickers):
        now_ms = int(time.time() * 1000)
        bar_ts = now_ms - now_ms % self.tf_ms
        pfrac = cfg.partial_pct / 100.0
        for sym, o in list(self.open_trades.items()):
            if o.get("pending"):
                continue  # la entrada aún no se ha ejecutado
            k = f"{sym}|{o['signal_ts']}|"
            if any((k + t) in self.seen for t in FINAL_TYPES):
                continue
            tk = tickers.get(sym) or {}
            if not tk.get("last"):
                continue
            last, s = float(tk["last"]), o["side"]
            tp_done = o["partial_taken"] or (k + "TP1") in self.seen
            stop_lvl = o["fill"] if tp_done else o["stop_init"]
            if (last <= stop_lvl) if s == 1 else (last >= stop_lvl):
                kind, px, frac = ("BE_STOP" if tp_done else "STOP"), stop_lvl, (1.0 - pfrac if tp_done else 1.0)
            elif not tp_done and ((last >= o["tp1"]) if s == 1 else (last <= o["tp1"])):
                kind, px, frac = "TP1", o["tp1"], pfrac
            else:
                continue
            if (k + kind) in self.seen:
                continue
            base_r = o["realized_r"] if o["partial_taken"] else (
                pfrac * s * (o["tp1"] - o["fill"]) / o["risk"] if tp_done else 0.0)
            r_part = s * (px - o["fill"]) / o["risk"]
            ev = {"type": kind, "symbol": sym, "setup": o["setup"], "side": s, "signal_ts": o["signal_ts"],
                  "bar_ts": bar_ts, "event_ts": now_ms, "price": px, "frac": frac, "r_part": r_part,
                  "r_total": base_r + frac * r_part, "fill": o["fill"], "stop_init": o["stop_init"],
                  "tp1": o["tp1"], "risk": o["risk"], "live": True}
            if kind == "TP1":
                ev["remaining"] = 1.0 - pfrac
            await self.emit(ev, now_ms)

    async def live_loop(self):
        if cfg.live_poll_sec <= 0 or not cfg.send_exit_updates:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=cfg.live_poll_sec)
                return
            except asyncio.TimeoutError:
                pass
            if not self.open_trades or self.paused:
                continue
            try:
                await self.live_check(await self.bx.fetch_tickers())
            except Exception as e:
                log.debug("live_check: %s", e)

    # ───────────── comandos de Telegram ─────────────
    def _resolve(self, arg):
        a = arg.upper().replace("-", "/").strip()
        for suf in ("/USDT:USDT", "/USDT", "USDT"):
            if a.endswith(suf) and len(a) > len(suf):
                a = a[: -len(suf)]
                break
        for sym in self.bx.symbols:
            if sym.split("/")[0].upper() == a:
                return sym
        return None

    async def analyze(self, arg):
        sym = self._resolve(arg)
        if not sym:
            return f"❓ No encuentro «{M.esc(arg)}» entre los perpetuos USDT analizados."
        ds = self.daily.get(sym)
        now_ms = int(time.time() * 1000)
        if ds is None:
            raw = await self.bx.fetch_ohlcv(sym, "1d", 150)
            raw = raw[raw[:, 0] + DAY_MS <= now_ms] if raw is not None else None
            if raw is None or len(raw) < 35:
                return "ℹ️ Ese par no tiene histórico diario suficiente (mín. 35 días)."
            ds = S.daily_series(raw[:, 0], raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5], cfg.ps_lookback)
        res = await self.replay_symbol(sym, now_ms, ds)
        if "diag" not in res:
            return "ℹ️ No hay velas suficientes para analizar ese par."
        return M.analysis(sym, res["diag"], cfg)

    async def on_command(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        if cmd in ("/start", "/help", "/ayuda"):
            return M.help_text()
        if cmd == "/status":
            return M.heartbeat(self.last_stats, self.open_trades, cfg, self.paused)
        if cmd in ("/posiciones", "/positions"):
            return M.open_positions(self.open_trades)
        if cmd == "/watchlist":
            return M.watchlist(self.diags, cfg)
        if cmd == "/config":
            return M.startup(cfg, len(self.bx.symbols))
        if cmd == "/pausa":
            self.paused = True
            return "⏸ Señales en pausa. El bot sigue analizando; usa /reanudar para volver a recibirlas."
        if cmd == "/reanudar":
            self.paused = False
            return "▶️ Señales reactivadas."
        if cmd in ("/analiza", "/analyze"):
            if len(parts) < 2:
                return "Uso: /analiza BTC"
            return await self.analyze(parts[1])
        return "Comando no reconocido. /ayuda para ver la lista."

    # ───────────── ejecución ─────────────
    async def alert(self, text):
        if time.time() - self.last_error_alert < 1800:
            return
        self.last_error_alert = time.time()
        await self.tg.send(M.error_alert(text))

    async def run(self):
        self.load_state()
        await self.tg.start()
        while not self.stop.is_set():  # el universo debe cargar antes de arrancar
            try:
                await self.bx.load_universe()
                self.universe_at = time.time()
                break
            except Exception as e:
                log.error("No se pudo cargar el universo de BingX: %s", e)
                await self.alert(f"No se puede conectar con BingX: {e}. Si persiste, cambia la región del servicio en Railway.")
                await asyncio.sleep(60)
        if not self.stop.is_set():
            await self.tg.send(M.startup(cfg, len(self.bx.symbols)) + ("\n• Comandos: /ayuda" if cfg.commands_enabled else ""))

        tasks = [asyncio.create_task(self.live_loop())]
        if cfg.commands_enabled:
            tasks.append(asyncio.create_task(self.tg.poll_commands(self.on_command, self.stop)))

        while not self.stop.is_set():
            try:
                await self.cycle()
            except Exception as e:
                log.exception("Fallo en el ciclo")
                await self.alert(f"{type(e).__name__}: {e}")
            now = time.time()
            nxt = (int(now) // self.tf_s + 1) * self.tf_s + cfg.bar_close_delay
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(1.0, nxt - now))
            except asyncio.TimeoutError:
                pass

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.tg.close()
        await self.bx.close()
        log.info("Bot detenido.")


async def main():
    bot = Bot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bot.stop.set)
        except NotImplementedError:
            pass
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
