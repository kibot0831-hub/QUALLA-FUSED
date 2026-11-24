"""Construcción de los mensajes detallados de Telegram (HTML)."""
import html
import math
from datetime import datetime, timezone

esc = html.escape


# ───────────── helpers de formato ─────────────

def _nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def p(x):
    """Precio con 5 cifras significativas (mín. 2 decimales)."""
    if _nan(x):
        return "—"
    if x == 0:
        return "0"
    dec = min(10, max(2, 4 - math.floor(math.log10(abs(x)))))
    return f"{x:.{dec}f}"


def pct(x, signed=True):
    if _nan(x):
        return "—"
    return f"{x:+.2f}%" if signed else f"{x:.2f}%"


def qty_fmt(q):
    if q >= 1000:
        return f"{q:,.0f}"
    if q >= 1:
        return f"{q:.2f}"
    return f"{q:.4f}"


def ts_fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def base_of(symbol):
    return symbol.split("/")[0]


def links(symbol):
    b = base_of(symbol)
    return (f'<a href="https://bingx.com/en/perpetual/{b}-USDT/">BingX</a> · '
            f'<a href="https://www.tradingview.com/chart/?symbol=BINGX:{b}USDT.P">TradingView</a>')


def _stop_label(cfg, long_):
    if cfg.stop_mode == "OR":
        ref = "mín. del OR" if long_ else "máx. del OR"
    elif cfg.stop_mode == "ATR":
        return "2×ATR"
    else:
        ref = "mín. del día" if long_ else "máx. del día"
    return f"{ref} {'−' if long_ else '+'} colchón {cfg.sl_buf}×ATR"


# ───────────── mensajes ─────────────

def entry_message(ev, cfg, now_ms):
    long_ = ev["side"] == 1
    icon = "🟢" if long_ else "🔴"
    name = "EP LONG" if long_ else "PARABOLIC SHORT"
    base = base_of(ev["symbol"])
    age = max(0.0, (now_ms - ev["event_ts"]) / 60000.0)
    tp_pct = abs(ev["tp1"] - ev["entry"]) / ev["entry"] * 100.0
    sign_stop = "-" if long_ else "+"
    c = ev["ctx"]
    lv = ev["levels"]

    L = [f"{icon} <b>{name}</b> — <b>{esc(base)}/USDT</b>  <i>(perp BingX)</i>",
         f"🕒 Vela {ts_fmt(ev['bar_ts'])} · TF {esc(cfg.timeframe)}"
         + (f" · hace {age:.0f} min" if age >= 2 else ""),
         "",
         "<b>📌 Operación</b>",
         f"• {'Compra' if long_ else 'Venta'} a mercado ≈ <code>{p(ev['entry'])}</code> (cierre de la vela)",
         f"• Stop: <code>{p(ev['stop'])}</code> ({sign_stop}{ev['stop_pct']:.2f}%) — {esc(_stop_label(cfg, long_))}",
         f"• TP1 (+{ev['partial_r']:g}R): <code>{p(ev['tp1'])}</code> ({'+' if long_ else '-'}{tp_pct:.2f}%) → cerrar {ev['partial_pct']:g}%",
         "• Tras TP1: stop del resto a break-even (tu precio de entrada)"]
    if cfg.use_trail_ema:
        side_txt = "por debajo" if long_ else "por encima"
        L.append(f"• Trail: cerrar el resto si el cierre diario queda {side_txt} de la EMA{cfg.trail_ema_len} diaria "
                 f"(se evalúa al abrir cada día UTC)")
    if cfg.max_hold_bars > 0:
        L.append(f"• Salida por tiempo: {cfg.max_hold_bars} velas")

    L += ["",
          f"<b>💰 Riesgo y tamaño</b> <i>(equity configurado ${cfg.account_equity:,.0f})</i>",
          f"• Riesgo: {ev['risk_pct']:g}% = ${ev['risk_cash']:,.2f}",
          f"• Tamaño: {qty_fmt(ev['qty'])} {esc(base)} · nocional ${ev['notional']:,.0f} "
          f"({ev['notional'] / cfg.account_equity * 100:.1f}% del equity)"
          + (" · <i>limitado por el tope de posición</i>" if ev["capped"] else ""),
          f"• Apalancamiento mínimo: {ev['notional'] / cfg.account_equity:.2f}x",
          f"• Stop = {ev['adr_mult']:.2f}× ADR (máx. {cfg.max_adr_mult * (1 if long_ else 1.2):.2f}×)",
          "",
          "<b>🔎 Contexto</b>"]
    if long_:
        L += [f"• Movimiento vs cierre de ayer: {pct(c['move'])} (cambio {pct(c['chg'])} · gap {pct(c['gap'])})",
              f"• RVOL a esta hora: {c['rvol']:.2f}x (mín. {cfg.ep_min_rvol:g}x)" if not _nan(c["rvol"]) else "• RVOL: —",
              f"• ADR 20d: {pct(c['adr'], False)} (mín. {cfg.ep_min_adr:g}%)"]
    else:
        L += [f"• Subida desde el mínimo de {cfg.ps_lookback}d: {pct(c['gain'])} (mín. {cfg.ps_min_gain:g}%)",
              f"• Días verdes seguidos: {c['gs']:.0f} (mín. {cfg.ps_min_green})",
              f"• Extensión sobre EMA20 diaria: {pct(c['ext'])} (mín. {cfg.ps_min_ext:g}%)",
              f"• ADR 20d: {pct(c['adr'], False)}"]
    L += [f"• Gatillo: {esc(ev['trigger'])}",
          "",
          "<b>📊 Niveles</b>",
          f"• OR: <code>{p(lv['or_low'])}</code> – <code>{p(lv['or_high'])}</code> · Día: <code>{p(lv['day_low'])}</code> – <code>{p(lv['day_high'])}</code>",
          f"• VWAP <code>{p(lv['vwap'])}</code> · EMA21 <code>{p(lv['ema21'])}</code> · EMA10d <code>{p(lv['e10'])}</code> · EMA20d <code>{p(lv['e20'])}</code>",
          "",
          f"🔢 Trade {ev['trades_today']}/{cfg.max_trades_day} de hoy en este par",
          f"🔗 {links(ev['symbol'])}",
          "<i>Coloca el stop y el TP1 en BingX nada más entrar. Ajusta el tamaño al paso mínimo del contrato. "
          "Señal informativa, no es asesoramiento financiero.</i>"]
    return "\n".join(L)


def exit_message(ev, cfg, now_ms):
    t = ev["type"]
    long_ = ev["side"] == 1
    name = "EP LONG" if long_ else "PARABOLIC SHORT"
    base = base_of(ev["symbol"])
    titles = {"TP1": "🎯 TP1 alcanzado", "STOP": "🛑 Stop alcanzado", "BE_STOP": "⚖️ Stop a break-even",
              "TRAIL": "🔔 Trail EMA · cerrar el resto", "TIME": "⏱ Salida por tiempo · cerrar el resto"}
    live = " ⚡" if ev.get("live") else ""
    L = [f"{titles.get(t, t)}{live} — <b>{esc(base)}/USDT</b>  <i>({name})</i>",
         f"🕒 Vela {ts_fmt(ev['bar_ts'])} · TF {esc(cfg.timeframe)}"
         + (" · <i>aviso intravela (precio en vivo), se confirma al cierre</i>" if ev.get("live") else ""), ""]
    if t == "TP1":
        L += [f"• Parcial: cerrar {ev['frac'] * 100:g}% en <code>{p(ev['price'])}</code> ({ev['r_part']:+.2f}R en esa parte)",
              f"• Mueve el stop del resto a break-even: <code>{p(ev['fill'])}</code> (tu precio de entrada)",
              f"• Queda el {ev['remaining'] * 100:g}% corriendo con trail EMA{cfg.trail_ema_len}d" if cfg.use_trail_ema
              else f"• Queda el {ev['remaining'] * 100:g}% corriendo"]
    elif t in ("STOP", "BE_STOP"):
        L += [f"• Salida en <code>{p(ev['price'])}</code> ({ev['r_part']:+.2f}R sobre {ev['frac'] * 100:g}% de la posición)"]
    elif t == "TRAIL":
        cmp_txt = "por debajo" if long_ else "por encima"
        L += [f"• Cierre diario de ayer <code>{p(ev['prior_close'])}</code> {cmp_txt} de la EMA{ev['ema_len']}d <code>{p(ev['trail_ema'])}</code>",
              f"• <b>Cierra el {ev['frac'] * 100:g}% restante a mercado ahora</b> (≈ <code>{p(ev['price'])}</code>, {ev['r_part']:+.2f}R)"]
    elif t == "TIME":
        L += [f"• Máximo de velas en posición alcanzado: <b>cierra el {ev['frac'] * 100:g}% restante a mercado</b> "
              f"(≈ <code>{p(ev['price'])}</code>, {ev['r_part']:+.2f}R)"]
    if t != "TP1":
        L.append(f"• <b>Resultado total del trade ≈ {ev['r_total']:+.2f}R</b>")
    else:
        L.append(f"• R realizado hasta ahora ≈ {ev['r_total']:+.2f}R")
    L += ["",
          f"Entrada ≈ <code>{p(ev['fill'])}</code> · Stop inicial <code>{p(ev['stop_init'])}</code> · TP1 <code>{p(ev['tp1'])}</code>",
          f"Señal original: {ts_fmt(ev['signal_ts'])}",
          f"🔗 {links(ev['symbol'])}"]
    return "\n".join(L)


def format_event(ev, cfg, now_ms):
    return entry_message(ev, cfg, now_ms) if ev["type"] == "ENTRY" else exit_message(ev, cfg, now_ms)


def startup(cfg, n_universe):
    setups = " + ".join(x for x, on in (("EP Long", cfg.ep_on), ("Parabolic Short", cfg.ps_on)) if on) or "ninguno"
    return "\n".join([
        "🤖 <b>Qullamaggie Fused Bot — iniciado</b>",
        f"• Setups: {setups}",
        f"• Universo: {n_universe} perpetuos USDT de BingX (vol. 24h mín. ${cfg.min_quote_volume:,.0f})",
        f"• Timeframe: {esc(cfg.timeframe)} · Replay {cfg.replay_days}d · Máx. {cfg.max_trades_day} trades/día/par",
        f"• EP: ≥{cfg.ep_min_change:g}% · ADR≥{cfg.ep_min_adr:g}% · RVOL≥{cfg.ep_min_rvol:g}x · gatillo {esc(cfg.ep_trigger)}",
        f"• PS: ≥{cfg.ps_min_gain:g}% en {cfg.ps_lookback}d · {cfg.ps_min_green} verdes · ext. EMA20≥{cfg.ps_min_ext:g}% · gatillo {esc(cfg.ps_trigger)}",
        f"• Riesgo EP {cfg.risk_pct_ep:g}% · PS {cfg.risk_pct_short:g}% · parcial {cfg.partial_pct:g}% a +{cfg.partial_r:g}R",
        f"• Gráficos: {'sí' if cfg.send_charts else 'no'} · Alertas stop/TP1 en vivo: "
        + (f"cada {cfg.live_poll_sec}s" if cfg.live_poll_sec > 0 and cfg.send_exit_updates else "no"),
    ])


def heartbeat(stats, opens, cfg, paused=False):
    L = ["⏸ <b>Bot en PAUSA</b> (no se envían señales; /reanudar para activarlo)" if paused else "✅ <b>Bot activo</b>",
         f"• Universo: {stats['universe']} pares · con histórico diario: {stats['daily_ok']}",
         f"• Candidatos en el último ciclo: {stats['cands']} · duración {stats['secs']:.0f}s",
         f"• Hoy (UTC): {stats['entries']} entradas · {stats['exits']} salidas enviadas"]
    if opens:
        L.append(f"• Posiciones virtuales abiertas: {len(opens)}")
        for o in list(opens.values())[:15]:
            side = "LONG" if o["side"] == 1 else "SHORT"
            flag = " (BE)" if o["partial_taken"] else ""
            L.append(f"   – {esc(base_of(o['symbol']))} {side}{flag} · entrada ≈ {p(o['fill'])} · stop {p(o['stop'])} · TP1 {p(o['tp1'])}")
    else:
        L.append("• Posiciones virtuales abiertas: 0")
    return "\n".join(L)


def error_alert(text):
    return f"⚠️ <b>Bot: incidencia</b>\n<code>{esc(text[:600])}</code>"


# ───────────── comandos ─────────────

def help_text():
    return "\n".join([
        "🤖 <b>Comandos</b>",
        "/status — estado del bot",
        "/posiciones — posiciones virtuales abiertas",
        "/watchlist — pares armados (EP) o con contexto Parabolic Short",
        "/analiza BTC — diagnóstico de un par (qué condiciones cumple)",
        "/config — parámetros activos",
        "/pausa · /reanudar — silenciar / reactivar las señales",
    ])


def open_positions(opens):
    if not opens:
        return "📭 No hay posiciones virtuales abiertas."
    L = [f"📂 <b>Posiciones virtuales abiertas: {len(opens)}</b>"]
    for o in list(opens.values())[:25]:
        side = "LONG" if o["side"] == 1 else "SHORT"
        state = "pendiente de ejecutar" if o.get("pending") else ("TP1 hecho · stop en BE" if o["partial_taken"] else "sin TP1")
        L.append(f"• <b>{esc(base_of(o['symbol']))}</b> {side} · {esc(o['setup'])} · entrada ≈ <code>{p(o['fill'])}</code> · "
                 f"stop <code>{p(o['stop'])}</code> · TP1 <code>{p(o['tp1'])}</code> · {state} · desde {ts_fmt(o['signal_ts'])}")
    return "\n".join(L)


def watchlist(diags, cfg):
    ep = sorted(((s, d) for s, d in diags.items() if cfg.ep_on and d["ep_armed"]),
                key=lambda x: -(x[1]["rvol"] if not _nan(x[1]["rvol"]) else 0))
    ps = sorted(((s, d) for s, d in diags.items() if cfg.ps_on and d["ps_context"]),
                key=lambda x: -(x[1]["gain"] if not _nan(x[1]["gain"]) else 0))
    if not ep and not ps:
        return "👀 Watchlist vacía: ningún par cumple ahora el contexto EP ni Parabolic Short."
    L = ["👀 <b>Watchlist</b> (candidatos del último ciclo)"]
    if ep:
        L += ["", f"🟢 <b>EP armados ({len(ep)})</b> — esperando gatillo {esc(cfg.ep_trigger)}"]
        for s, d in ep[:12]:
            dist = ""
            if _nan(d["or_high"]) or not d["or_done"]:
                dist = " · OR sin completar"
            else:
                dist = f" · ORH <code>{p(d['or_high'])}</code> ({(d['or_high'] - d['close']) / d['close'] * 100:+.2f}% desde el precio)"
            rv = f"{d['rvol']:.1f}x" if not _nan(d["rvol"]) else "—"
            L.append(f"• <b>{esc(base_of(s))}</b> {pct(d['move'])} · RVOL {rv}{dist}")
    if ps:
        L += ["", f"🔴 <b>Parabolic Short en contexto ({len(ps)})</b> — esperando gatillo {esc(cfg.ps_trigger)}"]
        for s, d in ps[:12]:
            dist = ""
            if not _nan(d["or_low"]) and d["or_done"]:
                dist = f" · ORL <code>{p(d['or_low'])}</code> ({(d['or_low'] - d['close']) / d['close'] * 100:+.2f}% desde el precio)"
            L.append(f"• <b>{esc(base_of(s))}</b> subida {pct(d['gain'])} · verdes {d['gs']:.0f} · ext. EMA20 {pct(d['ext'])}{dist}")
    return "\n".join(L)


def _chk(b):
    return "✅" if b else "❌"


def _num(x, fmt="{:.2f}"):
    return "—" if _nan(x) else fmt.format(x)


def analysis(symbol, d, cfg):
    base = base_of(symbol)
    L = [f"🔬 <b>{esc(base)}/USDT</b> · {esc(cfg.timeframe)} · vela {ts_fmt(d['ts'])}",
         f"Precio <code>{p(d['close'])}</code> · cierre de ayer <code>{p(d['prior_close'])}</code> · "
         f"VWAP <code>{p(d['vwap'])}</code> · EMA21 <code>{p(d['ema21'])}</code>"]
    if cfg.ep_on:
        L += ["", "<b>🟢 EP Long</b>",
              f"{_chk(d['move'] >= cfg.ep_min_change)} Movimiento ≥ {cfg.ep_min_change:g}%: {pct(d['move'])}",
              f"{_chk(d['adr'] >= cfg.ep_min_adr)} ADR 20d ≥ {cfg.ep_min_adr:g}%: {_num(d['adr'])}%",
              f"{_chk(d['rvol'] >= cfg.ep_min_rvol)} RVOL ≥ {cfg.ep_min_rvol:g}x: {_num(d['rvol'])}x",
              f"{_chk(d['ep_armed'])} Armado hoy · {_chk(d['ep_trig'])} gatillo {esc(cfg.ep_trigger)} en esta vela"]
    if cfg.ps_on:
        L += ["", "<b>🔴 Parabolic Short</b>",
              f"{_chk(d['gain'] >= cfg.ps_min_gain)} Subida desde mín. {cfg.ps_lookback}d ≥ {cfg.ps_min_gain:g}%: {pct(d['gain'])}",
              f"{_chk(d['gs'] >= cfg.ps_min_green)} Días verdes ≥ {cfg.ps_min_green}: {_num(d['gs'], '{:.0f}')}",
              f"{_chk(d['ext'] >= cfg.ps_min_ext)} Extensión sobre EMA20d ≥ {cfg.ps_min_ext:g}%: {pct(d['ext'])}",
              f"{_chk(d['ps_context'])} Contexto completo · {_chk(d['ps_trig'])} gatillo {esc(cfg.ps_trigger)} en esta vela"]
    L += ["",
          f"OR: <code>{p(d['or_low'])}</code> – <code>{p(d['or_high'])}</code> ({'completo' if d['or_done'] else 'en curso'}) · "
          f"Día: <code>{p(d['day_low'])}</code> – <code>{p(d['day_high'])}</code>",
          f"{_chk(d['in_window'])} Ventana de entradas · Trades hoy {d['trades_today']}/{cfg.max_trades_day} · "
          f"{'en posición' if d['in_trade'] else 'sin posición'}"]
    return "\n".join(L)
