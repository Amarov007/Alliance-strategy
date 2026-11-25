"""Main scheduler and orchestration engine for the Multi-School Institutional Trading Bot."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_filter import AIConfirmationFilter
from config import BotConfig, validate_config
from data_fetcher import MarketDataFetcher, SymbolMetadata
from database import TradeDatabase, TradeRecord
from strategy_engine import ConfluenceEngine
from telegram_bot import (
    REPLY_BREAKEVEN_STOP,
    REPLY_ENTRY_HIT,
    REPLY_EXPIRED,
    REPLY_MISSED_ENTRY,
    REPLY_SL_HIT,
    REPLY_TP1_HIT,
    REPLY_TP2_HIT,
    REPLY_TP3_HIT,
    TelegramNotifier,
)


class SecretRedactingFilter(logging.Filter):
    """Mask sensitive tokens/keys if they ever appear in log messages."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Tracebacks from `requests` embed the full URL, i.e. the Telegram token.
            record.exc_text = self._scrub(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


def setup_logging(cfg: BotConfig) -> None:
    log_path = Path(cfg.log_file)
    if str(log_path.parent) not in {"", "."}:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.log_level, logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    redact_filter = SecretRedactingFilter(
        [cfg.telegram_bot_token, cfg.ai_api_key, cfg.twelvedata_api_key]
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redact_filter)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact_filter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Reduce noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


class TradingSignalBot:
    """Coordinates market scanning, confluence scoring, AI filtering, and live trade tracking."""

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.db = TradeDatabase(cfg.db_path)
        # Separate fetchers: ccxt exchange objects are not thread-safe, and the
        # trade monitor must keep running while a long market scan is in progress.
        self.scan_fetcher = MarketDataFetcher(cfg)
        self.monitor_fetcher = MarketDataFetcher(cfg)
        self.engine = ConfluenceEngine(cfg)
        self.ai_filter = AIConfirmationFilter(cfg)
        self.notifier = TelegramNotifier(cfg)
        self._running = threading.Event()
        self._running.set()
        self._last_check: dict[int, datetime] = {}

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def stop(self) -> None:
        self._running.clear()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_asset(self, meta: SymbolMetadata) -> None:
        name = meta.display_name
        if self.db.has_open_trade(name):
            logger.debug("Skipping %s: open trade already exists.", name)
            return
        if self.db.in_cooldown(name, self.cfg.signal_cooldown_hours):
            logger.debug("Skipping %s: cooldown active.", name)
            return
        if self.db.count_open_trades() >= self.cfg.max_open_trades:
            logger.debug("Skipping %s: max open trades reached.", name)
            return

        if meta.asset_class == "GOLD" and not ConfluenceEngine.is_gold_session_active(
            start_h=self.cfg.thresholds.gold_session_start_utc,
            end_h=self.cfg.thresholds.gold_session_end_utc,
        ):
            logger.debug("Skipping %s: outside London/NY liquidity hours.", name)
            return

        try:
            frames = self.scan_fetcher.fetch_multi_timeframe(meta.symbol, meta.asset_class)
            candidate = self.engine.evaluate(meta, frames)
            if candidate is None:
                return

            logger.info(
                "High-probability setup detected: %s %s | Score=%d | RR=1:%.2f | Regime=%s",
                name, candidate.direction, candidate.score, candidate.rr_tp2, candidate.regime_code,
            )

            decision = self.ai_filter.confirm_and_explain(candidate)
            if not decision.approved:
                logger.info("Setup rejected by AI layer (%s): %s", name, decision.rejection_reason)
                return

            trade_id = self.db.create_trade(
                symbol=name,
                asset_class=candidate.asset_class,
                direction=candidate.direction,
                timeframe=candidate.timeframe,
                regime=candidate.regime_ar,
                score=candidate.score,
                entry_price=candidate.entry_price,
                stop_loss=candidate.stop_loss,
                tp1=candidate.tp1,
                tp2=candidate.tp2,
                tp3=candidate.tp3,
                rr_ratio=candidate.rr_tp2,
                rationale_ar=decision.rationale_ar,
            )

            msg_id = self.notifier.send_signal(candidate, decision.rationale_ar)
            if msg_id is None:
                # Never track a trade that subscribers were not told about.
                self.db.delete_trade(trade_id)
                logger.error("Telegram send failed; rolled back trade #%d (%s).", trade_id, name)
                return

            self.db.set_telegram_message_id(trade_id, msg_id)
            logger.info("Signal #%d (%s) broadcast (msg_id=%d).", trade_id, name, msg_id)

        except Exception as exc:
            logger.exception("Error scanning %s: %s", name, exc)

    def run_market_scan(self) -> None:
        logger.info("Starting market scan cycle...")
        assets: list[SymbolMetadata] = []

        if self.cfg.enable_crypto:
            try:
                assets.extend(self.scan_fetcher.get_top_crypto_symbols())
            except Exception as exc:
                logger.error("Failed to fetch top crypto pairs: %s", exc)

        if self.cfg.enable_gold:
            assets.append(self.scan_fetcher.get_gold_metadata())

        for meta in assets:
            if not self.running:
                break
            self._scan_asset(meta)

        logger.info("Market scan cycle completed (%d instruments checked).", len(assets))

    # ------------------------------------------------------------------
    # Live trade tracking (candle-accurate, conservative ordering)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _reply(self, trade: TradeRecord, text: str) -> None:
        self.notifier.send_trade_reply(trade.telegram_message_id, text)

    def _handle_pending(self, trade: TradeRecord, low: float, high: float, now: datetime) -> bool:
        """Handle a PENDING trade. Returns True if it became ACTIVE this cycle."""
        created = self._parse_ts(trade.created_at) or now
        age = (now - created).total_seconds()
        near_fill = age <= 300
        is_long = trade.direction == "LONG"

        if is_long:
            fill_level = trade.entry_price * (1.0008 if near_fill else 1.0)
            filled = low <= fill_level
            ran_away = high >= trade.tp1
        else:
            fill_level = trade.entry_price * (0.9992 if near_fill else 1.0)
            filled = high >= fill_level
            ran_away = low <= trade.tp1

        if filled:
            self.db.mark_entry_hit(trade.id)
            self._reply(trade, REPLY_ENTRY_HIT)
            logger.info("Trade #%d (%s) activated.", trade.id, trade.symbol)
            return True

        if ran_away:
            self.db.cancel_trade(trade.id)
            self._reply(trade, REPLY_MISSED_ENTRY)
            logger.info("Trade #%d (%s) cancelled: ran to TP1 unfilled.", trade.id, trade.symbol)
            return False

        if age > self.cfg.pending_expiry_hours * 3600:
            self.db.cancel_trade(trade.id)
            self._reply(trade, REPLY_EXPIRED)
            logger.info("Trade #%d (%s) expired unfilled.", trade.id, trade.symbol)
        return False

    def _handle_active(self, trade: TradeRecord, low: float, high: float) -> None:
        """Advance an ACTIVE trade using the traded range since the last check.

        Ordering is deliberately conservative: when SL and a target could both have
        printed inside the same window, the stop is assumed to have come first.
        After TP1 the stop is breakeven (entry), exactly as the signal message promises.
        """
        is_long = trade.direction == "LONG"

        def reached(level: float) -> bool:
            return high >= level if is_long else low <= level

        def stopped(level: float) -> bool:
            return low <= level if is_long else high >= level

        progressed = False  # a target printed this cycle -> window may hold pre-TP1 prices

        # Phase 1: before TP1 the original stop is in force
        if not trade.tp1_hit:
            if stopped(trade.stop_loss):
                self.db.mark_sl_hit(trade.id)
                self._reply(trade, REPLY_SL_HIT)
                logger.info("Trade #%d (%s) hit SL.", trade.id, trade.symbol)
                return
            if reached(trade.tp1):
                self.db.mark_tp1_hit(trade.id)
                self._reply(trade, REPLY_TP1_HIT)
                logger.info("Trade #%d (%s) hit TP1.", trade.id, trade.symbol)
                trade = self.db.get_trade_by_id(trade.id) or trade
                progressed = True
            else:
                return

        # Phase 2: stop now sits at breakeven; targets can still print in the same window
        if not trade.tp2_hit and reached(trade.tp2):
            self.db.mark_tp2_hit(trade.id)
            self._reply(trade, REPLY_TP2_HIT)
            logger.info("Trade #%d (%s) hit TP2.", trade.id, trade.symbol)
            trade = self.db.get_trade_by_id(trade.id) or trade
            progressed = True

        if trade.tp2_hit and not trade.tp3_hit and reached(trade.tp3):
            self.db.mark_tp3_hit(trade.id)
            self._reply(trade, REPLY_TP3_HIT)
            logger.info("Trade #%d (%s) hit TP3 (CLOSED).", trade.id, trade.symbol)
            return

        # Breakeven stop-out: only on windows that began after TP1 was banked
        if not progressed and trade.tp1_hit and not trade.tp3_hit and stopped(trade.entry_price):
            fresh = self.db.get_trade_by_id(trade.id)
            if fresh is not None and fresh.status == "ACTIVE":
                self.db.mark_sl_hit(trade.id)
                self._reply(trade, REPLY_BREAKEVEN_STOP)
                logger.info("Trade #%d (%s) stopped at breakeven.", trade.id, trade.symbol)

    @staticmethod
    def _resolve_ccxt_symbol(trade: TradeRecord) -> str:
        if trade.asset_class == "GOLD" or trade.symbol == "XAUUSD":
            return "XAUUSD"
        if "/" in trade.symbol:
            return trade.symbol
        if trade.symbol.endswith("USDT"):
            return f"{trade.symbol[:-4]}/USDT:USDT"
        return trade.symbol

    def monitor_open_trades(self) -> None:
        open_trades = self.db.get_open_trades()
        live_ids = {t.id for t in open_trades}
        for stale in [i for i in self._last_check if i not in live_ids]:
            self._last_check.pop(stale, None)
        if not open_trades:
            return

        window_cache: dict[tuple[str, str], tuple[float, float, float]] = {}
        for trade in open_trades:
            if not self.running:
                break
            try:
                now = datetime.now(timezone.utc)
                since = self._last_check.get(trade.id)
                if since is None:
                    if trade.tp1_hit:
                        # After a restart never replay pre-TP1 history against the breakeven stop
                        since = now - timedelta(minutes=2)
                    else:
                        since = (
                            self._parse_ts(trade.activated_at)
                            or self._parse_ts(trade.created_at)
                            or now
                        )
                sym = self._resolve_ccxt_symbol(trade)
                key = (sym, since.replace(second=0, microsecond=0).isoformat())
                if key not in window_cache:
                    window_cache[key] = self.monitor_fetcher.fetch_price_window(
                        sym, trade.asset_class, since
                    )
                low, high, _last = window_cache[key]

                became_active = False
                if trade.status == "PENDING":
                    became_active = self._handle_pending(trade, low, high, now)
                    if became_active:
                        trade = self.db.get_trade_by_id(trade.id) or trade
                if trade.status == "ACTIVE":
                    self._handle_active(trade, low, high)

                self._last_check[trade.id] = now
            except Exception as exc:
                logger.warning(
                    "Live monitor error for trade #%d (%s): %s", trade.id, trade.symbol, exc
                )

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def check_scheduled_reports(self) -> None:
        now = datetime.now(timezone.utc)
        if now.hour < self.cfg.daily_report_hour_utc:
            return

        today_str = now.strftime("%Y-%m-%d")
        if self.db.get_meta("last_daily_report_date") != today_str:
            stats_daily = self.db.get_performance_stats(days=1)
            if self.notifier.send_performance_report("اليومي (آخر 24 ساعة)", stats_daily) is not None:
                self.db.set_meta("last_daily_report_date", today_str)
                logger.info("Sent daily performance report for %s.", today_str)

        if now.weekday() == self.cfg.weekly_report_day_utc:
            iso_year, iso_week, _ = now.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            if self.db.get_meta("last_weekly_report_week") != week_key:
                stats_weekly = self.db.get_performance_stats(days=7)
                if self.notifier.send_performance_report("الأسبوعي (آخر 7 أيام)", stats_weekly) is not None:
                    self.db.set_meta("last_weekly_report_week", week_key)
                    logger.info("Sent weekly performance report for %s.", week_key)

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while self.running:
            started = time.monotonic()
            try:
                self.monitor_open_trades()
            except Exception as exc:
                logger.exception("Unhandled error in monitor loop: %s", exc)
            remaining = self.cfg.monitor_interval_seconds - (time.monotonic() - started)
            self._sleep(max(1.0, remaining))

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def run_forever(self) -> None:
        logger.info(
            "Institutional Multi-School Signal Bot started | Scan=%ds | Monitor=%ds | MinScore=%d | MinRR=1:%.1f",
            self.cfg.scan_interval_seconds,
            self.cfg.monitor_interval_seconds,
            self.cfg.thresholds.min_confluence_score,
            self.cfg.thresholds.min_rr_tp2,
        )
        if self.cfg.send_startup_message:
            self.notifier.send_text(
                "🤖 <b>تم تشغيل البوت</b>\n"
                f"الحد الأدنى للتقييم: <code>{self.cfg.thresholds.min_confluence_score}</code> | "
                f"أدنى عائد/مخاطرة: <code>1:{self.cfg.thresholds.min_rr_tp2:g}</code>"
            )

        monitor_thread = threading.Thread(target=self._monitor_loop, name="trade-monitor", daemon=True)
        monitor_thread.start()

        last_scan = 0.0
        while self.running:
            if time.monotonic() - last_scan >= self.cfg.scan_interval_seconds:
                try:
                    self.run_market_scan()
                    self.check_scheduled_reports()
                except Exception as exc:
                    logger.exception("Unhandled error in scan loop: %s", exc)
                last_scan = time.monotonic()
            if not monitor_thread.is_alive() and self.running:
                logger.error("Monitor thread died; restarting.")
                monitor_thread = threading.Thread(
                    target=self._monitor_loop, name="trade-monitor", daemon=True
                )
                monitor_thread.start()
            self._sleep(1.0)

        monitor_thread.join(timeout=5)
        logger.info("Bot shutdown complete.")


def main() -> int:
    cfg = BotConfig()
    setup_logging(cfg)

    errors = validate_config(cfg)
    if errors:
        for err in errors:
            logger.critical("Configuration error: %s", err)
        return 1

    bot = TradingSignalBot(cfg)

    if cfg.api_enabled:
        from api_server import start_api_server

        start_api_server(cfg, bot.db)

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received OS signal %d; stopping gracefully...", signum)
        bot.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
