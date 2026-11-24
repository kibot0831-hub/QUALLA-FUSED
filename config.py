"""Configuración del bot. Todo se controla con variables de entorno (Railway > Variables).
Los valores por defecto son los mismos que los inputs del script Pine original."""
import os


def _s(name, default=""):
    return os.getenv(name, default).strip()


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return int(default)


def _b(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def _list(name):
    return [x.strip().upper() for x in os.getenv(name, "").split(",") if x.strip()]


class Config:
    def __init__(self):
        # ---------- Telegram ----------
        self.telegram_token = _s("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _s("TELEGRAM_CHAT_ID")
        self.telegram_thread_id = _s("TELEGRAM_THREAD_ID")  # opcional (temas de grupos)

        # ---------- General ----------
        setup = _s("ACTIVE_SETUP", "BOTH").upper()
        self.ep_on = setup in ("BOTH", "EP", "EP LONG")
        self.ps_on = setup in ("BOTH", "PS", "PARABOLIC SHORT")
        self.timeframe = _s("TIMEFRAME", "5m")

        # ---------- Sesión ----------
        self.sess_offset = _i("SESS_OFFSET_MIN", 0)
        self.or_minutes = _i("OR_MINUTES", 15)
        self.skip_first_min = _i("SKIP_FIRST_MIN", 5)
        self.window_min = _i("WINDOW_MIN", 1080)
        self.day_len = _i("DAY_LEN_MIN", 1440)
        self.max_trades_day = _i("MAX_TRADES_DAY", 2)

        # ---------- Riesgo ----------
        self.risk_pct_ep = _f("RISK_PCT_EP", 0.35)
        self.risk_pct_short = _f("RISK_PCT_SHORT", 0.25)
        self.max_pos_pct = _f("MAX_POS_PCT", 22.0)
        self.max_adr_mult = _f("MAX_ADR_MULT", 1.3)
        self.stop_mode = _s("STOP_MODE", "DAY").upper()  # DAY | OR | ATR
        self.sl_buf = _f("SL_BUF_ATR", 0.1)
        self.account_equity = _f("ACCOUNT_EQUITY", 10000)  # solo para calcular el tamaño sugerido

        # ---------- EP Long ----------
        self.ep_min_change = _f("EP_MIN_CHANGE", 7.0)
        self.ep_min_adr = _f("EP_MIN_ADR", 3.5)
        self.ep_min_rvol = _f("EP_MIN_RVOL", 1.5)
        self.ep_trigger = _s("EP_TRIGGER", "ORH").upper().replace("/", "_")  # ORH | VWAP_EMA | HH

        # ---------- Parabolic Short ----------
        self.ps_min_gain = _f("PS_MIN_GAIN", 50.0)
        self.ps_lookback = _i("PS_LOOKBACK", 15)
        self.ps_min_green = _i("PS_MIN_GREEN", 3)
        self.ps_min_ext = _f("PS_MIN_EXT", 25.0)
        self.ps_trigger = _s("PS_TRIGGER", "ORL").upper().replace(" ", "_")  # ORL | FIRST_RED | VWAP_FAIL

        # ---------- Gestión ----------
        self.partial_r = _f("PARTIAL_R", 1.8)
        self.partial_pct = _f("PARTIAL_PCT", 50.0)
        self.use_trail_ema = _b("USE_TRAIL_EMA", True)
        self.trail_ema_len = _i("TRAIL_EMA_LEN", 10)  # 10 o 20
        self.max_hold_bars = _i("MAX_HOLD_BARS", 0)

        # ---------- Bot ----------
        self.min_quote_volume = _f("MIN_QUOTE_VOLUME", 100000)  # USDT 24h; 0 = analizar todo
        self.whitelist = _list("SYMBOL_WHITELIST")  # ej. BTC,ETH (vacío = todos)
        self.blacklist = _list("SYMBOL_BLACKLIST")
        self.replay_days = max(1, _i("REPLAY_DAYS", 3))
        self.bar_close_delay = _i("BAR_CLOSE_DELAY_SEC", 8)
        self.max_event_age_bars = max(1, _i("MAX_EVENT_AGE_BARS", 3))
        self.concurrency = max(1, _i("CONCURRENCY", 6))
        self.rate_limit_ms = _i("RATE_LIMIT_MS", 120)
        self.state_path = _s("STATE_PATH", "state.json")
        self.heartbeat_hours = _f("HEARTBEAT_HOURS", 6)
        self.send_exit_updates = _b("SEND_EXIT_UPDATES", True)
        self.log_level = _s("LOG_LEVEL", "INFO").upper()
        self.send_charts = _b("SEND_CHARTS", True)          # gráfico PNG con cada entrada
        self.chart_bars = max(40, _i("CHART_BARS", 120))
        self.live_poll_sec = _i("LIVE_POLL_SEC", 20)        # vigilancia casi en vivo de stop/TP1 (0 = off)
        self.commands_enabled = _b("COMMANDS_ENABLED", True)  # /status /analiza /watchlist ...


cfg = Config()
