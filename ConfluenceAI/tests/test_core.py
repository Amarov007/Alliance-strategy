"""Offline test-suite: no network, no ccxt/yfinance needed.  Run:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import BotConfig, validate_config  # noqa: E402
from data_fetcher import SymbolMetadata, round_to_tick  # noqa: E402
from database import TradeDatabase  # noqa: E402
from strategy_engine import ConfluenceEngine, compute_adx, compute_atr, compute_rsi  # noqa: E402
from telegram_bot import format_price, format_signal_message_html  # noqa: E402


def synth_frames(seed: int, drift: float = 0.0004, vol: float = 0.004) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    n = 1800
    ts = pd.date_range(end=datetime(2026, 11, 20, 12, tzinfo=timezone.utc), periods=n, freq="1h")
    rets = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, vol * 0.6, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.lognormal(10, 0.5, n)
    df = pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ix = df.set_index("timestamp")
    return {
        "1h": df,
        "4h": ix.resample("4h").agg(agg).dropna().reset_index(),
        "1d": ix.resample("1D").agg(agg).dropna().reset_index(),
    }


class IndicatorTests(unittest.TestCase):
    def test_rsi_bounds_and_atr_positive(self):
        f = synth_frames(1)["1h"]
        rsi = compute_rsi(f["close"])
        self.assertTrue(((rsi >= 0) & (rsi <= 100)).all())
        self.assertGreater(float(compute_atr(f).iloc[-1]), 0)

    def test_adx_in_range(self):
        adx, p, m = compute_adx(synth_frames(2)["4h"])
        self.assertTrue((adx >= 0).all() and (adx <= 100).all())

    def test_round_to_tick(self):
        self.assertEqual(round_to_tick(100.123, 0.01), 100.12)
        self.assertEqual(round_to_tick(0.00001234, 0.00000001), 0.00001234)


class EngineInvariantTests(unittest.TestCase):
    """Whatever the engine emits must be internally consistent."""

    def test_signals_are_well_formed_over_many_markets(self):
        import dataclasses
        base = BotConfig()
        # Production threshold (75) is deliberately rare on random data, so exercise the
        # full scoring + risk pipeline with a lowered bar to make the invariants meaningful.
        cfg = dataclasses.replace(
            base, thresholds=dataclasses.replace(base.thresholds, min_confluence_score=35)
        )
        engine = ConfluenceEngine(cfg)
        meta = SymbolMetadata("TEST/USDT:USDT", "TESTUSDT", "CRYPTO", 0.01, 2)
        emitted = 0
        for seed in range(60):
            drift = (-1) ** seed * 0.0006
            frames = synth_frames(seed, drift=drift)
            cand = engine.evaluate(meta, frames)
            if cand is None:
                continue
            emitted += 1
            self.assertGreaterEqual(cand.score, cfg.thresholds.min_confluence_score)
            self.assertLessEqual(cand.score, 100)
            self.assertGreaterEqual(cand.rr_tp2, cfg.thresholds.min_rr_tp2)
            if cand.direction == "LONG":
                self.assertTrue(cand.stop_loss < cand.entry_price < cand.tp1 < cand.tp2 < cand.tp3)
            else:
                self.assertTrue(cand.stop_loss > cand.entry_price > cand.tp1 > cand.tp2 > cand.tp3 > 0)
            risk = abs(cand.entry_price - cand.stop_loss)
            self.assertLessEqual(risk, cfg.thresholds.max_risk_atr * cand.atr + 1e-9)
            self.assertGreaterEqual(risk, cfg.thresholds.min_risk_atr * cand.atr - 1e-9)
            self.assertLessEqual(abs(cand.tp2 - cand.entry_price) / risk, cfg.thresholds.tp2_max_r + 0.05)
            self.assertEqual(sum(cand.school_scores.values()), cand.score)
            # message renders and keeps HTML safe
            msg = format_signal_message_html(cand, "• <b>x</b> & y")
            self.assertIn("&lt;b&gt;", msg)
        print(f"\n   engine emitted {emitted}/60 synthetic setups at min_score=35")
        self.assertGreater(emitted, 0, "pipeline never produced a signal; invariants untested")

    def test_insufficient_history_returns_none(self):
        engine = ConfluenceEngine(BotConfig())
        meta = SymbolMetadata("X", "X", "CRYPTO", 0.01, 2)
        f = synth_frames(3)
        f["1h"] = f["1h"].tail(100)
        self.assertIsNone(engine.evaluate(meta, f))

    def test_gold_session_filter(self):
        night = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)
        day = datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc)
        self.assertFalse(ConfluenceEngine.is_gold_session_active(night, 7, 21))
        self.assertTrue(ConfluenceEngine.is_gold_session_active(day, 7, 21))


class FormatTests(unittest.TestCase):
    def test_format_price_uses_tick(self):
        self.assertEqual(format_price(2650.5, 0.01), "2650.50")
        self.assertEqual(format_price(0.00001234, 1e-8), "0.00001234")
        self.assertEqual(format_price(65000, 0.1), "65000.0")
        self.assertEqual(format_price(1234, 1), "1234")
        self.assertNotIn("e", format_price(0.0000001, None).lower())


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TradeDatabase(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, symbol="BTCUSDT", direction="LONG"):
        return self.db.create_trade(
            symbol=symbol, asset_class="CRYPTO", direction=direction, timeframe="4H / 1H",
            regime="x", score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0,
            tp3=107.0, rr_ratio=2.0, rationale_ar="r",
        )

    def test_effective_stop_moves_to_entry_after_tp1(self):
        tid = self._mk()
        self.db.mark_entry_hit(tid)
        self.assertEqual(self.db.get_trade_by_id(tid).effective_stop, 98.0)
        self.db.mark_tp1_hit(tid)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual(t.effective_stop, 100.0)
        self.assertAlmostEqual(t.realized_r, 0.6, places=3)  # 50% * 1.2R

    def test_breakeven_exit_is_win_with_banked_r(self):
        tid = self._mk()
        self.db.mark_entry_hit(tid); self.db.mark_tp1_hit(tid); self.db.mark_sl_hit(tid)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual((t.status, t.outcome), ("CLOSED", "WIN"))
        self.assertAlmostEqual(t.realized_r, 0.6, places=3)

    def test_full_stop_is_minus_one(self):
        tid = self._mk()
        self.db.mark_entry_hit(tid); self.db.mark_sl_hit(tid)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual((t.outcome, t.realized_r), ("LOSS", -1.0))

    def test_cooldown_and_capacity_and_rollback(self):
        tid = self._mk()
        self.assertTrue(self.db.has_open_trade("BTCUSDT"))
        self.assertEqual(self.db.count_open_trades(), 1)
        self.db.delete_trade(tid)
        self.assertEqual(self.db.count_open_trades(), 0)
        self.assertFalse(self.db.in_cooldown("BTCUSDT", 4))
        tid = self._mk(); self.db.mark_entry_hit(tid); self.db.mark_sl_hit(tid)
        self.assertTrue(self.db.in_cooldown("BTCUSDT", 4))
        self.assertFalse(self.db.in_cooldown("ETHUSDT", 4))

    def test_cancelled_trade_does_not_trigger_cooldown(self):
        tid = self._mk(); self.db.cancel_trade(tid)
        self.assertFalse(self.db.in_cooldown("BTCUSDT", 4))

    def test_stats_profit_factor_and_curve(self):
        a = self._mk("AAA"); self.db.mark_entry_hit(a); self.db.mark_sl_hit(a)           # -1
        b = self._mk("BBB"); self.db.mark_entry_hit(b); self.db.mark_tp1_hit(b)
        self.db.mark_tp2_hit(b); self.db.mark_tp3_hit(b)                                  # + big
        stats = self.db.get_performance_stats(days=1)
        self.assertEqual(stats["total_closed"], 2)
        self.assertGreater(stats["profit_factor"], 1)
        curve = self.db.get_equity_curve(days=1)
        self.assertEqual(len(curve), 2)
        self.assertAlmostEqual(curve[-1]["cum_r"], stats["total_r"], delta=0.02)


class FakeNotifier:
    def __init__(self): self.replies: list[str] = []
    def send_trade_reply(self, _mid, text): self.replies.append(text)


def make_bot(db):
    from main import TradingSignalBot
    bot = TradingSignalBot.__new__(TradingSignalBot)
    bot.cfg = BotConfig()
    bot.db = db
    bot.notifier = FakeNotifier()
    bot._running = threading.Event(); bot._running.set()
    bot._last_check = {}
    return bot


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TradeDatabase(os.path.join(self.tmp.name, "t.db"))
        self.bot = make_bot(self.db)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def _active_long(self):
        tid = self.db.create_trade(
            symbol="BTCUSDT", asset_class="CRYPTO", direction="LONG", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0, tp3=107.0,
            rr_ratio=2.0, rationale_ar="r", telegram_message_id=1)
        self.db.mark_entry_hit(tid)
        return tid

    def _active_short(self):
        tid = self.db.create_trade(
            symbol="ETHUSDT", asset_class="CRYPTO", direction="SHORT", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=102.0, tp1=97.6, tp2=96.0, tp3=93.0,
            rr_ratio=2.0, rationale_ar="r", telegram_message_id=2)
        self.db.mark_entry_hit(tid)
        return tid

    def test_pending_fills_quickly_after_signal(self):
        tid = self.db.create_trade(
            symbol="BTCUSDT", asset_class="CRYPTO", direction="LONG", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0, tp3=107.0,
            rr_ratio=2.0, rationale_ar="r")
        t = self.db.get_trade_by_id(tid)
        self.assertTrue(self.bot._handle_pending(t, low=100.05, high=100.3, now=self.now))
        self.assertEqual(self.db.get_trade_by_id(tid).status, "ACTIVE")

    def test_pending_cancelled_if_price_runs_to_tp1_unfilled(self):
        tid = self.db.create_trade(
            symbol="BTCUSDT", asset_class="CRYPTO", direction="LONG", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0, tp3=107.0,
            rr_ratio=2.0, rationale_ar="r")
        t = self.db.get_trade_by_id(tid)
        late = self.now + timedelta(hours=1)
        self.assertFalse(self.bot._handle_pending(t, low=101.0, high=103.0, now=late))
        self.assertEqual(self.db.get_trade_by_id(tid).outcome, "CANCELLED")

    def test_pending_expires(self):
        tid = self.db.create_trade(
            symbol="BTCUSDT", asset_class="CRYPTO", direction="LONG", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0, tp3=107.0,
            rr_ratio=2.0, rationale_ar="r")
        t = self.db.get_trade_by_id(tid)
        later = self.now + timedelta(hours=13)
        self.bot._handle_pending(t, low=100.8, high=101.2, now=later)
        self.assertEqual(self.db.get_trade_by_id(tid).outcome, "CANCELLED")

    def test_wick_through_sl_is_caught_even_if_last_price_recovered(self):
        tid = self._active_long()
        self.bot._handle_active(self.db.get_trade_by_id(tid), low=97.5, high=101.0)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual((t.status, t.outcome), ("CLOSED", "LOSS"))

    def test_conservative_ordering_sl_before_tp_in_same_window(self):
        tid = self._active_long()
        self.bot._handle_active(self.db.get_trade_by_id(tid), low=97.9, high=103.0)
        self.assertEqual(self.db.get_trade_by_id(tid).outcome, "LOSS")

    def test_tp1_then_breakeven_stop_on_next_window(self):
        tid = self._active_long()
        self.bot._handle_active(self.db.get_trade_by_id(tid), low=100.1, high=102.6)
        t = self.db.get_trade_by_id(tid)
        self.assertTrue(t.tp1_hit); self.assertEqual(t.status, "ACTIVE")
        # price falls back through entry (but never touches the ORIGINAL stop)
        self.bot._handle_active(t, low=99.4, high=101.0)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual((t.status, t.outcome), ("CLOSED", "WIN"))
        self.assertAlmostEqual(t.realized_r, 0.6, places=3)
        self.assertIn("🛡️", self.bot.notifier.replies[-1])

    def test_no_false_breakeven_stop_in_the_same_window_as_tp1(self):
        tid = self._active_long()
        self.bot._handle_active(self.db.get_trade_by_id(tid), low=99.5, high=102.6)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual(t.status, "ACTIVE")

    def test_full_run_to_tp3_short(self):
        tid = self._active_short()
        self.bot._handle_active(self.db.get_trade_by_id(tid), low=92.0, high=99.0)
        t = self.db.get_trade_by_id(tid)
        self.assertEqual((t.status, t.outcome), ("CLOSED", "WIN"))
        self.assertTrue(t.tp1_hit and t.tp2_hit and t.tp3_hit)
        self.assertEqual(len(self.bot.notifier.replies), 3)

    def test_symbol_resolution(self):
        from main import TradingSignalBot
        class T: asset_class = "CRYPTO"; symbol = "1000PEPEUSDT"
        self.assertEqual(TradingSignalBot._resolve_ccxt_symbol(T), "1000PEPE/USDT:USDT")


class RedactionTests(unittest.TestCase):
    def test_token_scrubbed_from_tracebacks(self):
        from main import SecretRedactingFilter
        f = SecretRedactingFilter(["SECRET123"])
        try:
            raise RuntimeError("GET https://api.telegram.org/botSECRET123/sendMessage failed")
        except RuntimeError:
            rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom %s", ("SECRET123",), sys.exc_info())
        f.filter(rec)
        out = logging.Formatter("%(message)s").format(rec)
        self.assertNotIn("SECRET123", out)
        self.assertIn("REDACTED", out)


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        errs = validate_config(BotConfig())
        self.assertTrue(any("TELEGRAM_BOT_TOKEN" in e for e in errs))

    def test_exposed_api_requires_token(self):
        import dataclasses
        cfg = dataclasses.replace(BotConfig(), api_host="0.0.0.0", api_token="", telegram_bot_token="t", telegram_chat_id="c")
        self.assertTrue(any("API_TOKEN" in e for e in validate_config(cfg)))


class ApiServerTests(unittest.TestCase):
    def setUp(self):
        import dataclasses
        from api_server import start_api_server
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TradeDatabase(os.path.join(self.tmp.name, "t.db"))
        self.db.create_trade(
            symbol="BTCUSDT", asset_class="CRYPTO", direction="LONG", timeframe="x", regime="x",
            score=80, entry_price=100.0, stop_loss=98.0, tp1=102.4, tp2=104.0, tp3=107.0,
            rr_ratio=2.0, rationale_ar="<script>alert(1)</script>")
        cfg = dataclasses.replace(BotConfig(), api_host="127.0.0.1", api_port=0, api_token="tok")
        self.server = start_api_server(cfg, self.db)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()

    def _get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token: req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_and_endpoints(self):
        self.assertEqual(self._get("/api/health")[0], 200)
        self.assertEqual(self._get("/api/summary")[0], 401)
        self.assertEqual(self._get("/api/summary", "wrong")[0], 401)
        code, body = self._get("/api/trades?status=PENDING", "tok")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["trades"][0]["symbol"], "BTCUSDT")
        for p in ("/api/summary", "/api/equity", "/api/breakdown"):
            self.assertEqual(self._get(p, "tok")[0], 200)

    def test_static_and_traversal(self):
        code, body = self._get("/")
        self.assertEqual(code, 200); self.assertIn(b"ConfluenceAI", body)
        self.assertEqual(self._get("/manifest.webmanifest")[0], 200)
        self.assertIn(self._get("/..%2f..%2fconfig.py")[0], (403, 404))
        self.assertIn(self._get("/../config.py")[0], (403, 404))


if __name__ == "__main__":
    unittest.main(verbosity=2)
