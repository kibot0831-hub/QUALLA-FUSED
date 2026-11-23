"""telegram_bot.py — Envío a Telegram + formato de todos los mensajes (HTML)."""
from __future__ import annotations

import html
import logging
import math
import re
import time
from datetime import datetime, timezone

import requests

from strategy import DAY_MS

log = logging.getLogger("qfused.telegram")
esc = html.escape


# ═══════════════════════════ Formato de números ═══════════════════════════
def fmt_price(p) -> str:
    """Precio SIN notación científica y con decimales adaptados a la magnitud."""
    if p is None:
        return "—"
    if p == 0:
        return "0"
    dec = min(12, max(2, 4 - math.floor(math.log10(abs(p)))))
    return f"{p:.{dec}f}"


def fmt_qty(q, prec=None) -> str:
    if q is None:
        return "—"
    if prec is not None:
        f = 10 ** int(prec)
        return f"{math.floor(q * f) / f:.{int(prec)}f}"
    if q >= 100:
        return f"{q:.0f}"
    if q >= 1:
        return f"{q:.2f}"
    return f"{q:.4f}"


def pct(x, d=1) -> str:
    return "—" if x is None else f"{x:+.{d}f}%"


def num(x, d=1, suffix="") -> str:
    return "—" if x is None else f"{x:.{d}f}{suffix}"


def hhmm(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M")


def ymd(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _links(sym: str) -> str:
    tv = esc(sym.replace("-", "")) + ".P"
    return (f'<a href="https://www.tradingview.com/chart/?symbol=BINGX:{tv}">TradingView</a>'
            f' · <a href="https://bingx.com/en/perpetual/{esc(sym)}/">BingX</a>')


def _trail_text(cfg, side, c) -> str:
    if not cfg.use_trail_ema:
        return "sin trail automático (gestiona el resto a mano)"
    n = cfg.trail_ema_len
    ema = c.get("ema10d") if n == 10 else c.get("ema20d")
    op = "&lt;" if side == "long" else "&gt;"
    return f"salir si el cierre diario previo {op} EMA{n} diaria (hoy {fmt_price(ema)}); se evalúa a las 00:00 UTC"


def _trail_short(cfg, side) -> str:
    if not cfg.use_trail_ema:
        return "gestión manual"
    op = "&lt;" if side == "long" else "&gt;"
    return f"sale si el cierre diario previo {op} EMA{cfg.trail_ema_len} diaria (00:00 UTC)"


# ═══════════════════════════ Mensajes ═══════════════════════════
def format_signal(sig, cfg, info=None) -> str:
    info = info or {}
    long = sig.side == "long"
    c, sz = sig.ctx, sig.size
    icon, title = ("🟢", "EP LONG") if long else ("🔴", "PARABOLIC SHORT")
    base = sig.symbol.rsplit("-", 1)[0]
    stop_pct = -sig.stop_pct if long else sig.stop_pct
    tp_pct = sig.tp_pct if long else -sig.tp_pct
    adr_ratio = sig.stop_pct / c["adr"] if c.get("adr") else None

    L = [f"{icon} <b>{title}</b> · <code>{esc(sig.symbol)}</code>",
         f"⏱ {cfg.timeframe} · vela cerrada {hhmm(sig.ts + cfg.tf_min * 60_000)} UTC · "
         f"trade {sig.trade_n}/{cfg.max_trades_day} del día",
         f"🎯 Gatillo: {esc(sig.trigger)}",
         "",
         "<b>Niveles</b>",
         f"Entrada ≈ <code>{fmt_price(sig.entry)}</code> (mercado)",
         f"Stop: <code>{fmt_price(sig.stop)}</code> ({pct(stop_pct, 2)})",
         f"TP1: <code>{fmt_price(sig.target)}</code> ({pct(tp_pct, 2)} · {cfg.partial_r:g}R) → cierra {cfg.partial_pct:g}%",
         f"Tras TP1: stop del resto a BE (<code>{fmt_price(sig.entry)}</code>); trail: {_trail_text(cfg, sig.side, c)}",
         "",
         f"<b>Tamaño sugerido</b> (cuenta ref. {sz['equity']:.0f} USDT)",
         f"Riesgo {sz['risk_pct']:g}% = {sz['risk_cash']:.2f} USDT → cantidad "
         f"<code>{fmt_qty(sz['qty'], info.get('qty_prec'))}</code> {esc(base)}",
         f"Nocional ≈ {sz['notional']:.2f} USDT ({sz['notional_pct']:.1f}% de la cuenta)"
         + (" · limitado por el tope de posición" if sz["capped"] else ""),
         f"Riesgo real ≈ {sz['real_risk']:.2f} USDT"]
    min_q = info.get("min_qty")
    if min_q and sz["qty"] < min_q:
        L.append(f"⚠️ Cantidad por debajo del mínimo del par ({min_q:g}). Sube ACCOUNT_EQUITY o descarta.")
    L.append("")

    if long:
        L += ["<b>Contexto EP</b>",
              f"Cambio vs cierre previo {pct(c['chg'])} · gap {pct(c['gap'])}",
              f"RVOL {num(c['rvol'], 1, 'x')} · ADR20 {num(c['adr'], 1, '%')}"
              + (f" · stop = {adr_ratio:.2f} ADR" if adr_ratio else "")]
    else:
        L += ["<b>Contexto Parabolic</b>",
              f"Subida {pct(c['gain'], 0)} desde el mínimo de {c['lookback']}d · {c['green']} días verdes seguidos",
              f"Extensión {pct(c['ext'], 0)} sobre EMA20 diaria (<code>{fmt_price(c['ema20d'])}</code>)",
              f"RVOL {num(c['rvol'], 1, 'x')} · ADR20 {num(c['adr'], 1, '%')}"
              + (f" · stop = {adr_ratio:.2f} ADR" if adr_ratio else "")]
    L += [f"OR: {fmt_price(c['or_l'])} – {fmt_price(c['or_h'])} · VWAP {fmt_price(c['vwap'])} · EMA21 {fmt_price(c['ema21'])}",
          f"Día: máx {fmt_price(c['day_high'])} / mín {fmt_price(c['day_low'])} · cierre previo {fmt_price(c['prior_close'])}",
          "",
          _links(sig.symbol)]
    return "\n".join(L)


def format_event(pos, ev, cfg) -> str:
    sym = esc(pos.symbol)
    side = "LONG" if pos.side == "long" else "SHORT"
    head = f"<code>{sym}</code> · {esc(pos.setup)}"
    r = ev.total_r
    if ev.kind == "TP1":
        body = (f"🎯 <b>TP1 alcanzado</b> · {head}\n"
                f"Cierra {cfg.partial_pct:g}% en <code>{fmt_price(ev.price)}</code> → {r:+.2f}R realizado.\n"
                f"👉 Mueve el stop del resto a BE <code>{fmt_price(pos.entry)}</code>.\n"
                f"Resto: {_trail_short(cfg, pos.side)}")
    elif ev.kind == "STOP":
        body = (f"🛑 <b>Stop alcanzado</b> · {head}\n"
                f"Salida ≈ <code>{fmt_price(ev.price)}</code> → resultado {r:+.2f}R")
    elif ev.kind == "BE":
        body = (f"⚪ <b>Cierre en breakeven</b> · {head}\n"
                f"Resto cerrado ≈ <code>{fmt_price(ev.price)}</code> → resultado final {r:+.2f}R")
    elif ev.kind == "TRAIL":
        n = cfg.trail_ema_len
        body = (f"🔄 <b>Salida por trail EMA{n} diaria</b> · {head}\n"
                f"Cierra el resto a mercado ≈ <code>{fmt_price(ev.price)}</code> → resultado final {r:+.2f}R")
    else:
        body = (f"⏳ <b>Salida por tiempo</b> ({cfg.max_hold_bars} velas) · {head}\n"
                f"Cierra a mercado ≈ <code>{fmt_price(ev.price)}</code> → resultado final {r:+.2f}R")
    return f"{body}\n{side} · {hhmm(ev.ts + cfg.tf_min * 60_000)} UTC\n{_links(pos.symbol)}"


def _ep_trigger_text(cfg, it) -> str:
    if cfg.ep_trigger == "ORH":
        return f"cierre &gt; ORH <code>{fmt_price(it.get('or_h'))}</code>"
    if cfg.ep_trigger == "HH":
        return "cierre &gt; máx. de 10 velas"
    return "cierre recupera VWAP + EMA21"


def _ps_trigger_text(cfg, it) -> str:
    if cfg.ps_trigger == "ORL":
        return f"cierre &lt; ORL <code>{fmt_price(it.get('or_l'))}</code>"
    if cfg.ps_trigger == "FIRST_RED":
        return "primera vela roja tras verde"
    return "rechazo bajista en VWAP"


def format_radar(ep_items, ps_items, cfg, ts, max_items) -> str:
    L = [f"👀 <b>RADAR</b> · {cfg.timeframe} · {hhmm(ts)} UTC"]
    if ep_items:
        L += ["", "🟢 <b>EP armado</b> — esperando gatillo"]
        for it in ep_items[:max_items]:
            L.append(f"• <code>{esc(it['sym'])}</code> {pct(it['move'])} · RVOL {num(it['rvol'], 1, 'x')} · "
                     f"ADR {num(it['adr'], 1, '%')} → {_ep_trigger_text(cfg, it)}"
                     + (f" · SL≈<code>{fmt_price(it['stop_est'])}</code>" if it.get("stop_est") else ""))
        if len(ep_items) > max_items:
            L.append(f"… y {len(ep_items) - max_items} más")
    if ps_items:
        L += ["", "🔴 <b>Parabolic activo</b> — esperando gatillo"]
        for it in ps_items[:max_items]:
            L.append(f"• <code>{esc(it['sym'])}</code> {pct(it['gain'], 0)} ({it['lookback']}d) · {it['green']} verdes · "
                     f"{pct(it['ext'], 0)} s/EMA20d → {_ps_trigger_text(cfg, it)}")
        if len(ps_items) > max_items:
            L.append(f"… y {len(ps_items) - max_items} más")
    return "\n".join(L)


def format_summary(state, start_ms, end_ms) -> str | None:
    entered = [t for t in state.closed if start_ms <= t["entry_ts"] < end_ms]
    entered += [{"setup": p.setup, "symbol": p.symbol, "r": None}
                for p in state.positions.values() if start_ms <= p.entry_ts < end_ms]
    closed_today = [t for t in state.closed if start_ms <= t["close_ts"] < end_ms]
    if not entered and not closed_today:
        return None
    ep = sum(1 for t in entered if t["setup"] == "EP Long")
    ps = len(entered) - ep
    wins = sum(1 for t in closed_today if t["r"] > 0)
    total_r = sum(t["r"] for t in closed_today)
    L = [f"📊 <b>Resumen {ymd(start_ms)} (UTC)</b>",
         f"Señales nuevas: {len(entered)} (EP {ep} · PS {ps})",
         f"Cerradas: {len(closed_today)} → {wins} ganadoras / {len(closed_today) - wins} no · R total {total_r:+.2f}R",
         f"Abiertas ahora: {len(state.positions)}"]
    for t in closed_today[-12:]:
        L.append(f"• <code>{esc(t['symbol'])}</code> {esc(t['setup'])}: {t['r']:+.2f}R ({esc(t['reason'])})")
    return "\n".join(L)


def format_startup(cfg, n_symbols, note="") -> str:
    setups = {"BOTH": "EP Long + Parabolic Short", "EP": "solo EP Long", "PS": "solo Parabolic Short"}[cfg.setups]
    L = ["🤖 <b>Qullamaggie Fused v2</b> — bot iniciado",
         f"BingX perpetuos USDT · {cfg.timeframe} · {setups}",
         f"Monedas a analizar: <b>{n_symbols}</b> (liquidez 24h ≥ {cfg.min_quote_vol:,.0f} USDT)",
         f"EP: cambio ≥ {cfg.ep_min_change:g}% · ADR ≥ {cfg.ep_min_adr:g}% · RVOL ≥ {cfg.ep_min_rvol:g}x · gatillo {cfg.ep_trigger}",
         f"PS: subida ≥ {cfg.ps_min_gain:g}% / {cfg.ps_lookback}d · ≥ {cfg.ps_min_green} verdes · ext ≥ {cfg.ps_min_ext:g}% EMA20d · gatillo {cfg.ps_trigger}",
         f"Riesgo: EP {cfg.risk_pct_ep:g}% · PS {cfg.risk_pct_short:g}% · cuenta ref. {cfg.account_equity:.0f} USDT · máx {cfg.max_trades_day} trades/día/moneda",
         "Solo señales: el bot NO opera."]
    if note:
        L.append(note)
    return "\n".join(L)


# ═══════════════════════════ Envío ═══════════════════════════
def _split(text: str, limit: int = 3900):
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self.session = requests.Session()
        if not self.enabled:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID sin configurar: los mensajes solo irán al log")

    def send(self, text: str, silent: bool = False) -> bool:
        if not self.enabled:
            log.info("[TG desactivado]\n%s", re.sub(r"<[^>]+>", "", html.unescape(text)))
            return False
        ok = True
        for chunk in _split(text):
            ok = self._post(chunk, silent) and ok
            time.sleep(0.06)
        return ok

    def _post(self, text: str, silent: bool) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        for attempt in range(4):
            try:
                r = self.session.post(url, json=payload, timeout=15)
            except requests.RequestException as e:
                # no se imprime la excepción completa: contiene la URL con el token
                log.warning("Telegram: error de red (%s), reintento", type(e).__name__)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    wait = 3
                time.sleep(wait + 1)
                continue
            if r.status_code == 400 and "parse" in r.text.lower() and "parse_mode" in payload:
                payload.pop("parse_mode")
                payload["text"] = re.sub(r"<[^>]+>", "", html.unescape(text))
                continue
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return False
