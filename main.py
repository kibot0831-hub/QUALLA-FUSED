"""main.py — Qullamaggie Fused v2 · bot de señales (BingX perpetuos USDT → Telegram).

Cada vez que cierra una vela del timeframe, escanea TODAS las monedas cripto de BingX,
reproduce la estrategia (EP Long + Parabolic Short) y avisa por Telegram con el detalle.
Solo manda señales: no opera ni necesita API keys de BingX.
"""
from __future__ import annotations

import logging
import os
import signal as sig_mod
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from bingx import BingXClient, BingXError
from config import Config, is_excluded
from state import State
from strategy import (DAY_MS, MIN_MS, advance_position, compute_daily_ctx, evaluate,
                      position_from_signal)
from telegram_bot import (Telegram, format_event, format_radar, format_signal,
                          format_startup, format_summary)

log = logging.getLogger("qfused")

CONTRACTS_REFRESH_SEC = 6 * 3600
ERROR_NOTIFY_SEC = 1800


class Bot:
    def __init__(self, cfg: Config, client=None, tg=None, state_path: str | None = None):
        self.cfg = cfg
        self.tf_ms = cfg.tf_min * MIN_MS
        self.client = client or BingXClient(rps=cfg.rps)
        self.tg = tg or Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.state = State(state_path or os.path.join(cfg.state_dir, "qfused_state.json"))
        self.contracts: dict = {}      # symbol -> {'qty_prec', 'min_qty'}
        self.contracts_ts = 0.0
        self.daily: dict = {}          # symbol -> [Bar diarias]
        self.daily_day: dict = {}      # symbol -> día UTC (ms) de la última descarga diaria
        self.daily_checked = False
        self.first_cycle = True
        self.stop = False
        self._last_err_notify = 0.0

    # ───────────────────────────── contratos ─────────────────────────────
    def load_contracts(self):
        raw = self.client.contracts()
        out = {}
        for c in raw:
            sym = c.get("symbol")
            if not sym or not sym.endswith("-USDT"):
                continue
            if str(c.get("status", "1")) != "1":
                continue
            if is_excluded(sym, self.cfg.exclude):
                continue
            info = {}
            try:
                info["qty_prec"] = int(float(c["quantityPrecision"]))
            except (KeyError, TypeError, ValueError):
                pass
            for k in ("tradeMinQuantity", "tradeMinLimit"):
                try:
                    v = float(c[k])
                    if v > 0:
                        info["min_qty"] = v
                        break
                except (KeyError, TypeError, ValueError):
                    pass
            out[sym] = info
        if not out:
            raise BingXError("la lista de contratos llegó vacía")
        self.contracts = out
        self.contracts_ts = time.time()
        log.info("Contratos USDT cripto activos: %d", len(out))

    def select_symbols(self, tickers: dict) -> list:
        out = []
        for sym in self.contracts:
            t = tickers.get(sym)
            if t and t.get("qv") is not None and t["qv"] < self.cfg.min_quote_vol:
                continue
            out.append(sym)
        return out

    # ───────────────────────────── descarga ─────────────────────────────
    def fetch(self, sym, need_daily, limit):
        bars = self.client.klines(sym, self.cfg.timeframe, limit)
        daily = None
        if need_daily:
            try:
                daily = self.client.klines(sym, "1d", self.cfg.daily_bars)
            except BingXError as e:
                log.debug("1d %s falló: %s", sym, e)
        return bars, daily

    def _check_daily_alignment(self, daily):
        if self.daily_checked or not daily:
            return
        self.daily_checked = True
        off = daily[-1].ts % DAY_MS
        if off == 0:
            log.info("Velas 1d de BingX alineadas a 00:00 UTC ✔")
        else:
            msg = (f"⚠️ Las velas diarias de BingX NO empiezan a las 00:00 UTC (desfase {off / 3_600_000:.1f} h). "
                   "El contexto diario puede ir desfasado respecto a TradingView.")
            log.warning(msg)
            self.tg.send(msg)

    # ───────────────────────────── procesado ─────────────────────────────
    def manage(self, pos, bars, ctx, d0):
        """Avanza una señal abierta con las velas nuevas y avisa de TP1 / stop / trail."""
        cfg = self.cfg
        for idx, b in enumerate(bars):
            if b.ts <= pos.last_ts:
                continue
            prev = bars[idx - 1] if idx > 0 else None
            bday = b.ts - b.ts % DAY_MS
            first_day = prev is not None and (prev.ts - prev.ts % DAY_MS) != bday
            trail = pclose = None
            if ctx is not None and bday == d0:
                trail = ctx.ema10 if cfg.trail_ema_len == 10 else ctx.ema20
                pclose = ctx.prior_close
            for ev in advance_position(pos, b, cfg, self.tf_ms, first_day, pclose, trail):
                self.tg.send(format_event(pos, ev, cfg))
                if pos.closed:
                    self.state.close_position(pos, ev.kind, ev.ts)
            if pos.closed:
                return
        self.state.dirty = True

    def process(self, sym, bars_raw, daily_raw, day0, now_ms, stats, radar_ep, radar_ps):
        cfg, tf_ms = self.cfg, self.tf_ms
        if daily_raw:
            self.daily[sym] = daily_raw
            self.daily_day[sym] = day0
            self._check_daily_alignment(daily_raw)

        bars = [b for b in bars_raw if b.ts + tf_ms <= now_ms]  # solo velas cerradas
        if len(bars) < 40:
            stats["pocas_velas"] += 1
            return
        last = bars[-1]
        if last.ts < (now_ms // tf_ms) * tf_ms - tf_ms:
            stats["velas_atrasadas"] += 1
        d0 = last.ts - last.ts % DAY_MS
        daily = self.daily.get(sym)
        ctx = compute_daily_ctx(daily, d0, cfg.ps_lookback) if daily else None

        pos = self.state.positions.get(sym)
        if pos is not None:
            self.manage(pos, bars, ctx, d0)
        if ctx is None:
            stats["sin_contexto_diario"] += 1
            return

        res = evaluate(sym, bars, ctx, cfg, cfg.tf_min, blocked=sym in self.state.positions)
        stats["analizadas"] += 1
        if res.ep_armed:
            stats["ep_armados"] += 1
        if res.ps_active:
            stats["ps_activos"] += 1

        age_ms = cfg.max_signal_age_bars * tf_ms
        sent_signal = False
        for s in res.signals:
            if s.ts < last.ts - age_ms or sym in self.state.positions:
                continue
            if not self.state.once(f"sig|{sym}|{s.setup}|{s.ts}"):
                continue
            self.state.positions[sym] = position_from_signal(s)
            self.tg.send(format_signal(s, cfg, self.contracts.get(sym)))
            self.state.save()
            stats["señales"] += 1
            sent_signal = True

        if sent_signal:  # la moneda ya tuvo señal hoy: que no aparezca también en el radar
            self.state.once(f"radar|{d0}|{sym}|EP")
            self.state.once(f"radar|{d0}|{sym}|PS")
        if not cfg.send_radar or sent_signal or sym in self.state.positions:
            return
        if res.ep_armed and res.ep_arm_info and cfg.setups in ("BOTH", "EP"):
            fresh = res.ep_arm_ts is not None and res.ep_arm_ts >= last.ts - age_ms
            if (fresh or self.first_cycle) and self.state.once(f"radar|{d0}|{sym}|EP"):
                i, now = res.ep_arm_info, res.last
                radar_ep.append({"sym": sym, "move": max(now["chg"], now["gap"]), "rvol": now["rvol"],
                                 "adr": i["adr"], "or_h": now["or_h"], "stop_est": i.get("stop_est")})
        if res.ps_active and res.ps_info and cfg.setups in ("BOTH", "PS"):
            fresh = res.ps_first_ts is not None and res.ps_first_ts >= last.ts - age_ms
            if (fresh or self.first_cycle) and self.state.once(f"radar|{d0}|{sym}|PS"):
                now = res.last
                radar_ps.append({"sym": sym, "gain": now["gain"], "green": now["green"], "ext": now["ext"],
                                 "or_l": now["or_l"], "lookback": cfg.ps_lookback})

    # ───────────────────────────── ciclo ─────────────────────────────
    def maybe_daily_summary(self, day0):
        prev = self.state.meta.get("summary_day")
        self.state.meta["summary_day"] = day0
        if prev is None or prev >= day0 or not self.cfg.send_daily_summary:
            return
        msg = format_summary(self.state, prev, day0)
        if msg:
            self.tg.send(msg, silent=True)

    def cycle(self, now_ms: int | None = None):
        cfg, tf_ms = self.cfg, self.tf_ms
        t0 = time.time()
        now_ms = int(now_ms if now_ms is not None else t0 * 1000)
        exp_ts = (now_ms // tf_ms) * tf_ms - tf_ms          # apertura de la última vela cerrada
        day0 = exp_ts - exp_ts % DAY_MS

        if not self.contracts or t0 - self.contracts_ts > CONTRACTS_REFRESH_SEC:
            try:
                self.load_contracts()
            except BingXError as e:
                if not self.contracts:
                    raise
                log.warning("No pude refrescar contratos (%s); sigo con la lista anterior", e)
        try:
            tickers = self.client.tickers()
        except BingXError as e:
            log.warning("tickers falló (%s); sin filtro de liquidez este ciclo", e)
            tickers = {}

        symbols = self.select_symbols(tickers)
        for s in self.state.positions:           # siempre gestionar las señales abiertas
            if s not in symbols and s in self.contracts:
                symbols.append(s)
        self.maybe_daily_summary(day0)

        bars_needed = int((exp_ts - day0) // tf_ms) + 1 + cfg.warmup_bars
        limit = max(60, min(1440, bars_needed))

        stats, radar_ep, radar_ps = Counter(), [], []
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
            futs = {ex.submit(self.fetch, s, self.daily_day.get(s) != day0, limit): s for s in symbols}
            for fut in as_completed(futs):
                if self.stop:
                    break
                sym = futs[fut]
                try:
                    bars_raw, daily_raw = fut.result()
                except Exception as e:
                    stats["errores_api"] += 1
                    log.debug("fetch %s: %s", sym, e)
                    continue
                try:
                    self.process(sym, bars_raw, daily_raw, day0, now_ms, stats, radar_ep, radar_ps)
                except Exception:
                    stats["errores_proceso"] += 1
                    log.exception("Error procesando %s", sym)

        if radar_ep or radar_ps:
            radar_ep.sort(key=lambda x: -(x["move"] or 0))
            radar_ps.sort(key=lambda x: -(x["gain"] or 0))
            self.tg.send(format_radar(radar_ep, radar_ps, cfg, exp_ts + tf_ms, cfg.radar_max), silent=True)

        self.state.prune()
        self.state.save()
        dt = time.time() - t0
        log.info("Ciclo %s UTC: %d/%d monedas · EP armados %d · PS activos %d · señales %d · "
                 "abiertas %d · errores API %d · %.0fs",
                 time.strftime("%H:%M", time.gmtime(exp_ts / 1000 + cfg.tf_min * 60)),
                 stats["analizadas"], len(symbols), stats["ep_armados"], stats["ps_activos"],
                 stats["señales"], len(self.state.positions), stats["errores_api"], dt)
        if stats:
            log.debug("Detalle: %s", dict(stats))
        if symbols and stats["errores_api"] > 0.5 * len(symbols):
            self.notify_error(f"⚠️ BingX respondió con errores en {stats['errores_api']}/{len(symbols)} monedas. "
                              "Sigo reintentando en el próximo ciclo.")
        if dt > cfg.tf_min * 60:
            log.warning("El ciclo tardó %.0fs (> %dm). Baja MIN_QUOTE_VOL_24H o sube API_RPS.", dt, cfg.tf_min)
        self.first_cycle = False
        return stats

    # ───────────────────────────── bucle ─────────────────────────────
    def notify_error(self, text):
        if time.time() - self._last_err_notify > ERROR_NOTIFY_SEC:
            self._last_err_notify = time.time()
            self.tg.send(text, silent=True)

    def sleep_until_next_close(self):
        tf = self.tf_ms / 1000.0
        target = (time.time() // tf + 1) * tf + self.cfg.candle_delay_sec
        while not self.stop and time.time() < target:
            time.sleep(min(1.0, max(0.0, target - time.time())))

    def run(self):
        for s in (sig_mod.SIGTERM, sig_mod.SIGINT):
            sig_mod.signal(s, lambda *_: setattr(self, "stop", True))
        while not self.stop:
            try:
                self.load_contracts()
                break
            except BingXError as e:
                log.error("No pude cargar los contratos de BingX: %s. Reintento en 30 s", e)
                time.sleep(30)
        if self.stop:
            return
        if self.cfg.send_startup:
            note = f"Estado persistente: {os.path.dirname(os.path.abspath(self.state.path))}"
            self.tg.send(format_startup(self.cfg, len(self.select_symbols({})), note), silent=True)
        while not self.stop:
            try:
                self.cycle()
            except Exception as e:
                log.exception("Error en el ciclo")
                self.notify_error(f"⚠️ Error en el ciclo: {type(e).__name__}: {str(e)[:200]}")
            if self.cfg.run_once:
                break
            self.sleep_until_next_close()
        self.state.save()
        log.info("Bot detenido")


def main():
    cfg = Config.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    Bot(cfg).run()


if __name__ == "__main__":
    main()
