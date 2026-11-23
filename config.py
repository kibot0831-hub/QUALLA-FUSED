"""config.py — Parámetros del bot.

Todos los valores por defecto son los inputs del Pine "Qullamaggie Fused v2".
Cada uno se puede cambiar con una variable de entorno (Railway → Variables).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("qfused.config")

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

# Contratos de BingX que NO son cripto (acciones, índices, divisas, materias primas)
NON_CRYPTO_PREFIXES = ("NCCO", "NCSK", "NCFX", "NCSI", "NCIN", "NCST")
NON_CRYPTO_BASES = frozenset({
    "XAU", "XAG", "XPT", "XPD", "WTI", "BRENT", "USOIL", "UKOIL", "NATGAS",
    "NAS100", "US30", "US500", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
})
STABLE_BASES = frozenset({"USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD", "USDP", "PYUSD"})


def is_excluded(symbol: str, extra=()) -> bool:
    """True si el contrato no es cripto "operable" (stable, acción, índice...)."""
    s = symbol.upper()
    base = s.rsplit("-", 1)[0]
    if s in extra or base in extra:
        return True
    if base in STABLE_BASES or base in NON_CRYPTO_BASES:
        return True
    if base.startswith(NON_CRYPTO_PREFIXES) or "2USD" in base:
        return True
    return False


# ───────────────────────── lectura de entorno ─────────────────────────
def _raw(name):
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _float(name, default):
    v = _raw(name)
    if v is None:
        return default
    try:
        return float(v.replace(",", "."))
    except ValueError:
        log.warning("%s=%r no es un número; uso %s", name, v, default)
        return default


def _int(name, default):
    v = _raw(name)
    if v is None:
        return default
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        log.warning("%s=%r no es un entero; uso %s", name, v, default)
        return default


def _bool(name, default):
    v = _raw(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "si", "sí", "on", "verdadero")


def _choice(name, default, options):
    v = _raw(name)
    if v is None:
        return default
    key = v.upper().replace(" ", "_").replace("/", "_").replace("-", "_")
    if key in options:
        return key
    log.warning("%s=%r inválido (opciones: %s); uso %s", name, v, "/".join(options), default)
    return default


def _csv(name):
    v = _raw(name)
    if not v:
        return ()
    return tuple(x.strip().upper() for x in v.split(",") if x.strip())


def resolve_state_dir() -> str:
    """Carpeta para el JSON de estado: STATE_DIR → volumen de Railway → /data → ."""
    cands = [_raw("STATE_DIR"), _raw("RAILWAY_VOLUME_MOUNT_PATH"), "/data"]
    for d in cands:
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return "."


@dataclass
class Config:
    # General
    timeframe: str = "15m"
    setups: str = "BOTH"            # BOTH | EP | PS
    # Sesión
    sess_offset_min: int = 0        # 0 = 00:00 UTC (cripto 24/7)
    or_minutes: int = 15
    skip_first_min: int = 5
    window_min: int = 1080
    day_len_min: int = 1440
    max_trades_day: int = 2
    # Riesgo
    risk_pct_ep: float = 0.35
    risk_pct_short: float = 0.25
    max_pos_pct: float = 22.0
    max_adr_mult: float = 1.3
    stop_mode: str = "DAY"          # DAY | OR | ATR
    sl_buf: float = 0.1
    # EP Long
    ep_min_change: float = 7.0
    ep_min_adr: float = 3.5
    ep_min_rvol: float = 1.5
    ep_trigger: str = "ORH"         # ORH | HH | VWAP_EMA
    # Parabolic Short
    ps_min_gain: float = 50.0
    ps_lookback: int = 15
    ps_min_green: int = 3
    ps_min_ext: float = 25.0
    ps_trigger: str = "ORL"         # ORL | FIRST_RED | VWAP_FAIL
    # Gestión
    partial_r: float = 1.8
    partial_pct: float = 50.0
    use_trail_ema: bool = True
    trail_ema_len: int = 10         # 10 | 20
    max_hold_bars: int = 0
    # Bot
    account_equity: float = 1000.0  # solo para calcular el tamaño sugerido
    min_quote_vol: float = 100_000.0
    exclude: tuple = ()
    rps: float = 8.0
    concurrency: int = 8
    candle_delay_sec: int = 8
    warmup_bars: int = 120
    daily_bars: int = 120
    max_signal_age_bars: int = 1
    send_radar: bool = True
    radar_max: int = 15
    send_daily_summary: bool = True
    send_startup: bool = True
    run_once: bool = False
    log_level: str = "INFO"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    state_dir: str = "."

    @property
    def tf_min(self) -> int:
        return TF_MINUTES.get(self.timeframe, 15)

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        tf = (_raw("TIMEFRAME") or d.timeframe).lower()
        if tf not in TF_MINUTES:
            log.warning("TIMEFRAME=%r no soportado (%s); uso 15m", tf, "/".join(TF_MINUTES))
            tf = "15m"
        trail_len = _int("TRAIL_EMA_LEN", d.trail_ema_len)
        if trail_len not in (10, 20):
            log.warning("TRAIL_EMA_LEN debe ser 10 o 20; uso 10")
            trail_len = 10
        return cls(
            timeframe=tf,
            setups=_choice("SETUPS", d.setups, ("BOTH", "EP", "PS")),
            sess_offset_min=_int("SESS_OFFSET_MIN", d.sess_offset_min),
            or_minutes=max(1, _int("OR_MINUTES", d.or_minutes)),
            skip_first_min=max(0, _int("SKIP_FIRST_MIN", d.skip_first_min)),
            window_min=max(0, _int("WINDOW_MIN", d.window_min)),
            day_len_min=max(60, _int("DAY_LEN_MIN", d.day_len_min)),
            max_trades_day=max(1, _int("MAX_TRADES_DAY", d.max_trades_day)),
            risk_pct_ep=_float("RISK_PCT_EP", d.risk_pct_ep),
            risk_pct_short=_float("RISK_PCT_SHORT", d.risk_pct_short),
            max_pos_pct=_float("MAX_POS_PCT", d.max_pos_pct),
            max_adr_mult=_float("MAX_ADR_MULT", d.max_adr_mult),
            stop_mode=_choice("STOP_MODE", d.stop_mode, ("DAY", "OR", "ATR")),
            sl_buf=_float("SL_BUF", d.sl_buf),
            ep_min_change=_float("EP_MIN_CHANGE", d.ep_min_change),
            ep_min_adr=_float("EP_MIN_ADR", d.ep_min_adr),
            ep_min_rvol=_float("EP_MIN_RVOL", d.ep_min_rvol),
            ep_trigger=_choice("EP_TRIGGER", d.ep_trigger, ("ORH", "HH", "VWAP_EMA")),
            ps_min_gain=_float("PS_MIN_GAIN", d.ps_min_gain),
            ps_lookback=max(2, _int("PS_LOOKBACK", d.ps_lookback)),
            ps_min_green=_int("PS_MIN_GREEN", d.ps_min_green),
            ps_min_ext=_float("PS_MIN_EXT", d.ps_min_ext),
            ps_trigger=_choice("PS_TRIGGER", d.ps_trigger, ("ORL", "FIRST_RED", "VWAP_FAIL")),
            partial_r=_float("PARTIAL_R", d.partial_r),
            partial_pct=min(90.0, max(10.0, _float("PARTIAL_PCT", d.partial_pct))),
            use_trail_ema=_bool("USE_TRAIL_EMA", d.use_trail_ema),
            trail_ema_len=trail_len,
            max_hold_bars=max(0, _int("MAX_HOLD_BARS", d.max_hold_bars)),
            account_equity=max(1.0, _float("ACCOUNT_EQUITY", d.account_equity)),
            min_quote_vol=max(0.0, _float("MIN_QUOTE_VOL_24H", d.min_quote_vol)),
            exclude=_csv("EXCLUDE_SYMBOLS"),
            rps=max(1.0, _float("API_RPS", d.rps)),
            concurrency=max(1, _int("CONCURRENCY", d.concurrency)),
            candle_delay_sec=max(0, _int("CANDLE_DELAY_SEC", d.candle_delay_sec)),
            warmup_bars=max(60, _int("WARMUP_BARS", d.warmup_bars)),
            daily_bars=max(60, _int("DAILY_BARS", d.daily_bars)),
            max_signal_age_bars=max(0, _int("MAX_SIGNAL_AGE_BARS", d.max_signal_age_bars)),
            send_radar=_bool("SEND_RADAR", d.send_radar),
            radar_max=max(1, _int("RADAR_MAX", d.radar_max)),
            send_daily_summary=_bool("SEND_DAILY_SUMMARY", d.send_daily_summary),
            send_startup=_bool("SEND_STARTUP", d.send_startup),
            run_once=_bool("RUN_ONCE", d.run_once),
            log_level=(_raw("LOG_LEVEL") or d.log_level).upper(),
            telegram_token=_raw("TELEGRAM_BOT_TOKEN") or _raw("TELEGRAM_TOKEN") or "",
            telegram_chat_id=_raw("TELEGRAM_CHAT_ID") or _raw("CHAT_ID") or "",
            state_dir=resolve_state_dir(),
        )
