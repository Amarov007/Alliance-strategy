"""Configuration module for the Multi-School Institutional Trading Signals Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class SchoolWeights:
    """Confluence engine school weights (must sum to 100)."""

    smc: int = 35
    volume_vwap: int = 25
    pa_divergence: int = 20
    regime_mtf: int = 20

    @property
    def total(self) -> int:
        return self.smc + self.volume_vwap + self.pa_divergence + self.regime_mtf


@dataclass(frozen=True)
class StrategyThresholds:
    """Quantitative indicator and risk management thresholds."""

    min_confluence_score: int = _get_int("MIN_CONFLUENCE_SCORE", 75)
    min_rr_tp2: float = _get_float("MIN_RR_TP2", 2.0)

    # Structural detection
    swing_left: int = 3
    swing_right: int = 3
    ob_lookback: int = 40
    fvg_lookback: int = 30
    fvg_min_atr_ratio: float = 0.15
    sweep_lookback: int = 12
    zone_proximity_atr: float = 1.0

    # Risk management
    atr_period: int = 14
    sl_atr_buffer: float = 0.5
    tp1_min_r: float = 1.0
    tp2_min_r: float = 2.0
    tp3_min_r: float = 3.2
    tp2_max_r: float = 5.0      # cap on structural TP2 distance (in R)
    tp3_max_r: float = 8.0      # cap on structural TP3 distance (in R)
    max_risk_atr: float = 4.0   # reject setups whose stop is wider than N * ATR
    min_risk_atr: float = 0.35  # reject setups whose stop is inside the noise band

    # Volume & VWAP
    rvol_period: int = 20
    rvol_moderate: float = 1.0
    rvol_strong: float = 1.3
    rvol_institutional: float = 1.8

    # Momentum & Divergence
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    divergence_lookback: int = 45

    # Regime & MTF
    adx_period: int = 14
    adx_trend_threshold: float = 25.0
    ema_fast_period: int = 20
    ema_mid_period: int = 50
    ema_trend_period: int = 200

    # Gold (XAUUSD) London + New York session liquidity hours in UTC
    gold_session_start_utc: int = _get_int("GOLD_SESSION_START_UTC", 7)
    gold_session_end_utc: int = _get_int("GOLD_SESSION_END_UTC", 21)


@dataclass(frozen=True)
class BotConfig:
    """Runtime configuration loaded from environment variables."""

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    telegram_timeout_seconds: int = _get_int("TELEGRAM_TIMEOUT_SECONDS", 15)

    # Market Data
    top_crypto_count: int = _get_int("TOP_CRYPTO_COUNT", 20)
    ohlcv_limit: int = _get_int("OHLCV_LIMIT", 300)
    enable_crypto: bool = _get_bool("ENABLE_CRYPTO", True)
    enable_gold: bool = _get_bool("ENABLE_GOLD", True)

    gold_data_provider: str = field(
        default_factory=lambda: os.getenv("GOLD_DATA_PROVIDER", "yfinance").strip().lower()
    )
    gold_yfinance_symbol: str = field(
        default_factory=lambda: os.getenv("GOLD_YFINANCE_SYMBOL", "GC=F").strip()
    )
    gold_twelvedata_symbol: str = field(
        default_factory=lambda: os.getenv("GOLD_TWELVEDATA_SYMBOL", "XAU/USD").strip()
    )
    twelvedata_api_key: str = field(
        default_factory=lambda: os.getenv("TWELVEDATA_API_KEY", "").strip()
    )

    # Optional AI Confirmation Layer
    ai_api_key: str = field(
        default_factory=lambda: os.getenv("AI_API_KEY", "").strip()
    )
    ai_provider: str = field(
        default_factory=lambda: os.getenv("AI_PROVIDER", "auto").strip().lower()
    )
    ai_model: str = field(
        default_factory=lambda: os.getenv("AI_MODEL", "").strip()
    )
    ai_timeout_seconds: int = _get_int("AI_TIMEOUT_SECONDS", 25)

    # Scheduler & Persistence
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "trades.db").strip()
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").strip().upper()
    )
    log_file: str = field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/trading_bot.log").strip()
    )
    scan_interval_seconds: int = _get_int("SCAN_INTERVAL_SECONDS", 300)
    monitor_interval_seconds: int = _get_int("MONITOR_INTERVAL_SECONDS", 30)
    daily_report_hour_utc: int = _get_int("DAILY_REPORT_HOUR_UTC", 20)
    weekly_report_day_utc: int = _get_int("WEEKLY_REPORT_DAY_UTC", 6)  # 6 = Sunday

    # Trade lifecycle & risk-of-ruin guards
    signal_cooldown_hours: float = _get_float("SIGNAL_COOLDOWN_HOURS", 4.0)
    pending_expiry_hours: float = _get_float("PENDING_EXPIRY_HOURS", 12.0)
    max_open_trades: int = _get_int("MAX_OPEN_TRADES", 6)
    drop_forming_candle: bool = _get_bool("DROP_FORMING_CANDLE", True)
    send_startup_message: bool = _get_bool("SEND_STARTUP_MESSAGE", True)

    # Dashboard / API (read-only)
    api_enabled: bool = _get_bool("API_ENABLED", True)
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "127.0.0.1").strip())
    api_port: int = _get_int("API_PORT", 8080)
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", "").strip())

    # Rate limits & Retries
    ccxt_pause_seconds: float = _get_float("CCXT_PAUSE_SECONDS", 0.25)
    max_retries: int = _get_int("MAX_RETRIES", 3)
    retry_backoff_base: float = _get_float("RETRY_BACKOFF_BASE", 1.5)

    # Sub-configs
    weights: SchoolWeights = field(default_factory=SchoolWeights)
    thresholds: StrategyThresholds = field(default_factory=StrategyThresholds)


def validate_config(cfg: BotConfig) -> list[str]:
    """Validate configuration and return a list of fatal configuration errors."""
    errors: list[str] = []

    if cfg.weights.total != 100:
        errors.append(
            f"School weights must sum to 100, got {cfg.weights.total}."
        )
    if not (0 < cfg.thresholds.min_confluence_score <= 100):
        errors.append("MIN_CONFLUENCE_SCORE must be between 1 and 100.")
    if cfg.thresholds.min_rr_tp2 < 1.0:
        errors.append("MIN_RR_TP2 must be at least 1.0.")
    if not cfg.telegram_bot_token:
        errors.append("TELEGRAM_BOT_TOKEN is required.")
    if not cfg.telegram_chat_id:
        errors.append("TELEGRAM_CHAT_ID is required.")
    if cfg.gold_data_provider not in {"yfinance", "twelvedata"}:
        errors.append(
            f"Unsupported GOLD_DATA_PROVIDER '{cfg.gold_data_provider}'. Use 'yfinance' or 'twelvedata'."
        )
    if cfg.enable_gold and cfg.gold_data_provider == "twelvedata" and not cfg.twelvedata_api_key:
        errors.append(
            "TWELVEDATA_API_KEY is required when GOLD_DATA_PROVIDER=twelvedata."
        )
    if cfg.ai_provider not in {"auto", "anthropic", "openai"}:
        errors.append(
            f"Unsupported AI_PROVIDER '{cfg.ai_provider}'. Use 'auto', 'anthropic', or 'openai'."
        )
    if cfg.scan_interval_seconds < 30:
        errors.append("SCAN_INTERVAL_SECONDS must be >= 30 seconds.")
    if cfg.monitor_interval_seconds < 5:
        errors.append("MONITOR_INTERVAL_SECONDS must be >= 5 seconds.")
    if cfg.ohlcv_limit < 250:
        errors.append("OHLCV_LIMIT must be >= 250 (EMA200 needs enough history).")
    if cfg.max_open_trades < 1:
        errors.append("MAX_OPEN_TRADES must be >= 1.")
    if cfg.api_enabled and cfg.api_host not in {"127.0.0.1", "localhost"} and not cfg.api_token:
        errors.append(
            "API_TOKEN is required when API_HOST exposes the dashboard beyond localhost."
        )

    return errors
