"""Réplica vela a vela de 'Qullamaggie Fused v2' (EP Long + Parabolic Short).

El motor es SIN ESTADO: en cada ciclo se re-simula la ventana reciente (REPLAY_DAYS días)
desde un inicio de día UTC, igual que haría TradingView. Así el bot se recupera solo
tras un reinicio y las posiciones virtuales abiertas se siguen hasta su salida.
"""
import math

import numpy as np

from indicators import sma, ema, atr, rolling_min, highest_prev

DAY_MS = 86_400_000
nan = float("nan")
isnan = math.isnan

EMPTY_CTX = {"prior_close": nan, "adr": nan, "vavg": nan, "e10": nan, "e20": nan, "gs": nan, "base_low": nan}


# ═══════════════════════════════════════════════════════════════
# DATOS DIARIOS (equivalente a request.security(..., "D", f_daily) con [1])
# ═══════════════════════════════════════════════════════════════

def daily_series(ts, o, h, l, c, v, lookback):
    """Series diarias a partir de velas diarias YA CERRADAS."""
    ts = np.asarray(ts, dtype=np.int64)
    o = np.asarray(o, float)
    h = np.asarray(h, float)
    l = np.asarray(l, float)
    c = np.asarray(c, float)
    v = np.asarray(v, float)
    rng = (h - l) / np.maximum(c, 1e-12) * 100.0
    gs = np.zeros(len(c))
    run = 0
    for i in range(len(c)):
        run = run + 1 if c[i] > o[i] else 0
        gs[i] = run
    return {
        "days": ts // DAY_MS,
        "h": h,
        "close": c,
        "adr": sma(rng, 20),
        "vavg": sma(v, 20),
        "e10": ema(c, 10),
        "e20": ema(c, 20),
        "gs": gs,
        "lown": rolling_min(l, lookback),
    }


def ctx_for_day(ds, day):
    """Valores 'confirmados' para un día: los de la última vela diaria cerrada anterior a ese día."""
    j = int(np.searchsorted(ds["days"], day, side="left")) - 1
    if j < 0:
        return None
    return {
        "prior_close": float(ds["close"][j]),
        "adr": float(ds["adr"][j]),
        "vavg": float(ds["vavg"][j]),
        "e10": float(ds["e10"][j]),
        "e20": float(ds["e20"][j]),
        "gs": float(ds["gs"][j]),
        "base_low": float(ds["lown"][j]),
    }


def could_signal(ctx, hi, cfg):
    """Pre-filtro CONSERVADOR (superconjunto): ¿podría haber contexto EP/PS si el máximo del día fue `hi`?"""
    if ctx is None:
        return False
    pc = ctx["prior_close"]
    if not (pc > 0 and hi > 0):
        return False
    if cfg.ep_on and ctx["adr"] >= cfg.ep_min_adr and (hi - pc) / pc * 100.0 >= cfg.ep_min_change:
        return True
    if cfg.ps_on and ctx["gs"] >= cfg.ps_min_green:
        bl, e20 = ctx["base_low"], ctx["e20"]
        if bl > 0 and e20 > 0 and (hi - bl) / bl * 100.0 >= cfg.ps_min_gain and (hi - e20) / e20 * 100.0 >= cfg.ps_min_ext:
            return True
    return False


def prior_days_potential(ds, cfg, today):
    """¿Algún día anterior de la ventana de replay pudo generar una señal (y dejar una posición abierta)?"""
    days = ds["days"]
    for k in range(1, cfg.replay_days):
        d = today - k
        j = int(np.searchsorted(days, d, side="left"))
        if j < len(days) and days[j] == d and could_signal(ctx_for_day(ds, d), float(ds["h"][j]), cfg):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# ENTRADAS / SALIDAS
# ═══════════════════════════════════════════════════════════════

def _build_entry(cfg, symbol, side, setup, i, ts_i, tf_ms, close, atr_i, stop_ref, adr,
                 risk_pct, adr_limit, trigger, extra, levels, trades_today):
    buf = cfg.sl_buf * atr_i
    if side == 1:
        struct = stop_ref - buf
        dist = close - struct
    else:
        struct = stop_ref + buf
        dist = struct - close
    adr_dist = adr / 100.0 * close
    if not (dist > 0 and dist <= adr_dist * adr_limit):  # NaN -> descarta
        return None
    risk_cash = cfg.account_equity * risk_pct / 100.0
    qty = risk_cash / dist
    max_qty = cfg.account_equity * cfg.max_pos_pct / 100.0 / close
    capped = qty > max_qty
    qty = min(qty, max_qty)
    if not qty > 0:
        return None
    target = close + side * dist * cfg.partial_r
    pend = {"side": side, "setup": setup, "signal_i": i, "signal_ts": int(ts_i),
            "stop": struct, "risk": dist, "target": target}
    ev = {
        "type": "ENTRY", "symbol": symbol, "setup": setup, "side": side,
        "signal_ts": int(ts_i), "bar_ts": int(ts_i), "event_ts": int(ts_i) + tf_ms,
        "entry": float(close), "stop": struct, "risk": dist, "stop_pct": dist / close * 100.0,
        "tp1": target, "partial_r": cfg.partial_r, "partial_pct": cfg.partial_pct,
        "qty": qty, "notional": qty * close, "risk_cash": risk_cash, "risk_pct": risk_pct,
        "capped": capped, "adr": adr, "adr_mult": dist / adr_dist if adr_dist > 0 else nan,
        "trigger": trigger, "ctx": extra, "levels": levels, "trades_today": trades_today,
    }
    return pend, ev


def _exit_event(symbol, tr, kind, bar_i, bar_ts, px, frac, tf_ms, extra=None):
    r_part = tr["side"] * (px - tr["fill"]) / tr["risk"]
    tr["realized_r"] += frac * r_part
    ev = {
        "type": kind, "symbol": symbol, "setup": tr["setup"], "side": tr["side"],
        "signal_ts": tr["signal_ts"], "bar_ts": int(bar_ts), "event_ts": int(bar_ts) + tf_ms,
        "price": float(px), "frac": frac, "r_part": r_part, "r_total": tr["realized_r"],
        "fill": tr["fill"], "stop_init": tr["stop"], "tp1": tr["target"], "risk": tr["risk"],
    }
    if extra:
        ev.update(extra)
    return ev


# ═══════════════════════════════════════════════════════════════
# REPLAY
# ═══════════════════════════════════════════════════════════════

def replay(symbol, bars, ds, cfg, tf_ms):
    """bars = (ts, o, h, l, c, v) de velas CERRADAS. Devuelve {'events': [...], 'open_trade': dict|None}."""
    ts, o, h, l, c, v = bars
    n = len(ts)
    out = {"events": [], "open_trade": None}
    if n < 80:
        return out
    events = out["events"]
    days = ts // DAY_MS

    e21 = ema(c, 21)
    atrv = atr(h, l, c, 14)
    hh10 = highest_prev(h, 10)
    hlc3 = (h + l + c) / 3.0
    vwap = np.empty(n)
    cpv = cv = 0.0
    pday = -1
    for i in range(n):
        if days[i] != pday:
            cpv = cv = 0.0
            pday = days[i]
        cpv += hlc3[i] * v[i]
        cv += v[i]
        vwap[i] = cpv / cv if cv > 0 else hlc3[i]
    out["series"] = (ts, o, h, l, c, e21, vwap)

    first_day = int(days[-1]) - cfg.replay_days + 1
    i0 = None
    for i in range(40, n):
        if days[i] != days[i - 1] and days[i] >= first_day:
            i0 = i
            break
    if i0 is None:
        return out

    ctx = EMPTY_CTX
    day_open = day_high = day_low = nan
    day_vol = 0.0
    or_high = or_low = nan
    prev_or_high = prev_or_low = nan
    trades_today = 0
    ep_armed = False
    pend_entry = None
    pend_close = False
    tr = None

    for i in range(i0, n):
        day = int(days[i])
        new_day = (i == i0) or (days[i] != days[i - 1])
        if new_day:
            ctx = ctx_for_day(ds, day) or EMPTY_CTX

        # ── 1) Órdenes pendientes que se ejecutan al OPEN de esta vela ──
        if tr is not None and pend_close:
            tr = None
            pend_close = False
        if pend_entry is not None:
            tr = dict(pend_entry, fill=float(o[i]), stop_cur=pend_entry["stop"], remaining=1.0,
                      partial_taken=False, be_set=False, realized_r=0.0)
            pend_entry = None

        # ── 2) Stops / objetivo durante la vela (si ambos tocan: se asume stop primero = conservador) ──
        if tr is not None:
            s = tr["side"]
            hit_stop = (l[i] <= tr["stop_cur"]) if s == 1 else (h[i] >= tr["stop_cur"])
            if hit_stop:
                px = min(tr["stop_cur"], o[i]) if s == 1 else max(tr["stop_cur"], o[i])
                kind = "BE_STOP" if tr["be_set"] else "STOP"
                events.append(_exit_event(symbol, tr, kind, i, ts[i], px, tr["remaining"], tf_ms))
                tr = None
            elif not tr["partial_taken"] and ((h[i] >= tr["target"]) if s == 1 else (l[i] <= tr["target"])):
                px = max(tr["target"], o[i]) if s == 1 else min(tr["target"], o[i])
                frac = cfg.partial_pct / 100.0
                tr["remaining"] = 1.0 - frac
                tr["partial_taken"] = True
                events.append(_exit_event(symbol, tr, "TP1", i, ts[i], px, frac, tf_ms,
                                          {"remaining": tr["remaining"]}))

        # ── 3) Cálculo al CIERRE de la vela (barstate.isconfirmed) ──
        mins_from_day = (ts[i] - day * DAY_MS) / 60000.0
        mins_from_open = mins_from_day - cfg.sess_offset
        if new_day:
            day_open, day_high, day_low, day_vol = float(o[i]), float(h[i]), float(l[i]), float(v[i])
            or_high = or_low = nan
            trades_today = 0
            ep_armed = False
        else:
            day_high = max(day_high, float(h[i]))
            day_low = min(day_low, float(l[i]))
            day_vol += float(v[i])

        in_or = 0 <= mins_from_open < cfg.or_minutes
        or_done = mins_from_open >= cfg.or_minutes
        if in_or:
            or_high = float(h[i]) if isnan(or_high) else max(or_high, float(h[i]))
            or_low = float(l[i]) if isnan(or_low) else min(or_low, float(l[i]))

        cl, cl1 = float(c[i]), float(c[i - 1])
        in_window = mins_from_open >= cfg.skip_first_min and (cfg.window_min == 0 or mins_from_open <= cfg.window_min)

        x_orh = (not isnan(or_high)) and (not isnan(prev_or_high)) and cl > or_high and cl1 <= prev_or_high
        x_orl = (not isnan(or_low)) and (not isnan(prev_or_low)) and cl < or_low and cl1 >= prev_or_low
        x_hh = (not isnan(hh10[i])) and (not isnan(hh10[i - 1])) and cl > hh10[i] and cl1 <= hh10[i - 1]
        vwap_up = (cl > e21[i] and cl > vwap[i] and cl1 <= max(e21[i - 1], vwap[i - 1]))
        first_red = c[i] < o[i] and c[i - 1] >= o[i - 1]
        vwap_fail = cl < vwap[i] and cl1 >= vwap[i] and h[i] >= vwap[i]

        pc = ctx["prior_close"]
        adr = ctx["adr"]
        chg = (cl - pc) / pc * 100.0 if pc > 0 else nan
        gap = (day_open - pc) / pc * 100.0 if pc > 0 else nan
        move = max(0.0 if isnan(chg) else chg, 0.0 if isnan(gap) else gap)
        frac_day = min(max(mins_from_day / cfg.day_len, 0.05), 1.0)
        rvol = day_vol / (ctx["vavg"] * frac_day) if ctx["vavg"] > 0 else nan

        ep_context = move >= cfg.ep_min_change and adr >= cfg.ep_min_adr and rvol >= cfg.ep_min_rvol
        ep_just_armed = ep_context and not ep_armed
        if ep_context:
            ep_armed = True

        ep_trig = None
        if cfg.ep_trigger == "ORH" and or_done and (x_orh or (ep_just_armed and cl > or_high)):
            ep_trig = "ORH · ruptura del máximo del Opening Range"
        elif cfg.ep_trigger == "HH" and x_hh:
            ep_trig = "HH · ruptura del máximo de las últimas 10 velas"
        elif cfg.ep_trigger == "VWAP_EMA" and vwap_up:
            ep_trig = "VWAP/EMA · recuperación de VWAP y EMA21"

        bl, e20 = ctx["base_low"], ctx["e20"]
        gain = (cl - bl) / bl * 100.0 if bl > 0 else nan
        ext = (cl - e20) / e20 * 100.0 if e20 > 0 else nan
        ps_context = gain >= cfg.ps_min_gain and ctx["gs"] >= cfg.ps_min_green and ext >= cfg.ps_min_ext

        ps_trig = None
        if cfg.ps_trigger == "ORL" and or_done and x_orl:
            ps_trig = "ORL · ruptura del mínimo del Opening Range"
        elif cfg.ps_trigger == "FIRST_RED" and first_red:
            ps_trig = "First Red · primera vela roja tras vela verde"
        elif cfg.ps_trigger == "VWAP_FAIL" and vwap_fail:
            ps_trig = "VWAP Fail · pérdida de VWAP"

        can_trade = tr is None and pend_entry is None and trades_today < cfg.max_trades_day and in_window
        ep_signal = can_trade and cfg.ep_on and ep_armed and ep_trig is not None
        ps_signal = can_trade and cfg.ps_on and ps_context and ps_trig is not None and not ep_signal

        levels = {"or_high": or_high, "or_low": or_low, "vwap": float(vwap[i]), "ema21": float(e21[i]),
                  "e10": ctx["e10"], "e20": ctx["e20"], "day_high": day_high, "day_low": day_low}

        if ep_signal or ps_signal:
            if ep_signal:
                side, setup, trig = 1, "EP Long", ep_trig
                ref = day_low if cfg.stop_mode == "DAY" else or_low if cfg.stop_mode == "OR" else cl - 2 * atrv[i]
                risk_pct, adr_lim = cfg.risk_pct_ep, cfg.max_adr_mult
                extra = {"move": move, "chg": chg, "gap": gap, "rvol": rvol, "adr": adr}
            else:
                side, setup, trig = -1, "Parabolic Short", ps_trig
                ref = day_high if cfg.stop_mode == "DAY" else or_high if cfg.stop_mode == "OR" else cl + 2 * atrv[i]
                risk_pct, adr_lim = cfg.risk_pct_short, cfg.max_adr_mult * 1.2
                extra = {"gain": gain, "gs": ctx["gs"], "ext": ext, "adr": adr, "rvol": rvol}
            built = _build_entry(cfg, symbol, side, setup, i, ts[i], tf_ms, cl, float(atrv[i]), ref, adr,
                                 risk_pct, adr_lim, trig, extra, levels, trades_today + 1)
            if built is not None:
                pend_entry, ev = built
                trades_today += 1
                events.append(ev)

        # ── 4) Gestión de la posición al cierre (inTrade) ──
        if tr is not None:
            if tr["partial_taken"] and not tr["be_set"]:
                tr["stop_cur"] = tr["fill"]  # stop a break-even desde la vela siguiente
                tr["be_set"] = True
            if tr["partial_taken"] and cfg.use_trail_ema and new_day and not pend_close:
                trail = ctx["e10"] if cfg.trail_ema_len == 10 else ctx["e20"]
                if not isnan(trail) and not isnan(pc):
                    bad = (pc < trail) if tr["side"] == 1 else (pc > trail)
                    if bad:
                        events.append(_exit_event(symbol, tr, "TRAIL", i, ts[i], cl, tr["remaining"], tf_ms,
                                                  {"trail_ema": trail, "prior_close": pc,
                                                   "ema_len": cfg.trail_ema_len}))
                        pend_close = True
            if cfg.max_hold_bars > 0 and i - tr["signal_i"] >= cfg.max_hold_bars and not pend_close:
                events.append(_exit_event(symbol, tr, "TIME", i, ts[i], cl, tr["remaining"], tf_ms))
                pend_close = True

        if i == n - 1:
            out["diag"] = {
                "ts": int(ts[i]), "close": cl, "prior_close": pc, "move": move, "chg": chg, "gap": gap,
                "rvol": rvol, "adr": adr, "ep_armed": bool(ep_armed), "ep_context": bool(ep_context),
                "ep_trig": ep_trig is not None, "gain": gain, "gs": ctx["gs"], "ext": ext,
                "ps_context": bool(ps_context), "ps_trig": ps_trig is not None, "in_window": bool(in_window),
                "or_done": bool(or_done), "or_high": or_high, "or_low": or_low, "vwap": float(vwap[i]),
                "ema21": float(e21[i]), "e10": ctx["e10"], "e20": ctx["e20"], "day_high": day_high,
                "day_low": day_low, "trades_today": trades_today, "in_trade": tr is not None,
            }

        prev_or_high, prev_or_low = or_high, or_low

    # Posición virtual abierta (o señal en la última vela pendiente de ejecutarse)
    if tr is not None and not pend_close:
        out["open_trade"] = {"symbol": symbol, "setup": tr["setup"], "side": tr["side"], "fill": tr["fill"],
                             "stop": tr["stop_cur"], "tp1": tr["target"], "partial_taken": tr["partial_taken"],
                             "signal_ts": tr["signal_ts"], "pending": False, "risk": tr["risk"],
                             "stop_init": tr["stop"], "realized_r": tr["realized_r"], "remaining": tr["remaining"]}
    elif pend_entry is not None:
        out["open_trade"] = {"symbol": symbol, "setup": pend_entry["setup"], "side": pend_entry["side"],
                             "fill": float(c[-1]), "stop": pend_entry["stop"], "tp1": pend_entry["target"],
                             "partial_taken": False, "signal_ts": pend_entry["signal_ts"], "pending": True,
                             "risk": pend_entry["risk"], "stop_init": pend_entry["stop"], "realized_r": 0.0,
                             "remaining": 1.0}
    return out
