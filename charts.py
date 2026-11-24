"""Gráfico PNG de cada entrada: velas, VWAP, EMA21, Opening Range, entrada, stop y TP1."""
import io
import math
from datetime import datetime, timezone

import numpy as np

DAY_MS = 86_400_000


def _fin(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and math.isfinite(x)


def render_entry_chart(series, ev, cfg):
    """series = (ts, o, h, l, c, ema21, vwap). Devuelve bytes PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts, o, h, l, c, e21, vwap = series
    k = int(np.searchsorted(ts, ev["signal_ts"]))
    k = min(k, len(ts) - 1)
    end = min(len(ts), k + 1 + 6)
    start = max(0, end - cfg.chart_bars)
    x = np.arange(start, end)
    O, H, L, C = o[start:end], h[start:end], l[start:end], c[start:end]
    long_ = ev["side"] == 1

    bg, fg, grid = "#0d1117", "#c9d1d9", "#21262d"
    up_c, dn_c = "#3fb950", "#f85149"
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    cols = np.where(C >= O, up_c, dn_c)
    ax.vlines(x, L, H, colors=cols, linewidth=0.8, zorder=2)
    body = np.maximum(np.abs(C - O), (H.max() - L.min()) * 0.0015)
    ax.bar(x, body, bottom=np.minimum(O, C), color=cols, width=0.7, zorder=3)

    days = ts[start:end] // DAY_MS
    cuts = [0] + [j for j in range(1, len(days)) if days[j] != days[j - 1]] + [len(days)]
    for n_seg, (a, b) in enumerate(zip(cuts[:-1], cuts[1:])):  # VWAP se reinicia cada día UTC
        ax.plot(x[a:b], vwap[start:end][a:b], color="#58a6ff", linewidth=1.3, zorder=4,
                label="VWAP" if n_seg == 0 else None)
    ax.plot(x, e21[start:end], color="#39c5cf", linewidth=1.1, label="EMA21", zorder=4)

    # separadores de día (00:00 UTC)
    for j in range(1, len(days)):
        if days[j] != days[j - 1]:
            ax.axvline(x[j] - 0.5, color=grid, linewidth=1, linestyle=":", zorder=1)

    lv = ev["levels"]
    lines = [
        (ev["entry"], "#ffffff", "--", f"Entrada {ev['entry']:.6g}"),
        (ev["stop"], "#f85149", "-", f"Stop {ev['stop']:.6g}"),
        (ev["tp1"], "#3fb950", "-", f"TP1 {ev['tp1']:.6g}"),
    ]
    if _fin(lv.get("or_high")):
        lines.append((lv["or_high"], "#7ee787", ":", "OR High"))
    if _fin(lv.get("or_low")):
        lines.append((lv["or_low"], "#ff7b72", ":", "OR Low"))
    lo_v, hi_v = float(L.min()), float(H.max())
    for key, color in (("e10", "#f0883e"), ("e20", "#ff7b72")):
        y = lv.get(key)
        if _fin(y) and lo_v * 0.97 <= y <= hi_v * 1.03:
            lines.append((y, color, "-.", f"EMA{key[1:]}d"))

    y_all = [y for y, *_ in lines] + [lo_v, hi_v]
    y_min, y_max = min(y_all), max(y_all)
    span = (y_max - y_min) or 1.0
    for y, color, ls, _ in lines:
        ax.hlines(y, start - 1, end + 0.3, colors=color, linestyles=ls, linewidth=1.1, alpha=0.9, zorder=1)

    # etiquetas a la derecha sin solaparse entre sí
    min_gap = span * 0.035
    placed = []
    for y, color, ls, label in sorted(lines, key=lambda t: t[0]):
        ty = y if not placed else max(y, placed[-1] + min_gap)
        placed.append(ty)
        ax.text(end + 0.8, ty, label, color=color, fontsize=8, va="center", zorder=6,
                bbox=dict(facecolor=bg, edgecolor="none", pad=1.2))

    pad = (max(hi_v, ev["stop"], ev["tp1"]) - min(lo_v, ev["stop"], ev["tp1"])) * 0.06
    ax.set_ylim(min(lo_v, ev["stop"], ev["tp1"]) - pad, max(hi_v, ev["stop"], ev["tp1"]) + pad + span * 0.02)
    ax.set_xlim(start - 1, end + 16)

    # marcador de la señal
    ks = min(max(k, start), end - 1)
    off = (H.max() - L.min()) * 0.02
    if long_:
        ax.scatter([ks], [l[ks] - off], marker="^", s=90, color="#ffd33d", zorder=5)
    else:
        ax.scatter([ks], [h[ks] + off], marker="v", s=90, color="#ffd33d", zorder=5)

    # etiquetas de tiempo UTC
    n_ticks = 8
    idx = np.linspace(start, end - 1, n_ticks).astype(int)
    ax.set_xticks(idx)
    ax.set_xticklabels([datetime.fromtimestamp(ts[i] / 1000, tz=timezone.utc).strftime("%d %H:%M") for i in idx],
                       color=fg, fontsize=8)
    ax.tick_params(axis="y", colors=fg, labelsize=8)
    ax.grid(color=grid, linewidth=0.5, alpha=0.8)
    for sp in ax.spines.values():
        sp.set_color(grid)

    base = ev["symbol"].split("/")[0]
    name = "EP LONG" if long_ else "PARABOLIC SHORT"
    ax.set_title(f"{name} · {base}/USDT · {cfg.timeframe} · UTC", color=fg, fontsize=11, loc="left")
    leg = ax.legend(loc="upper left", fontsize=8, facecolor=bg, edgecolor=grid)
    for t in leg.get_texts():
        t.set_color(fg)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
