"""strategy.py — Port del Pine v6 "Qullamaggie Fused v2 - EP Long + Parabolic Short".

Reglas de fidelidad con el Pine:
  · Solo velas CERRADAS (equivale a barstate.isconfirmed).
  · Datos diarios = valores [1] con lookahead_on: cierre previo, ADR20, volumen medio 20d,
    EMA10/EMA20 diarias, racha de días verdes y mínimo de los últimos N días, todos
    calculados con días COMPLETOS anteriores al día actual (sin repintado).
  · La sesión es el día UTC (cripto 24/7). Opening Range = primeros OR_MINUTES del día.
  · RVOL a esta hora = volumen acumulado hoy / (volumen medio diario × fracción del día).
  · Se reproduce el día actual vela a vela (EP armado, trades del día, posición virtual).
"""
from __future__ import annotations

import math
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Optional

Bar = namedtuple("Bar", "ts o h l c v")  # ts = apertura de la vela (ms, UTC)
MIN_MS = 60_000
DAY_MS = 86_400_000


# ═══════════════════════════ Indicadores ═══════════════════════════
def ema_list(values, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for x in values:
        e = x if e is None else a * x + (1 - a) * e
        out.append(e)
    return out


def atr_list(bars, n=14):
    """ATR de Wilder (ta.atr = rma del true range)."""
    out = [None] * len(bars)
    if len(bars) < n:
        return out
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            tr.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    r = sum(tr[:n]) / n
    out[n - 1] = r
    for i in range(n, len(bars)):
        r = (r * (n - 1) + tr[i]) / n
        out[i] = r
    return out


def vwap_list(bars):
    """VWAP de hlc3 anclado al día UTC (ta.vwap por sesión)."""
    out, cur, pv, vv = [], None, 0.0, 0.0
    for b in bars:
        d = b.ts - b.ts % DAY_MS
        if d != cur:
            cur, pv, vv = d, 0.0, 0.0
        tp = (b.h + b.l + b.c) / 3.0
        pv += tp * b.v
        vv += b.v
        out.append(pv / vv if vv > 0 else tp)
    return out


def highest_prev_list(values, n=10):
    """ta.highest(x, n)[1]: máximo de las n velas anteriores a la actual."""
    out = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = max(values[i - n:i])
    return out


# ═══════════════════════════ Contexto diario ═══════════════════════════
@dataclass
class DailyCtx:
    day_start: int
    prior_close: float
    adr: float            # SMA20 de (high-low)/close*100
    avg_vol: float        # SMA20 del volumen diario
    ema10: float
    ema20: float
    green_streak: int
    base_low: float       # mínimo de los últimos `lookback` días completos
    n_days: int


def compute_daily_ctx(daily, day_start, lookback) -> Optional[DailyCtx]:
    """Contexto diario para el día que empieza en `day_start` (ms UTC).
    Solo usa velas diarias COMPLETAS (la vela del día en curso se descarta)."""
    comp = [b for b in daily if b.ts + DAY_MS <= day_start]
    if len(comp) < max(20, lookback):
        return None
    if comp[-1].ts + DAY_MS < day_start - DAY_MS:
        return None  # hueco: el último cierre diario es demasiado viejo
    last20 = comp[-20:]
    ratios = [(b.h - b.l) / b.c * 100.0 for b in last20 if b.c > 0]
    if len(ratios) < 20:
        return None
    closes = [b.c for b in comp]
    gs = 0
    for b in comp:
        gs = gs + 1 if b.c > b.o else 0
    return DailyCtx(
        day_start=day_start,
        prior_close=comp[-1].c,
        adr=sum(ratios) / 20.0,
        avg_vol=sum(b.v for b in last20) / 20.0,
        ema10=ema_list(closes, 10)[-1],
        ema20=ema_list(closes, 20)[-1],
        green_streak=gs,
        base_low=min(b.l for b in comp[-lookback:]),
        n_days=len(comp),
    )


# ═══════════════════════════ Posición / señales ═══════════════════════════
@dataclass
class Position:
    id: str
    symbol: str
    setup: str            # "EP Long" | "Parabolic Short"
    side: str             # "long" | "short"
    entry: float
    stop: float           # stop vigente
    initial_stop: float
    risk: float           # distancia inicial al stop (1R)
    target: float         # TP1
    entry_ts: int
    last_ts: int
    partial_taken: bool = False
    remaining: float = 1.0
    realized_r: float = 0.0
    closed: bool = False


@dataclass
class Event:
    kind: str             # TP1 | STOP | BE | TRAIL | TIME
    price: float
    ts: int
    total_r: float


@dataclass
class Signal:
    symbol: str
    setup: str
    side: str
    ts: int
    entry: float
    stop: float
    risk: float
    target: float
    stop_pct: float
    tp_pct: float
    size: dict
    ctx: dict
    trigger: str
    trade_n: int


def position_from_signal(sig: Signal) -> Position:
    return Position(id=f"{sig.symbol}|{sig.ts}", symbol=sig.symbol, setup=sig.setup, side=sig.side,
                    entry=sig.entry, stop=sig.stop, initial_stop=sig.stop, risk=sig.risk,
                    target=sig.target, entry_ts=sig.ts, last_ts=sig.ts)


def size_position(entry, stop_dist, risk_pct, cfg) -> dict:
    """Mismo dimensionado que el Pine: riesgo fijo % del equity, tope de nocional."""
    equity = cfg.account_equity
    risk_cash = equity * risk_pct / 100.0
    qty = risk_cash / stop_dist if stop_dist > 0 else 0.0
    max_qty = equity * cfg.max_pos_pct / 100.0 / entry
    capped = qty > max_qty
    qty = min(qty, max_qty)
    notional = qty * entry
    return {"equity": equity, "risk_pct": risk_pct, "risk_cash": risk_cash, "qty": qty,
            "notional": notional, "notional_pct": notional / equity * 100.0,
            "real_risk": qty * stop_dist, "capped": capped}


def _close_remaining(pos: Position, px: float):
    sgn = 1 if pos.side == "long" else -1
    pos.realized_r += pos.remaining * sgn * (px - pos.entry) / pos.risk
    pos.remaining = 0.0
    pos.closed = True


def advance_position(pos: Position, bar: Bar, cfg, tf_ms: int, first_bar_of_day=False,
                     prior_close=None, trail_ema=None) -> list:
    """Aplica UNA vela cerrada a la posición y devuelve los eventos.

    Convenciones (conservadoras, como el broker emulator de TradingView):
      · Si la vela toca stop y TP1 a la vez, se asume que salta primero el stop.
      · Si abre con gap más allá del stop, el stop se ejecuta en la apertura.
      · Tras TP1 el stop pasa a breakeven desde la vela SIGUIENTE.
      · Trail: al abrir un día nuevo, si el cierre diario previo queda del lado malo de la
        EMA diaria (solo tras el parcial), se cierra el resto.
    """
    ev: list = []
    if pos.closed or bar.ts <= pos.last_ts:
        return ev
    pos.last_ts = bar.ts
    long = pos.side == "long"
    sgn = 1 if long else -1

    # 1) stop
    if (bar.l <= pos.stop) if long else (bar.h >= pos.stop):
        px = min(pos.stop, bar.o) if long else max(pos.stop, bar.o)
        kind = "BE" if pos.partial_taken else "STOP"
        _close_remaining(pos, px)
        ev.append(Event(kind, px, bar.ts, pos.realized_r))
        return ev

    # 2) TP1 (parcial)
    if not pos.partial_taken and ((bar.h >= pos.target) if long else (bar.l <= pos.target)):
        px = max(pos.target, bar.o) if long else min(pos.target, bar.o)
        frac = cfg.partial_pct / 100.0
        pos.realized_r += frac * sgn * (px - pos.entry) / pos.risk
        pos.remaining -= frac
        pos.partial_taken = True
        pos.stop = pos.entry  # BE vigente desde la próxima vela
        ev.append(Event("TP1", px, bar.ts, pos.realized_r))

    # 3) trail con EMA diaria (solo tras el parcial, al abrir día nuevo)
    if (cfg.use_trail_ema and first_bar_of_day and pos.partial_taken
            and prior_close is not None and trail_ema is not None):
        bad = prior_close < trail_ema if long else prior_close > trail_ema
        if bad:
            _close_remaining(pos, bar.c)
            ev.append(Event("TRAIL", bar.c, bar.ts, pos.realized_r))
            return ev

    # 4) salida por tiempo
    if cfg.max_hold_bars > 0 and (bar.ts - pos.entry_ts) // tf_ms >= cfg.max_hold_bars:
        _close_remaining(pos, bar.c)
        ev.append(Event("TIME", bar.c, bar.ts, pos.realized_r))
    return ev


# ═══════════════════════════ Evaluación ═══════════════════════════
@dataclass
class EvalResult:
    symbol: str
    signals: list = field(default_factory=list)
    ep_armed: bool = False
    ep_arm_ts: Optional[int] = None
    ep_arm_info: Optional[dict] = None
    ps_active: bool = False
    ps_first_ts: Optional[int] = None
    ps_info: Optional[dict] = None
    trades_today: int = 0
    last: dict = field(default_factory=dict)


def evaluate(symbol, bars, ctx, cfg, tf_min, blocked=False) -> EvalResult:
    """Reproduce el día actual (UTC) vela a vela y devuelve las señales generadas.

    bars    : velas CERRADAS, ascendentes, con histórico previo (warmup) + el día actual.
    ctx     : DailyCtx del día de la última vela.
    blocked : hay una posición real abierta en el símbolo → no se generan entradas.
    """
    res = EvalResult(symbol=symbol)
    n = len(bars)
    if ctx is None or n < 30:
        return res
    tf_ms = tf_min * MIN_MS
    day0 = bars[-1].ts - bars[-1].ts % DAY_MS
    i0 = 0
    for k in range(n - 1, -1, -1):
        if bars[k].ts < day0:
            i0 = k + 1
            break

    ema21 = ema_list([b.c for b in bars], 21)
    atr = atr_list(bars, 14)
    vwap = vwap_list(bars)
    hh10 = highest_prev_list([b.h for b in bars], 10)

    ep_on = cfg.setups in ("BOTH", "EP")
    ps_on = cfg.setups in ("BOTH", "PS")
    trail_ema = ctx.ema10 if cfg.trail_ema_len == 10 else ctx.ema20

    pos: Optional[Position] = None
    trades = 0
    ep_armed = False
    day_open = day_high = day_low = None
    day_vol = 0.0
    or_h = or_l = None

    for i in range(i0, n):
        b = bars[i]
        pb = bars[i - 1] if i > 0 else b
        pi = i - 1 if i > 0 else 0
        c, pc = b.c, pb.c

        # 1) salidas de la posición virtual en esta vela (antes de evaluar señales)
        if pos is not None:
            advance_position(pos, b, cfg, tf_ms, first_bar_of_day=(i == i0),
                             prior_close=ctx.prior_close, trail_ema=trail_ema)
            if pos.closed:
                pos = None

        # 2) estado diario
        if i == i0:
            day_open, day_high, day_low, day_vol = b.o, b.h, b.l, b.v
            or_h = or_l = None
        else:
            day_high = max(day_high, b.h)
            day_low = min(day_low, b.l)
            day_vol += b.v
        p_or_h, p_or_l = or_h, or_l  # valores de la vela anterior (para cruces)

        mfd = (b.ts - day0) / MIN_MS            # minutos desde el inicio del día (apertura de vela)
        mfo = mfd - cfg.sess_offset_min         # minutos desde la apertura de sesión
        in_or = 0 <= mfo < cfg.or_minutes
        or_done = mfo >= cfg.or_minutes
        if in_or:
            or_h = b.h if or_h is None else max(or_h, b.h)
            or_l = b.l if or_l is None else min(or_l, b.l)

        frac = min(max(mfd / cfg.day_len_min, 0.05), 1.0)
        rvol = day_vol / (ctx.avg_vol * frac) if ctx.avg_vol > 0 else None
        in_window = mfo >= cfg.skip_first_min and (cfg.window_min == 0 or mfo <= cfg.window_min)

        e21, vw, a, h10 = ema21[i], vwap[i], atr[i], hh10[i]
        x_orh = (or_h is not None and p_or_h is not None and c > or_h and pc <= p_or_h)
        x_orl = (or_l is not None and p_or_l is not None and c < or_l and pc >= p_or_l)
        x_hh = (h10 is not None and hh10[pi] is not None and c > h10 and pc <= hh10[pi])
        vwap_up = c > e21 and c > vw and pc <= max(ema21[pi], vwap[pi])
        first_red = c < b.o and pc >= pb.o
        vwap_fail = c < vw and pc >= vw and b.h >= vw

        # 3) contexto EP
        chg = (c - ctx.prior_close) / ctx.prior_close * 100.0
        gap = (day_open - ctx.prior_close) / ctx.prior_close * 100.0
        move = max(chg, gap)
        ep_context = (move >= cfg.ep_min_change and ctx.adr >= cfg.ep_min_adr
                      and rvol is not None and rvol >= cfg.ep_min_rvol)
        just_armed = ep_context and not ep_armed
        if ep_context:
            ep_armed = True

        trig_orh = (cfg.ep_trigger == "ORH" and or_done and or_h is not None
                    and (x_orh or (just_armed and c > or_h)))
        trig_hh = cfg.ep_trigger == "HH" and x_hh
        trig_vw = cfg.ep_trigger == "VWAP_EMA" and vwap_up
        ep_hit = trig_orh or trig_hh or trig_vw

        # 4) contexto Parabolic Short
        gain = (c - ctx.base_low) / ctx.base_low * 100.0 if ctx.base_low > 0 else None
        ext = (c - ctx.ema20) / ctx.ema20 * 100.0 if ctx.ema20 > 0 else None
        ps_context = (gain is not None and ext is not None and gain >= cfg.ps_min_gain
                      and ctx.green_streak >= cfg.ps_min_green and ext >= cfg.ps_min_ext)
        ps_hit = ((cfg.ps_trigger == "ORL" and or_done and x_orl)
                  or (cfg.ps_trigger == "FIRST_RED" and first_red)
                  or (cfg.ps_trigger == "VWAP_FAIL" and vwap_fail))

        def snap():
            return {"price": c, "prior_close": ctx.prior_close, "chg": chg, "gap": gap,
                    "rvol": rvol, "adr": ctx.adr, "or_h": or_h, "or_l": or_l, "vwap": vw,
                    "ema21": e21, "atr": a, "day_high": day_high, "day_low": day_low,
                    "day_open": day_open, "gain": gain, "ext": ext, "green": ctx.green_streak,
                    "base_low": ctx.base_low, "ema10d": ctx.ema10, "ema20d": ctx.ema20,
                    "lookback": cfg.ps_lookback}

        if ep_context and just_armed and res.ep_arm_ts is None:
            res.ep_arm_ts = b.ts
            info = snap()
            ref = (day_low if cfg.stop_mode == "DAY" else or_l if cfg.stop_mode == "OR"
                   else c - 2 * a if a is not None else None)
            info["stop_est"] = ref - cfg.sl_buf * a if (ref is not None and a is not None) else None
            res.ep_arm_info = info
        if ps_context and res.ps_first_ts is None:
            res.ps_first_ts = b.ts
            res.ps_info = snap()

        # 5) señales
        can_trade = (pos is None and not blocked and trades < cfg.max_trades_day and in_window)
        ep_signal = can_trade and ep_on and ep_armed and ep_hit
        ps_signal = can_trade and ps_on and ps_context and ps_hit and not ep_signal
        adr_dist = ctx.adr / 100.0 * c

        if ep_signal and a is not None:
            ref = day_low if cfg.stop_mode == "DAY" else or_l if cfg.stop_mode == "OR" else c - 2 * a
            if ref is not None:
                struct = ref - cfg.sl_buf * a
                dist = c - struct
                if 0 < dist <= adr_dist * cfg.max_adr_mult:
                    trades += 1
                    if trig_orh:
                        why = "Cierre rompe el máximo del Opening Range (ORH)"
                    elif trig_hh:
                        why = "Cierre rompe el máximo de las últimas 10 velas"
                    else:
                        why = "Cierre recupera VWAP y EMA21"
                    sig = Signal(symbol, "EP Long", "long", b.ts, c, struct, dist,
                                 c + dist * cfg.partial_r, dist / c * 100.0,
                                 dist * cfg.partial_r / c * 100.0,
                                 size_position(c, dist, cfg.risk_pct_ep, cfg), snap(), why, trades)
                    res.signals.append(sig)
                    pos = position_from_signal(sig)

        if ps_signal and a is not None:
            ref = day_high if cfg.stop_mode == "DAY" else or_h if cfg.stop_mode == "OR" else c + 2 * a
            if ref is not None:
                struct = ref + cfg.sl_buf * a
                dist = struct - c
                if 0 < dist <= adr_dist * cfg.max_adr_mult * 1.2:
                    trades += 1
                    if cfg.ps_trigger == "ORL":
                        why = "Cierre pierde el mínimo del Opening Range (ORL)"
                    elif cfg.ps_trigger == "FIRST_RED":
                        why = "Primera vela roja tras vela verde"
                    else:
                        why = "Rechazo bajista en VWAP"
                    sig = Signal(symbol, "Parabolic Short", "short", b.ts, c, struct, dist,
                                 c - dist * cfg.partial_r, dist / c * 100.0,
                                 dist * cfg.partial_r / c * 100.0,
                                 size_position(c, dist, cfg.risk_pct_short, cfg), snap(), why, trades)
                    res.signals.append(sig)
                    pos = position_from_signal(sig)

        if i == n - 1:
            res.last = snap()
            res.last.update({"in_window": in_window, "or_done": or_done, "bar_ts": b.ts})
            res.ep_armed = ep_armed
            res.ps_active = ps_context
            res.trades_today = trades
    return res
