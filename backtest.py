"""Backtest con el MISMO motor que usa el bot (strategy.replay). Se ejecuta en tu PC, no en Railway.

Ejemplos:
    python backtest.py --symbols BTC,ETH,SOL --days 14
    python backtest.py --top 30 --days 30 --tf 5m --csv trades.csv

Usa las mismas variables de entorno que el bot (EP_MIN_CHANGE, STOP_MODE, ...), así puedes probar parámetros:
    EP_MIN_RVOL=2 EP_TRIGGER=HH python backtest.py --top 30 --days 30

Resultados en múltiplos de R (riesgo inicial), descontando un coste por operación (--costs-pct).
"""
import argparse
import asyncio
import csv
import logging
import time

import numpy as np

import strategy as S
from bingx import BingX, timeframe_ms
from config import cfg

DAY_MS = S.DAY_MS
FINAL = ("STOP", "BE_STOP", "TRAIL", "TIME")


def build_trades(events):
    """Agrupa los eventos en operaciones (una por señal de entrada)."""
    trades = {}
    for ev in sorted(events, key=lambda e: e["event_ts"]):
        k = (ev["symbol"], ev["signal_ts"])
        if ev["type"] == "ENTRY":
            trades[k] = {"symbol": ev["symbol"], "setup": ev["setup"], "side": ev["side"],
                         "entry_ts": ev["signal_ts"], "stop_pct": ev["stop_pct"], "r": None,
                         "tp1": False, "exit": None, "exit_ts": None}
        elif k in trades:
            t = trades[k]
            if ev["type"] == "TP1":
                t["tp1"] = True
            elif ev["type"] in FINAL:
                t["r"], t["exit"], t["exit_ts"] = ev["r_total"], ev["type"], ev["event_ts"]
    return list(trades.values())


def summarize(trades, costs_pct=0.1):
    """Estadísticas por setup sobre operaciones CERRADAS (R neto de costes)."""
    out = {}
    closed = [t for t in trades if t["r"] is not None]
    for name in sorted({t["setup"] for t in closed}) + ["TOTAL"]:
        ts = [t for t in closed if name == "TOTAL" or t["setup"] == name]
        ts.sort(key=lambda t: t["exit_ts"])
        rs = np.array([t["r"] - costs_pct / max(t["stop_pct"], 1e-9) for t in ts])
        if len(rs) == 0:
            continue
        wins, losses = rs[rs > 0], rs[rs <= 0]
        cum = np.cumsum(rs)
        dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum])) - np.concatenate([[0.0], cum])))
        out[name] = {
            "n": len(rs), "win_pct": len(wins) / len(rs) * 100, "avg_r": float(rs.mean()),
            "total_r": float(rs.sum()),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "pf": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
            "max_dd": dd,
            "exits": {k: sum(1 for t in ts if t["exit"] == k) for k in FINAL},
            "tp1_pct": sum(1 for t in ts if t["tp1"]) / len(ts) * 100,
        }
    return out


def print_report(stats, trades, args, n_symbols):
    open_n = sum(1 for t in trades if t["r"] is None)
    print(f"\n=== Backtest {args.days}d · {args.tf} · {n_symbols} pares · coste {args.costs_pct}% por operación ===")
    if not stats:
        print("Sin operaciones cerradas en el periodo (prueba más días, más pares o umbrales menos estrictos).")
        return
    print(f"{'Setup':<18}{'Trades':>7}{'Win%':>7}{'TP1%':>7}{'R medio':>9}{'R total':>9}{'PF':>7}{'MaxDD(R)':>10}")
    for name, s in stats.items():
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{name:<18}{s['n']:>7}{s['win_pct']:>7.1f}{s['tp1_pct']:>7.1f}{s['avg_r']:>9.2f}{s['total_r']:>9.1f}{pf:>7}{s['max_dd']:>10.1f}")
    t = stats["TOTAL"]
    print(f"\nR medio ganador {t['avg_win']:+.2f} · R medio perdedor {t['avg_loss']:+.2f} · salidas: "
          + ", ".join(f"{k} {v}" for k, v in t["exits"].items()) + f" · abiertas sin cerrar: {open_n}")
    by_sym = {}
    for tr in trades:
        if tr["r"] is not None:
            by_sym.setdefault(tr["symbol"], []).append(tr["r"])
    top = sorted(by_sym.items(), key=lambda kv: -sum(kv[1]))
    if top:
        print("\nMejores pares (R total bruto): " + ", ".join(f"{s.split('/')[0]} {sum(r):+.1f}R ({len(r)})" for s, r in top[:5]))
        print("Peores pares:                  " + ", ".join(f"{s.split('/')[0]} {sum(r):+.1f}R ({len(r)})" for s, r in top[-5:][::-1]))
    print("\nOrientativo: velas cerradas, sin slippage real, universo = listados actuales (sesgo de supervivencia).")


async def run(args):
    logging.basicConfig(level=logging.WARNING)
    cfg.replay_days = args.days
    tf_ms = timeframe_ms(args.tf)
    bx = BingX(cfg)
    try:
        await bx.load_universe()
        if args.symbols:
            wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
            syms = [s for s in bx.symbols if s.split("/")[0].upper() in wanted]
        else:
            tickers = await bx.fetch_tickers()
            syms = sorted((s for s in bx.symbols if s in tickers),
                          key=lambda s: -(tickers[s].get("quoteVolume") or 0))[: args.top]
        if not syms:
            print("No se encontraron pares.")
            return
        now_ms = int(time.time() * 1000)
        today = now_ms // DAY_MS
        since = (today - args.days - 1) * DAY_MS  # 1 día extra de calentamiento de indicadores
        print(f"Descargando {len(syms)} pares ({args.days} días de {args.tf})...")

        async def one(sym):
            try:
                d = await bx.fetch_ohlcv(sym, "1d", min(1440, args.days + 200))
                if d is None:
                    return []
                d = d[d[:, 0] + DAY_MS <= now_ms]
                if len(d) < 35:
                    return []
                ds = S.daily_series(d[:, 0], d[:, 1], d[:, 2], d[:, 3], d[:, 4], d[:, 5], cfg.ps_lookback)
                raw = await bx.fetch_ohlcv_range(sym, args.tf, since, now_ms)
                if raw is not None:
                    raw = raw[raw[:, 0] + tf_ms <= now_ms]
                if raw is None or len(raw) < 100:
                    print(f"  {sym}: histórico intradía insuficiente, se omite")
                    return []
                bars = (raw[:, 0].astype(np.int64), raw[:, 1], raw[:, 2], raw[:, 3], raw[:, 4], raw[:, 5])
                return S.replay(sym, bars, ds, cfg, tf_ms)["events"]
            except Exception as e:
                print(f"  {sym}: {type(e).__name__}: {e}")
                return []

        events = [ev for evs in await asyncio.gather(*(one(s) for s in syms)) for ev in evs]
        trades = build_trades(events)
        print_report(summarize(trades, args.costs_pct), trades, args, len(syms))
        if args.csv and trades:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(trades)
            print(f"Operaciones guardadas en {args.csv}")
    finally:
        await bx.close()


def main():
    ap = argparse.ArgumentParser(description="Backtest de Qullamaggie Fused con el motor del bot")
    ap.add_argument("--symbols", help="bases separadas por coma, p. ej. BTC,ETH,SOL")
    ap.add_argument("--top", type=int, default=20, help="si no hay --symbols: N pares con más volumen 24h")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tf", default=cfg.timeframe)
    ap.add_argument("--costs-pct", type=float, default=0.1, help="coste ida y vuelta en %% del precio (def. 0.1)")
    ap.add_argument("--csv", help="guardar las operaciones en este CSV")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
