"""test_bot.py — Tests sin red: python -m unittest -v test_bot"""
import os
import re
import tempfile
import time
import unittest
from collections import Counter
from unittest import mock

from bingx import RateLimiter
from config import Config, is_excluded
from main import Bot
from state import State
from strategy import (DAY_MS, MIN_MS, Bar, Position, advance_position, compute_daily_ctx,
                      evaluate, size_position)
from telegram_bot import fmt_price, fmt_qty, format_event, format_signal

TF_MS = 15 * MIN_MS
DAY0 = 20_400 * DAY_MS  # día UTC cualquiera, alineado a 00:00


def flat_bars(start_ts, n, price, half=0.1, vol=10.0):
    return [Bar(start_ts + i * TF_MS, price, price + half, price - half, price, vol) for i in range(n)]


def daily_flat(day0, n=40):
    return [Bar(day0 - (n - i) * DAY_MS, 99, 105, 95, 100, 1000.0) for i in range(n)]


def daily_ramp(day0):
    """25 días planos + 15 días de subida (+6% diario, todos verdes)."""
    out, p = [], 100.0
    for i in range(25):
        out.append(Bar(day0 - (40 - i) * DAY_MS, 100, 101, 99, 100, 1000.0))
    for i in range(15):
        o, c = p, p * 1.06
        out.append(Bar(day0 - (15 - i) * DAY_MS, o, c * 1.02, o * 0.98, c, 1000.0))
        p = c
    return out, p


class FakeClient:
    def __init__(self):
        self.now_ms = 0
        self.bars = {}
        self.daily = {}
        self.contract_list = []
        self.ticker_map = {}

    def contracts(self):
        return self.contract_list

    def tickers(self):
        return self.ticker_map

    def klines(self, symbol, interval, limit=300):
        if interval == "1d":
            return list(self.daily[symbol])
        return [b for b in self.bars[symbol] if b.ts <= self.now_ms][-limit:]


class FakeTG:
    def __init__(self):
        self.msgs = []

    def send(self, text, silent=False):
        self.msgs.append(text)
        return True


# ───────────────────────── escenarios ─────────────────────────
def ep_scenario(gap=False):
    """Día de explosión: +8% con RVOL alto. gap=True → abre con gap y NO rompe el ORH."""
    daily = daily_flat(DAY0)
    prev = flat_bars(DAY0 - DAY_MS, 96, 100.0)
    if gap:
        today = [Bar(DAY0, 108, 109, 107.5, 108.5, 50.0),
                 Bar(DAY0 + TF_MS, 108.5, 108.8, 107.6, 108, 50.0)]
    else:
        today = [Bar(DAY0, 100, 101, 99.5, 100.5, 50.0),
                 Bar(DAY0 + TF_MS, 100.5, 108.5, 100.4, 108, 50.0),
                 Bar(DAY0 + 2 * TF_MS, 108, 125, 107.9, 120, 40.0),   # toca TP1
                 Bar(DAY0 + 3 * TF_MS, 120, 121, 107, 110, 40.0),     # vuelve a BE
                 Bar(DAY0 + 4 * TF_MS, 110, 111, 109, 110, 40.0)]
    return daily, prev + today


class TestIndicatorsAndCtx(unittest.TestCase):
    def test_daily_ctx_descarta_vela_parcial_de_hoy(self):
        daily = daily_flat(DAY0) + [Bar(DAY0, 100, 130, 100, 128, 300)]  # vela en formación
        ctx = compute_daily_ctx(daily, DAY0, 15)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.prior_close, 100)
        self.assertAlmostEqual(ctx.adr, 10.0)
        self.assertEqual(ctx.avg_vol, 1000.0)

    def test_daily_ctx_pocas_velas(self):
        self.assertIsNone(compute_daily_ctx(daily_flat(DAY0, 10), DAY0, 15))

    def test_daily_ctx_hueco_viejo(self):
        self.assertIsNone(compute_daily_ctx(daily_flat(DAY0 - 5 * DAY_MS), DAY0, 15))

    def test_size_position(self):
        cfg = Config()
        s = size_position(100.0, 5.0, 0.35, cfg)
        self.assertAlmostEqual(s["risk_cash"], 3.5)
        self.assertAlmostEqual(s["qty"], 0.7)
        self.assertFalse(s["capped"])
        s2 = size_position(100.0, 0.01, 0.35, cfg)      # stop minúsculo → tope de nocional
        self.assertTrue(s2["capped"])
        self.assertAlmostEqual(s2["notional_pct"], 22.0)


class TestEvaluate(unittest.TestCase):
    def test_ep_long_se_dispara(self):
        daily, bars = ep_scenario()
        cfg = Config()
        ctx = compute_daily_ctx(daily, DAY0, 15)
        res = evaluate("AAA-USDT", bars[:98], ctx, cfg, 15)   # hasta la vela 00:15 inclusive
        self.assertEqual(len(res.signals), 1)
        s = res.signals[0]
        self.assertEqual((s.setup, s.side, s.ts), ("EP Long", "long", DAY0 + TF_MS))
        self.assertEqual(s.entry, 108)
        self.assertLess(s.stop, 99.5)                       # stop bajo el mínimo del día
        self.assertAlmostEqual(s.target, s.entry + s.risk * cfg.partial_r)
        self.assertTrue(res.ep_armed)
        self.assertEqual(res.ep_arm_ts, DAY0 + TF_MS)

    def test_sin_rvol_no_arma(self):
        daily, bars = ep_scenario()
        bars = bars[:96] + [Bar(b.ts, b.o, b.h, b.l, b.c, 1.0) for b in bars[96:98]]
        res = evaluate("AAA-USDT", bars, compute_daily_ctx(daily, DAY0, 15), Config(), 15)
        self.assertEqual(res.signals, [])
        self.assertFalse(res.ep_armed)

    def test_blocked_no_genera_entradas(self):
        daily, bars = ep_scenario()
        res = evaluate("AAA-USDT", bars[:98], compute_daily_ctx(daily, DAY0, 15), Config(), 15, blocked=True)
        self.assertEqual(res.signals, [])

    def test_setup_ps_only_ignora_ep(self):
        daily, bars = ep_scenario()
        cfg = Config(setups="PS")
        res = evaluate("AAA-USDT", bars[:98], compute_daily_ctx(daily, DAY0, 15), cfg, 15)
        self.assertEqual(res.signals, [])

    def test_ep_armado_sin_gatillo_va_al_radar(self):
        daily, bars = ep_scenario(gap=True)
        res = evaluate("BBB-USDT", bars, compute_daily_ctx(daily, DAY0, 15), Config(), 15)
        self.assertEqual(res.signals, [])
        self.assertTrue(res.ep_armed)
        self.assertIsNotNone(res.ep_arm_info)

    def test_parabolic_short_se_dispara(self):
        daily, C = daily_ramp(DAY0)
        prev = flat_bars(DAY0 - DAY_MS, 96, C, half=C * 0.001)
        today = [Bar(DAY0, C * 1.01, C * 1.03, C * 1.00, C * 1.02, 30.0),
                 Bar(DAY0 + TF_MS, C * 1.02, C * 1.02, C * 0.96, C * 0.97, 30.0)]
        cfg = Config()
        ctx = compute_daily_ctx(daily, DAY0, 15)
        res = evaluate("PPP-USDT", prev + today, ctx, cfg, 15)
        self.assertEqual(len(res.signals), 1, res.last)
        s = res.signals[0]
        self.assertEqual((s.setup, s.side), ("Parabolic Short", "short"))
        self.assertGreater(s.stop, s.entry)
        self.assertLess(s.target, s.entry)
        self.assertGreaterEqual(s.ctx["gain"], 50)
        self.assertGreaterEqual(s.ctx["ext"], 25)

    def test_maximo_trades_por_dia(self):
        daily, bars = ep_scenario()
        cfg = Config(max_trades_day=1)
        res = evaluate("AAA-USDT", bars, compute_daily_ctx(daily, DAY0, 15), cfg, 15)
        self.assertLessEqual(len(res.signals), 1)
        self.assertLessEqual(res.trades_today, 1)


class TestAdvance(unittest.TestCase):
    def pos(self, side="long"):
        if side == "long":
            return Position("x", "AAA-USDT", "EP Long", "long", 100, 95, 95, 5, 109, 0, 0)
        return Position("x", "AAA-USDT", "Parabolic Short", "short", 100, 105, 105, 5, 91, 0, 0)

    def test_stop_directo(self):
        p, cfg = self.pos(), Config()
        ev = advance_position(p, Bar(1, 100, 101, 94, 96, 1), cfg, TF_MS)
        self.assertEqual([e.kind for e in ev], ["STOP"])
        self.assertAlmostEqual(p.realized_r, -1.0)
        self.assertTrue(p.closed)

    def test_tp1_luego_be(self):
        p, cfg = self.pos(), Config()
        ev = advance_position(p, Bar(1, 100, 110, 99, 108, 1), cfg, TF_MS)
        self.assertEqual([e.kind for e in ev], ["TP1"])
        self.assertAlmostEqual(p.realized_r, 0.9)
        self.assertEqual(p.stop, 100)
        ev = advance_position(p, Bar(2, 108, 109, 99.9, 101, 1), cfg, TF_MS)
        self.assertEqual([e.kind for e in ev], ["BE"])
        self.assertAlmostEqual(p.realized_r, 0.9)

    def test_vela_toca_stop_y_tp1_gana_el_stop(self):
        p = self.pos()
        ev = advance_position(p, Bar(1, 100, 120, 90, 100, 1), Config(), TF_MS)
        self.assertEqual([e.kind for e in ev], ["STOP"])

    def test_short_simetrico(self):
        p, cfg = self.pos("short"), Config()
        ev = advance_position(p, Bar(1, 100, 101, 90, 92, 1), cfg, TF_MS)
        self.assertEqual([e.kind for e in ev], ["TP1"])
        self.assertAlmostEqual(p.realized_r, 0.9)
        ev = advance_position(p, Bar(2, 92, 100.5, 91, 99, 1), cfg, TF_MS)
        self.assertEqual([e.kind for e in ev], ["BE"])

    def test_trail_ema_solo_tras_parcial(self):
        cfg = Config()
        p = self.pos()
        advance_position(p, Bar(1, 100, 101, 99, 100, 1), cfg, TF_MS, True, prior_close=90, trail_ema=95)
        self.assertFalse(p.closed)                       # sin parcial no hay trail
        advance_position(p, Bar(2, 100, 110, 99, 108, 1), cfg, TF_MS)
        ev = advance_position(p, Bar(3, 108, 109, 107, 108, 1), cfg, TF_MS, True, prior_close=90, trail_ema=95)
        self.assertEqual([e.kind for e in ev], ["TRAIL"])
        self.assertAlmostEqual(p.realized_r, 0.9 + 0.5 * (108 - 100) / 5)

    def test_gap_pasa_el_stop(self):
        p = self.pos()
        advance_position(p, Bar(1, 90, 91, 88, 90, 1), Config(), TF_MS)
        self.assertLess(p.realized_r, -1.0)


class TestFormatoYConfig(unittest.TestCase):
    def test_fmt_price_sin_notacion_cientifica(self):
        for p in (0.00000123, 1.5e-7, 0.0456, 1.2345, 65432.1):
            self.assertNotRegex(fmt_price(p), r"[eE]")
        self.assertEqual(fmt_price(65432.1), "65432.10")
        self.assertEqual(fmt_price(0.00000123), "0.0000012300")
        self.assertEqual(fmt_qty(1.2399, 2), "1.23")

    def test_excluidos(self):
        for s in ("NCCOGOLD2USD-USDT", "NCSKAAPL2USD-USDT", "USDC-USDT", "XAU-USDT"):
            self.assertTrue(is_excluded(s), s)
        for s in ("BTC-USDT", "SPX-USDT", "NCT-USDT", "1000PEPE-USDT"):
            self.assertFalse(is_excluded(s), s)
        self.assertTrue(is_excluded("DOGE-USDT", ("DOGE",)))

    def test_config_env(self):
        env = {"SETUPS": "ps", "STOP_MODE": "or", "TRAIL_EMA_LEN": "15", "EP_MIN_CHANGE": "9,5",
               "TIMEFRAME": "7m", "EP_TRIGGER": "vwap/ema"}
        with mock.patch.dict(os.environ, env, clear=False):
            c = Config.from_env()
        self.assertEqual((c.setups, c.stop_mode, c.trail_ema_len, c.ep_min_change, c.timeframe, c.ep_trigger),
                         ("PS", "OR", 10, 9.5, "15m", "VWAP_EMA"))

    def test_mensajes_html_validos(self):
        daily, bars = ep_scenario()
        cfg = Config()
        res = evaluate("AAA-USDT", bars[:98], compute_daily_ctx(daily, DAY0, 15), cfg, 15)
        txt = format_signal(res.signals[0], cfg, {"qty_prec": 1, "min_qty": 1})
        for tag in ("b", "code", "a"):
            self.assertEqual(len(re.findall(rf"<{tag}[ >]", txt)), len(re.findall(rf"</{tag}>", txt)), tag)
        self.assertEqual(re.findall(r"<(?!/?(?:b|code|a)[ >])", txt), [])
        self.assertIn("EP LONG", txt)
        p = Position("x", "AAA-USDT", "EP Long", "long", 100, 95, 95, 5, 109, 0, 0)
        for ev in advance_position(p, Bar(1, 100, 110, 99, 108, 1), cfg, TF_MS):
            self.assertIn("TP1", format_event(p, ev, cfg))

    def test_rate_limiter(self):
        rl, t0 = RateLimiter(50), time.monotonic()
        for _ in range(6):
            rl.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 5 / 50 - 0.005)

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            st = State(path)
            st.positions["AAA-USDT"] = Position("x", "AAA-USDT", "EP Long", "long", 100, 95, 95, 5, 109, 1, 1)
            self.assertTrue(st.once("k"))
            self.assertFalse(st.once("k"))
            st.save()
            st2 = State(path)
            self.assertEqual(st2.positions["AAA-USDT"].entry, 100)
            self.assertFalse(st2.once("k"))


class TestCicloCompleto(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(state_dir=tempfile.gettempdir())
        self.fc, self.tg = FakeClient(), FakeTG()
        d_ep, b_ep = ep_scenario()
        d_gap, b_gap = ep_scenario(gap=True)
        self.fc.daily = {"AAA-USDT": d_ep + [Bar(DAY0, 100, 108, 100, 107, 300)],  # con vela parcial de hoy
                         "BBB-USDT": d_gap}
        self.fc.bars = {"AAA-USDT": b_ep, "BBB-USDT": b_gap}
        self.fc.contract_list = [
            {"symbol": "AAA-USDT", "status": 1, "quantityPrecision": 1, "tradeMinQuantity": 0.1},
            {"symbol": "BBB-USDT", "status": 1, "quantityPrecision": 0},
            {"symbol": "ILLIQ-USDT", "status": 1},
            {"symbol": "NCCOGOLD2USD-USDT", "status": 1},
            {"symbol": "USDC-USDT", "status": 1},
            {"symbol": "OFF-USDT", "status": 0},
            {"symbol": "BTC-USD", "status": 1},
        ]
        self.fc.ticker_map = {"AAA-USDT": {"last": 108, "qv": 5e6}, "BBB-USDT": {"last": 108, "qv": 5e6},
                              "ILLIQ-USDT": {"last": 1, "qv": 10.0}}
        self.path = os.path.join(tempfile.mkdtemp(), "state.json")
        self.bot = Bot(self.cfg, client=self.fc, tg=self.tg, state_path=self.path)

    def run_cycle(self, minutes):
        now = DAY0 + minutes * MIN_MS + 8000
        self.fc.now_ms = now
        return self.bot.cycle(now_ms=now)

    def test_flujo_completo(self):
        st = self.run_cycle(30)                       # cierra la vela 00:15
        self.assertEqual(sorted(self.bot.contracts), ["AAA-USDT", "BBB-USDT", "ILLIQ-USDT"])
        self.assertEqual(st["analizadas"], 2)         # ILLIQ filtrada por liquidez
        self.assertEqual(st["señales"], 1)
        joined = "\n".join(self.tg.msgs)
        self.assertIn("EP LONG", joined)
        self.assertIn("AAA-USDT", joined)
        self.assertIn("RADAR", joined)                # BBB armada sin gatillo
        self.assertIn("BBB-USDT", joined)
        n = len(self.tg.msgs)

        self.run_cycle(30)                            # mismo ciclo otra vez: sin duplicados
        self.assertEqual(len(self.tg.msgs), n)

        self.run_cycle(45)                            # vela 00:30 toca TP1
        self.assertIn("TP1 alcanzado", self.tg.msgs[-1])

        self.run_cycle(60)                            # vela 00:45 vuelve a BE
        self.assertIn("breakeven", self.tg.msgs[-1])
        self.assertIn("+0.90R", self.tg.msgs[-1])
        self.assertEqual(self.bot.state.positions, {})
        self.assertEqual(len(self.bot.state.closed), 1)

        n = len(self.tg.msgs)
        self.run_cycle(75)                            # nada nuevo
        self.assertEqual(len(self.tg.msgs), n)

    def test_persistencia_evita_duplicar_tras_reinicio(self):
        self.run_cycle(30)
        n = len(self.tg.msgs)
        tg2 = FakeTG()
        bot2 = Bot(self.cfg, client=self.fc, tg=tg2, state_path=self.path)
        self.fc.now_ms = DAY0 + 30 * MIN_MS + 8000
        bot2.cycle(now_ms=self.fc.now_ms)
        self.assertEqual([m for m in tg2.msgs if "EP LONG" in m], [])
        self.assertIn("AAA-USDT", bot2.state.positions)
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
