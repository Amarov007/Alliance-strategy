"""Multi-School Institutional Confluence Scoring Engine (SMC, Volume/VWAP, Price Action, Regime/MTF)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from config import BotConfig
from data_fetcher import SymbolMetadata, round_to_tick

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: str  # 'HIGH' or 'LOW'


@dataclass(frozen=True)
class ZoneBox:
    index: int
    low: float
    high: float
    direction: str  # 'LONG' (bullish) or 'SHORT' (bearish)
    kind: str  # 'OB' or 'FVG'


@dataclass(frozen=True)
class StructureResult:
    trend: str  # 'BULLISH', 'BEARISH', 'NEUTRAL'
    bos_direction: str | None
    choch_direction: str | None
    swing_highs: list[SwingPoint]
    swing_lows: list[SwingPoint]


@dataclass(frozen=True)
class TradeSignalCandidate:
    """Complete institutional trade setup produced by the Confluence Engine."""

    symbol: str
    display_name: str
    asset_class: str
    direction: str  # 'LONG' or 'SHORT'
    timeframe: str
    regime_code: str  # 'TRENDING_BULLISH', 'TRENDING_BEARISH', 'RANGING'
    regime_ar: str
    score: int
    school_scores: dict[str, int]
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr_tp2: float
    atr: float
    reasons_ar: list[str]
    indicators_summary: dict[str, Any] = field(default_factory=dict)
    tick_size: float = 0.0001

    def to_ai_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.display_name,
            "asset_class": self.asset_class,
            "direction": self.direction,
            "timeframe": self.timeframe,
            "regime": self.regime_code,
            "confluence_score": self.score,
            "school_scores": self.school_scores,
            "entry": self.entry_price,
            "stop_loss": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "rr_to_tp2": self.rr_tp2,
            "atr_1h": round(self.atr, 6),
            "metrics": self.indicators_summary,
            "signals_detected_ar": self.reasons_ar,
        }


# ---------------------------------------------------------------------------
# Technical Indicator Primitives (Wilder / Institutional implementations)
# ---------------------------------------------------------------------------

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def compute_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_adx(
    df: pd.DataFrame, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    high = df["high"]
    low = df["low"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = compute_atr(df, period=period).replace(0.0, np.nan)
    plus_di = 100.0 * (
        pd.Series(plus_dm, index=df.index)
        .ewm(alpha=1.0 / period, adjust=False, min_periods=period)
        .mean()
        / atr
    )
    minus_di = 100.0 * (
        pd.Series(minus_dm, index=df.index)
        .ewm(alpha=1.0 / period, adjust=False, min_periods=period)
        .mean()
        / atr
    )
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def compute_anchored_vwap(df: pd.DataFrame) -> pd.Series:
    """Compute UTC-day anchored VWAP on intraday OHLCV data."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].clip(lower=0.0)
    if float(vol.sum()) <= 0:
        # Fallback when an FX spot feed reports zero volume
        return typical_price.rolling(window=24, min_periods=1).mean()

    dates = pd.to_datetime(df["timestamp"], utc=True).dt.date
    pv = typical_price * vol
    cum_pv = pv.groupby(dates).cumsum()
    cum_vol = vol.groupby(dates).cumsum().replace(0.0, np.nan)
    vwap = cum_pv / cum_vol
    return vwap.fillna(typical_price)


# ---------------------------------------------------------------------------
# Confluence Engine
# ---------------------------------------------------------------------------

class ConfluenceEngine:
    """Evaluates 4 institutional trading schools and computes a 0-100 confluence score."""

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.w = cfg.weights
        self.t = cfg.thresholds

    @staticmethod
    def is_gold_session_active(now_utc: datetime | None = None, start_h: int = 7, end_h: int = 21) -> bool:
        """Return True during London & New York liquidity hours (avoiding Asian chop)."""
        current = now_utc or datetime.now(timezone.utc)
        hour = current.hour
        if start_h < end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h

    # ------------------------------------------------------------------
    # School 1 Helpers: SMC / ICT (BOS, CHoCH, FVG, OB, Liquidity Sweep)
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_swings(
        df: pd.DataFrame, left: int = 3, right: int = 3
    ) -> tuple[list[SwingPoint], list[SwingPoint]]:
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        n = len(df)
        swing_highs: list[SwingPoint] = []
        swing_lows: list[SwingPoint] = []

        for i in range(left, n - right):
            left_h = highs[i - left : i]
            right_h = highs[i + 1 : i + right + 1]
            if highs[i] > np.max(left_h) and highs[i] >= np.max(right_h):
                swing_highs.append(SwingPoint(index=i, price=float(highs[i]), kind="HIGH"))

            left_l = lows[i - left : i]
            right_l = lows[i + 1 : i + right + 1]
            if lows[i] < np.min(left_l) and lows[i] <= np.min(right_l):
                swing_lows.append(SwingPoint(index=i, price=float(lows[i]), kind="LOW"))

        return swing_highs, swing_lows

    def _analyze_market_structure(self, df: pd.DataFrame) -> StructureResult:
        swing_highs, swing_lows = self._detect_swings(
            df, left=self.t.swing_left, right=self.t.swing_right
        )
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return StructureResult("NEUTRAL", None, None, swing_highs, swing_lows)

        closes = df["close"].to_numpy()
        trend = "NEUTRAL"
        bos_dir: str | None = None
        choch_dir: str | None = None

        # Determine baseline structural trend from last two confirmed swings
        if swing_highs[-1].price > swing_highs[-2].price and swing_lows[-1].price > swing_lows[-2].price:
            trend = "BULLISH"
        elif swing_highs[-1].price < swing_highs[-2].price and swing_lows[-1].price < swing_lows[-2].price:
            trend = "BEARISH"

        last_sh = swing_highs[-1]
        prev_sh = swing_highs[-2]
        last_sl = swing_lows[-1]
        prev_sl = swing_lows[-2]

        # Check recent closes (last 8 bars) against confirmed swing structure
        recent_start = max(last_sh.index + self.t.swing_right, last_sl.index + self.t.swing_right)
        recent_closes = closes[max(recent_start, len(closes) - 8) :]
        if len(recent_closes) == 0:
            recent_closes = closes[-3:]

        latest_close = float(closes[-1])
        broke_high = latest_close > last_sh.price or bool(np.any(recent_closes > last_sh.price))
        broke_low = latest_close < last_sl.price or bool(np.any(recent_closes < last_sl.price))

        if broke_high and not broke_low:
            if trend == "BEARISH":
                choch_dir = "LONG"
                trend = "BULLISH"
            else:
                bos_dir = "LONG"
                trend = "BULLISH"
        elif broke_low and not broke_high:
            if trend == "BULLISH":
                choch_dir = "SHORT"
                trend = "BEARISH"
            else:
                bos_dir = "SHORT"
                trend = "BEARISH"
        else:
            # Also check previous swing break if last swing was newly formed after break
            if latest_close > prev_sh.price and trend == "BULLISH":
                bos_dir = "LONG"
            elif latest_close < prev_sl.price and trend == "BEARISH":
                bos_dir = "SHORT"

        return StructureResult(
            trend=trend,
            bos_direction=bos_dir,
            choch_direction=choch_dir,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
        )

    def _detect_fvgs(self, df: pd.DataFrame, atr_series: pd.Series) -> list[ZoneBox]:
        n = len(df)
        start = max(2, n - self.t.fvg_lookback)
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        atrs = atr_series.to_numpy()
        zones: list[ZoneBox] = []

        for i in range(start, n - 1):
            atr_val = float(atrs[i]) if not np.isnan(atrs[i]) else 0.0
            min_gap = max(atr_val * self.t.fvg_min_atr_ratio, 1e-9)

            # Bullish FVG: candle[i].low > candle[i-2].high
            if lows[i] - highs[i - 2] >= min_gap:
                gap_low = float(highs[i - 2])
                gap_high = float(lows[i])
                # Mitigated only if a subsequent bar closed cleanly below gap_low
                subsequent_closes = closes[i + 1 :]
                if len(subsequent_closes) == 0 or not bool(np.any(subsequent_closes < gap_low)):
                    zones.append(ZoneBox(i, gap_low, gap_high, "LONG", "FVG"))

            # Bearish FVG: candle[i].high < candle[i-2].low
            if lows[i - 2] - highs[i] >= min_gap:
                gap_low = float(highs[i])
                gap_high = float(lows[i - 2])
                subsequent_closes = closes[i + 1 :]
                if len(subsequent_closes) == 0 or not bool(np.any(subsequent_closes > gap_high)):
                    zones.append(ZoneBox(i, gap_low, gap_high, "SHORT", "FVG"))

        return zones

    def _detect_order_blocks(self, df: pd.DataFrame, atr_series: pd.Series) -> list[ZoneBox]:
        n = len(df)
        start = max(1, n - self.t.ob_lookback)
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        atrs = atr_series.to_numpy()
        obs: list[ZoneBox] = []

        for i in range(start, n - 2):
            atr_val = float(atrs[i]) if not np.isnan(atrs[i]) and atrs[i] > 0 else 1e-6
            # Bullish OB: bearish candle followed by strong bullish displacement
            if closes[i] < opens[i]:
                impulse = float(closes[i + 1] - opens[i + 1])
                follow_high = float(max(highs[i + 1], highs[i + 2]))
                if impulse >= 1.1 * atr_val and follow_high > highs[i]:
                    ob_low = float(lows[i])
                    ob_high = float(highs[i])
                    subsequent_closes = closes[i + 2 :]
                    if len(subsequent_closes) == 0 or not bool(np.any(subsequent_closes < ob_low)):
                        obs.append(ZoneBox(i, ob_low, ob_high, "LONG", "OB"))

            # Bearish OB: bullish candle followed by strong bearish displacement
            if closes[i] > opens[i]:
                impulse = float(opens[i + 1] - closes[i + 1])
                follow_low = float(min(lows[i + 1], lows[i + 2]))
                if impulse >= 1.1 * atr_val and follow_low < lows[i]:
                    ob_low = float(lows[i])
                    ob_high = float(highs[i])
                    subsequent_closes = closes[i + 2 :]
                    if len(subsequent_closes) == 0 or not bool(np.any(subsequent_closes > ob_high)):
                        obs.append(ZoneBox(i, ob_low, ob_high, "SHORT", "OB"))

        return obs

    def _detect_liquidity_sweep(
        self, df: pd.DataFrame, structure: StructureResult, direction: str
    ) -> float | None:
        """Return swept extreme level if a recent liquidity sweep aligns with `direction`."""
        n = len(df)
        lookback_start = max(0, n - self.t.sweep_lookback)
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()

        if direction == "LONG":
            # Bullish SSL sweep: wick below a prior confirmed swing low, close back above it
            eligible_lows = [s for s in structure.swing_lows if s.index < lookback_start]
            if not eligible_lows:
                eligible_lows = structure.swing_lows[:-1]
            for swing in reversed(eligible_lows[-4:]):
                for idx in range(max(lookback_start, swing.index + self.t.swing_right), n):
                    if lows[idx] < swing.price and closes[idx] > swing.price and closes[-1] > swing.price:
                        return float(lows[idx])

        if direction == "SHORT":
            # Bearish BSL sweep: wick above a prior confirmed swing high, close back below it
            eligible_highs = [s for s in structure.swing_highs if s.index < lookback_start]
            if not eligible_highs:
                eligible_highs = structure.swing_highs[:-1]
            for swing in reversed(eligible_highs[-4:]):
                for idx in range(max(lookback_start, swing.index + self.t.swing_right), n):
                    if highs[idx] > swing.price and closes[idx] < swing.price and closes[-1] < swing.price:
                        return float(highs[idx])

        return None

    def _score_smc(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        atr_1h: pd.Series,
        direction: str,
    ) -> tuple[int, list[str], dict[str, Any]]:
        struct_4h = self._analyze_market_structure(df_4h)
        struct_1h = self._analyze_market_structure(df_1h)
        fvgs_1h = self._detect_fvgs(df_1h, atr_1h)
        obs_1h = self._detect_order_blocks(df_1h, atr_1h)

        current_price = float(df_1h["close"].iloc[-1])
        current_atr = float(atr_1h.iloc[-1])
        prox = current_atr * self.t.zone_proximity_atr

        score = 0
        reasons: list[str] = []

        # 1) BOS / CHoCH alignment on 4H & 1H (up to 12 pts)
        has_4h_struct = (
            struct_4h.bos_direction == direction
            or struct_4h.choch_direction == direction
            or (struct_4h.trend == ("BULLISH" if direction == "LONG" else "BEARISH"))
        )
        has_1h_trigger = (
            struct_1h.bos_direction == direction
            or struct_1h.choch_direction == direction
        )

        if has_4h_struct and has_1h_trigger:
            score += 12
            tag_1h = "CHoCH" if struct_1h.choch_direction == direction else "BOS"
            reasons.append(f"توافق هيكلي مؤسسي ({tag_1h}) على فريم 4H و 1H")
        elif has_1h_trigger:
            score += 8
            tag_1h = "CHoCH" if struct_1h.choch_direction == direction else "BOS"
            reasons.append(f"كسر هيكل سوق ({tag_1h}) على فريم 1H")
        elif has_4h_struct:
            score += 5
            reasons.append("توافق هيكل السوق العام على فريم 4H")

        # 2) Order Block & FVG proximity (up to 12 pts)
        matched_ob: ZoneBox | None = None
        for ob in reversed(obs_1h):
            if ob.direction == direction and (ob.low - prox) <= current_price <= (ob.high + prox):
                matched_ob = ob
                break

        matched_fvg: ZoneBox | None = None
        for fvg in reversed(fvgs_1h):
            if fvg.direction == direction and (fvg.low - prox) <= current_price <= (fvg.high + prox):
                matched_fvg = fvg
                break

        if matched_ob and matched_fvg:
            score += 12
            reasons.append("ارتكاز السعر داخل منطقة Order Block متطابقة مع فجوة سعرية FVG")
        elif matched_ob:
            score += 8
            reasons.append("تفاعل السعر مع منطقة عرض/طلب مؤسسية (Order Block)")
        elif matched_fvg:
            score += 7
            reasons.append("ملء فجوة قيمة عادلة (FVG) نشطة على فريم 1H")

        # 3) Liquidity Sweep (up to 11 pts)
        sweep_1h = self._detect_liquidity_sweep(df_1h, struct_1h, direction)
        sweep_4h = self._detect_liquidity_sweep(df_4h, struct_4h, direction)
        sweep_level = sweep_1h if sweep_1h is not None else sweep_4h

        if sweep_1h is not None and sweep_4h is not None:
            score += 11
            reasons.append("سحب سيولة مزدوج (Liquidity Sweep) وإغلاق قوي داخل النطاق على 4H و 1H")
        elif sweep_level is not None:
            score += 9
            tf_lbl = "1H" if sweep_1h is not None else "4H"
            reasons.append(f"سحب سيولة كاذب (Liquidity Sweep) لقمة/قاع سابق على فريم {tf_lbl}")

        score = min(score, self.w.smc)
        meta = {
            "struct_4h": struct_4h,
            "struct_1h": struct_1h,
            "matched_ob": matched_ob,
            "matched_fvg": matched_fvg,
            "sweep_level": sweep_level,
            "has_sweep": sweep_level is not None,
        }
        return score, reasons, meta

    # ------------------------------------------------------------------
    # School 2: Volume & Liquidity Flow (RVOL + VWAP) — Weight: 25 pts
    # ------------------------------------------------------------------

    def _score_volume_vwap(
        self, df_1h: pd.DataFrame, direction: str
    ) -> tuple[int, list[str], dict[str, Any]]:
        vol = df_1h["volume"]
        vwap = compute_anchored_vwap(df_1h)
        current_close = float(df_1h["close"].iloc[-1])
        current_vwap = float(vwap.iloc[-1])

        score = 0
        reasons: list[str] = []

        # RVOL check on latest 3 candles (capturing sweep/trigger volume surge)
        rvol_val = 0.0
        has_volume = float(vol.sum()) > 0
        if has_volume:
            vol_sma = vol.rolling(window=self.t.rvol_period, min_periods=5).mean().shift(1)
            rvol_series = (vol / vol_sma.replace(0.0, np.nan)).fillna(0.0)
            rvol_val = float(rvol_series.iloc[-3:].max())

            if rvol_val >= self.t.rvol_institutional:
                score += 13
                reasons.append(f"تدفق سيولة مؤسسية مرتفع جداً (RVOL = {rvol_val:.2f}x)")
            elif rvol_val >= self.t.rvol_strong:
                score += 10
                reasons.append(f"تأكيد حجم تداول نسبي قوي عند منطقة الدخول (RVOL = {rvol_val:.2f}x)")
            elif rvol_val >= self.t.rvol_moderate:
                score += 5
                reasons.append(f"حجم تداول نسبي مستقر (RVOL = {rvol_val:.2f}x)")

        # VWAP Discount / Premium confirmation (up to 12 pts)
        vwap_diff_pct = ((current_close - current_vwap) / current_vwap) * 100.0 if current_vwap > 0 else 0.0
        if direction == "LONG":
            if current_close <= current_vwap:
                score += 12
                reasons.append(
                    f"السعر في منطقة الخصم (Discount) أسفل متوسط VWAP ({vwap_diff_pct:+.2f}%)"
                )
            elif vwap_diff_pct <= 0.35:
                score += 7
                reasons.append("ارتكاز السعر فوق خط VWAP مباشرة كدعم ديناميكي")
        else:
            if current_close >= current_vwap:
                score += 12
                reasons.append(
                    f"السعر في منطقة العلاوة السعرية (Premium) أعلى متوسط VWAP ({vwap_diff_pct:+.2f}%)"
                )
            elif vwap_diff_pct >= -0.35:
                score += 7
                reasons.append("رفض السعر أسفل خط VWAP كمقاومة ديناميكية")

        score = min(score, self.w.volume_vwap)
        return score, reasons, {
            "rvol": round(rvol_val, 2),
            "vwap": round(current_vwap, 6),
            "vwap_diff_pct": round(vwap_diff_pct, 3),
        }

    # ------------------------------------------------------------------
    # School 3: Price Action & Momentum (Divergence + Reversal Candle) — 20 pts
    # ------------------------------------------------------------------

    def _detect_divergence(
        self, df: pd.DataFrame, direction: str
    ) -> tuple[bool, str | None]:
        if len(df) < 40:
            return False, None

        sub = df.tail(self.t.divergence_lookback).reset_index(drop=True)
        rsi = compute_rsi(sub["close"], self.t.rsi_period).to_numpy()
        _, _, macd_hist = compute_macd(
            sub["close"], self.t.macd_fast, self.t.macd_slow, self.t.macd_signal
        )
        hist_arr = macd_hist.to_numpy()

        swing_highs, swing_lows = self._detect_swings(sub, left=2, right=2)

        if direction == "LONG" and len(swing_lows) >= 2:
            l1, l2 = swing_lows[-2], swing_lows[-1]
            # Must be reasonably recent
            if (len(sub) - 1 - l2.index) <= 10 and l2.price < l1.price:
                rsi_bull = rsi[l2.index] > rsi[l1.index] + 1.5
                macd_bull = hist_arr[l2.index] > hist_arr[l1.index]
                if rsi_bull and macd_bull:
                    return True, "RSI + MACD"
                if rsi_bull:
                    return True, "RSI"
                if macd_bull:
                    return True, "MACD"

        if direction == "SHORT" and len(swing_highs) >= 2:
            h1, h2 = swing_highs[-2], swing_highs[-1]
            if (len(sub) - 1 - h2.index) <= 10 and h2.price > h1.price:
                rsi_bear = rsi[h2.index] < rsi[h1.index] - 1.5
                macd_bear = hist_arr[h2.index] < hist_arr[h1.index]
                if rsi_bear and macd_bear:
                    return True, "RSI + MACD"
                if rsi_bear:
                    return True, "RSI"
                if macd_bear:
                    return True, "MACD"

        return False, None

    @staticmethod
    def _detect_reversal_candle(df_1h: pd.DataFrame, direction: str) -> str | None:
        """Check last 2 closed/recent candles for Pin Bar or Engulfing pattern."""
        if len(df_1h) < 3:
            return None

        for offset in (-1, -2):
            c = df_1h.iloc[offset]
            p = df_1h.iloc[offset - 1]
            o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
            po, ph, pl, pc = float(p["open"]), float(p["high"]), float(p["low"]), float(p["close"])

            rng = h - l
            if rng <= 0:
                continue
            body = abs(cl - o)
            upper_wick = h - max(o, cl)
            lower_wick = min(o, cl) - l

            if direction == "LONG":
                # Bullish Pin Bar (hammer)
                if lower_wick >= max(2.0 * body, 0.55 * rng) and upper_wick <= 0.25 * rng:
                    return "Pin Bar صاعد"
                # Bullish Engulfing
                if pc < po and cl > o and cl >= po and o <= pc and body >= 0.45 * rng:
                    return "ابتلاع شرائي (Bullish Engulfing)"
            else:
                # Bearish Pin Bar (shooting star)
                if upper_wick >= max(2.0 * body, 0.55 * rng) and lower_wick <= 0.25 * rng:
                    return "Pin Bar هابط"
                # Bearish Engulfing
                if pc > po and cl < o and cl <= po and o >= pc and body >= 0.45 * rng:
                    return "ابتلاع بيعي (Bearish Engulfing)"

        return None

    def _score_pa_divergence(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        atr_1h: pd.Series,
        direction: str,
        has_smc_zone: bool,
    ) -> tuple[int, list[str], dict[str, Any]]:
        score = 0
        reasons: list[str] = []

        div_4h, div_4h_src = self._detect_divergence(df_4h, direction)
        div_1d, div_1d_src = self._detect_divergence(df_1d, direction)

        if div_4h and div_1d:
            score += 10
            reasons.append(f"دايفرجنس {div_4h_src} مؤكد على فريم 4H و 1D")
        elif div_1d:
            score += 9
            reasons.append(f"دايفرجنس ({div_1d_src}) إيجابي/سلبي على الفريم اليومي 1D")
        elif div_4h:
            score += 8
            reasons.append(f"دايفرجنس ({div_4h_src}) واضح على فريم 4H")

        # Dynamic S/R check (EMA 20 / EMA 50 / EMA 200 or SMC zone)
        close_1h = df_1h["close"]
        ema20 = float(compute_ema(close_1h, self.t.ema_fast_period).iloc[-1])
        ema50 = float(compute_ema(close_1h, self.t.ema_mid_period).iloc[-1])
        ema200 = float(compute_ema(close_1h, self.t.ema_trend_period).iloc[-1])
        cur_price = float(close_1h.iloc[-1])
        cur_atr = float(atr_1h.iloc[-1])

        near_dynamic_sr = has_smc_zone or any(
            abs(cur_price - lvl) <= 0.75 * cur_atr
            for lvl in (ema20, ema50, ema200)
            if not np.isnan(lvl)
        )

        candle_pattern = self._detect_reversal_candle(df_1h, direction)
        if candle_pattern and near_dynamic_sr:
            score += 10
            reasons.append(f"شمعة انعكاسية ({candle_pattern}) عند مستوى دعم/مقاومة ديناميكي")
        elif candle_pattern:
            score += 5
            reasons.append(f"تشكّل شمعة انعكاسية ({candle_pattern}) على فريم 1H")

        rsi_4h = float(compute_rsi(df_4h["close"], self.t.rsi_period).iloc[-1])
        score = min(score, self.w.pa_divergence)
        return score, reasons, {
            "has_divergence": div_4h or div_1d,
            "divergence_tf": "1D+4H" if (div_4h and div_1d) else ("1D" if div_1d else ("4H" if div_4h else None)),
            "candle_pattern": candle_pattern,
            "rsi_4h": round(rsi_4h, 2),
        }

    # ------------------------------------------------------------------
    # School 4: Adaptive Market Regime & Multi-Timeframe Filter — 20 pts
    # ------------------------------------------------------------------

    def _classify_regime(self, df_4h: pd.DataFrame) -> tuple[str, str, float, float]:
        adx, plus_di, minus_di = compute_adx(df_4h, self.t.adx_period)
        ema200 = compute_ema(df_4h["close"], self.t.ema_trend_period)

        adx_val = float(adx.iloc[-1])
        p_di = float(plus_di.iloc[-1])
        m_di = float(minus_di.iloc[-1])
        close_4h = float(df_4h["close"].iloc[-1])
        ema200_val = float(ema200.iloc[-1]) if not np.isnan(ema200.iloc[-1]) else close_4h

        if adx_val >= self.t.adx_trend_threshold:
            if close_4h > ema200_val and p_di >= m_di * 0.9:
                return "TRENDING_BULLISH", "اتجاه صاعد (Trending Bullish)", adx_val, ema200_val
            if close_4h < ema200_val and m_di >= p_di * 0.9:
                return "TRENDING_BEARISH", "اتجاه هابط (Trending Bearish)", adx_val, ema200_val

        return "RANGING", "نطاق عرضي (Ranging)", adx_val, ema200_val

    def _score_regime_mtf(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        regime_code: str,
        adx_4h: float,
        direction: str,
        has_sweep: bool,
        has_divergence: bool,
    ) -> tuple[bool, int, list[str]]:
        """Apply adaptive regime gate and score MTF alignment."""
        ema50_1d = float(compute_ema(df_1d["close"], self.t.ema_mid_period).iloc[-1])
        close_1d = float(df_1d["close"].iloc[-1])
        ema50_1h = float(compute_ema(df_1h["close"], self.t.ema_mid_period).iloc[-1])
        close_1h = float(df_1h["close"].iloc[-1])

        if regime_code == "TRENDING_BULLISH":
            if direction != "LONG":
                return False, 0, []
            score = 12
            reasons = [f"فلتر النظام: اتجاه صاعد مؤكد (ADX={adx_4h:.1f} وفوق EMA200)"]
            if close_1d > ema50_1d and close_1h > ema50_1h:
                score += 8
                reasons.append("توافق الزخم متعدد الفريمات (1D + 4H + 1H)")
            elif close_1d > ema50_1d or close_1h > ema50_1h:
                score += 4
            return True, min(score, self.w.regime_mtf), reasons

        if regime_code == "TRENDING_BEARISH":
            if direction != "SHORT":
                return False, 0, []
            score = 12
            reasons = [f"فلتر النظام: اتجاه هابط مؤكد (ADX={adx_4h:.1f} وأسفل EMA200)"]
            if close_1d < ema50_1d and close_1h < ema50_1h:
                score += 8
                reasons.append("توافق الزخم الهابط متعدد الفريمات (1D + 4H + 1H)")
            elif close_1d < ema50_1d or close_1h < ema50_1h:
                score += 4
            return True, min(score, self.w.regime_mtf), reasons

        # RANGING market: strictly allow liquidity-sweep reversals & divergences only
        if not (has_sweep or has_divergence):
            return False, 0, []

        score = 10
        reasons = [f"تكيّف النظام العرضي (ADX={adx_4h:.1f}): استهداف انعكاس من أطراف النطاق"]
        if has_sweep and has_divergence:
            score += 10
            reasons.append("تطابق انعكاس النطاق العرضي مع سحب السيولة والدايفرجنس")
        else:
            score += 6
        return True, min(score, self.w.regime_mtf), reasons

    # ------------------------------------------------------------------
    # Risk Management: SL behind OB/Sweep + 0.5*ATR, Structural TPs, R:R >= 1:2
    # ------------------------------------------------------------------

    def _compute_risk_levels(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        direction: str,
        atr_val: float,
        tick_size: float,
        smc_meta: dict[str, Any],
    ) -> tuple[float, float, float, float, float, float] | None:
        entry = round_to_tick(float(df_1h["close"].iloc[-1]), tick_size)
        buffer = self.t.sl_atr_buffer * atr_val
        matched_ob: ZoneBox | None = smc_meta["matched_ob"]
        sweep_level: float | None = smc_meta["sweep_level"]
        struct_1h: StructureResult = smc_meta["struct_1h"]
        struct_4h: StructureResult = smc_meta["struct_4h"]

        if direction == "LONG":
            anchors = []
            if matched_ob is not None:
                anchors.append(matched_ob.low)
            if sweep_level is not None:
                anchors.append(sweep_level)
            if not anchors:
                recent_lows = [s.price for s in struct_1h.swing_lows if s.price < entry]
                if recent_lows:
                    anchors.append(recent_lows[-1])
                else:
                    anchors.append(float(df_1h["low"].tail(10).min()))

            sl_raw = min(anchors) - buffer
            # Sanity cap if anchor is unusually far away (> 3.5 ATR)
            if (entry - sl_raw) > 3.5 * atr_val and sweep_level is not None and sweep_level < entry:
                sl_raw = sweep_level - buffer
            sl = round_to_tick(sl_raw, tick_size)
            if sl >= entry:
                return None
            risk = entry - sl
            if not (self.t.min_risk_atr * atr_val <= risk <= self.t.max_risk_atr * atr_val):
                logger.debug("Rejected LONG: risk %.6f outside ATR band.", risk)
                return None

            # Opposing liquidity pools (swing highs above entry)
            opposing = sorted(
                {
                    s.price
                    for s in (struct_1h.swing_highs + struct_4h.swing_highs)
                    if s.price > entry + 0.5 * risk
                }
            )
            opposing = [lvl for lvl in opposing if lvl > entry]

            tp1_min = entry + self.t.tp1_min_r * risk
            tp2_min = entry + self.t.tp2_min_r * risk
            tp3_min = entry + self.t.tp3_min_r * risk

            tp1_candidates = [lvl for lvl in opposing if tp1_min <= lvl < tp2_min]
            tp1_raw = tp1_candidates[0] if tp1_candidates else max(tp1_min, entry + 1.2 * risk)

            tp2_candidates = [
                lvl for lvl in opposing if tp2_min <= lvl <= entry + self.t.tp2_max_r * risk
            ]
            tp2_raw = tp2_candidates[0] if tp2_candidates else tp2_min
            if tp2_raw <= tp1_raw:
                tp2_raw = max(tp2_min, tp1_raw + 0.8 * risk)

            tp3_candidates = [
                lvl
                for lvl in opposing
                if tp2_raw + 0.5 * risk < lvl <= entry + self.t.tp3_max_r * risk
            ]
            tp3_raw = tp3_candidates[0] if tp3_candidates else max(tp3_min, tp2_raw + 1.2 * risk)

            tp1 = round_to_tick(tp1_raw, tick_size)
            tp2 = round_to_tick(tp2_raw, tick_size)
            tp3 = round_to_tick(tp3_raw, tick_size)

        else:
            anchors = []
            if matched_ob is not None:
                anchors.append(matched_ob.high)
            if sweep_level is not None:
                anchors.append(sweep_level)
            if not anchors:
                recent_highs = [s.price for s in struct_1h.swing_highs if s.price > entry]
                if recent_highs:
                    anchors.append(recent_highs[-1])
                else:
                    anchors.append(float(df_1h["high"].tail(10).max()))

            sl_raw = max(anchors) + buffer
            if (sl_raw - entry) > 3.5 * atr_val and sweep_level is not None and sweep_level > entry:
                sl_raw = sweep_level + buffer
            sl = round_to_tick(sl_raw, tick_size)
            if sl <= entry:
                return None
            risk = sl - entry
            if not (self.t.min_risk_atr * atr_val <= risk <= self.t.max_risk_atr * atr_val):
                logger.debug("Rejected SHORT: risk %.6f outside ATR band.", risk)
                return None

            # Opposing liquidity pools (swing lows below entry)
            opposing = sorted(
                {
                    s.price
                    for s in (struct_1h.swing_lows + struct_4h.swing_lows)
                    if s.price < entry - 0.5 * risk
                },
                reverse=True,
            )
            opposing = [lvl for lvl in opposing if lvl < entry]

            tp1_min = entry - self.t.tp1_min_r * risk
            tp2_min = entry - self.t.tp2_min_r * risk
            tp3_min = entry - self.t.tp3_min_r * risk

            tp1_candidates = [lvl for lvl in opposing if tp2_min < lvl <= tp1_min]
            tp1_raw = tp1_candidates[0] if tp1_candidates else min(tp1_min, entry - 1.2 * risk)

            tp2_candidates = [
                lvl for lvl in opposing if entry - self.t.tp2_max_r * risk <= lvl <= tp2_min
            ]
            tp2_raw = tp2_candidates[0] if tp2_candidates else tp2_min
            if tp2_raw >= tp1_raw:
                tp2_raw = min(tp2_min, tp1_raw - 0.8 * risk)

            tp3_candidates = [
                lvl
                for lvl in opposing
                if entry - self.t.tp3_max_r * risk <= lvl < tp2_raw - 0.5 * risk
            ]
            tp3_raw = tp3_candidates[0] if tp3_candidates else min(tp3_min, tp2_raw - 1.2 * risk)

            tp1 = round_to_tick(tp1_raw, tick_size)
            tp2 = round_to_tick(tp2_raw, tick_size)
            tp3 = round_to_tick(tp3_raw, tick_size)

            if tp3 <= 0:
                return None

        if risk <= 0:
            return None

        if direction == "LONG":
            ordered = sl < entry < tp1 < tp2 < tp3
        else:
            ordered = sl > entry > tp1 > tp2 > tp3 > 0
        if not ordered:
            logger.debug("Rejected %s: levels not strictly ordered after tick rounding.", direction)
            return None

        rr_tp2 = round(abs(tp2 - entry) / risk, 2)
        if rr_tp2 < self.t.min_rr_tp2:
            logger.debug("Rejected setup: RR to TP2 (%.2f) < %.2f", rr_tp2, self.t.min_rr_tp2)
            return None

        return entry, sl, tp1, tp2, tp3, rr_tp2

    # ------------------------------------------------------------------
    # Public Evaluation Entry Point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        meta: SymbolMetadata,
        frames: dict[str, pd.DataFrame],
        now_utc: datetime | None = None,
    ) -> TradeSignalCandidate | None:
        """Score a symbol across all 4 institutional schools and return a validated signal or None."""
        # Gold session liquidity filter
        if meta.asset_class == "GOLD":
            if not self.is_gold_session_active(
                now_utc=now_utc,
                start_h=self.t.gold_session_start_utc,
                end_h=self.t.gold_session_end_utc,
            ):
                logger.info("Skipping %s: outside London/NY liquidity session.", meta.display_name)
                return None

        df_1h = frames.get("1h")
        df_4h = frames.get("4h")
        df_1d = frames.get("1d")
        if df_1h is None or df_4h is None or df_1d is None:
            return None
        if len(df_1h) < 210 or len(df_4h) < 210 or len(df_1d) < 55:
            logger.debug("Insufficient candles for %s", meta.display_name)
            return None

        atr_1h = compute_atr(df_1h, self.t.atr_period)
        current_atr = float(atr_1h.iloc[-1])
        if np.isnan(current_atr) or current_atr <= 0:
            return None

        regime_code, regime_ar, adx_4h, ema200_4h = self._classify_regime(df_4h)

        best_candidate: TradeSignalCandidate | None = None
        for direction in ("LONG", "SHORT"):
            smc_score, smc_reasons, smc_meta = self._score_smc(
                df_1h, df_4h, atr_1h, direction
            )
            vol_score, vol_reasons, vol_meta = self._score_volume_vwap(
                df_1h, direction
            )
            has_smc_zone = (smc_meta["matched_ob"] is not None) or (smc_meta["matched_fvg"] is not None)
            pa_score, pa_reasons, pa_meta = self._score_pa_divergence(
                df_1h, df_4h, df_1d, atr_1h, direction, has_smc_zone
            )
            allowed, regime_score, regime_reasons = self._score_regime_mtf(
                df_1h=df_1h,
                df_4h=df_4h,
                df_1d=df_1d,
                regime_code=regime_code,
                adx_4h=adx_4h,
                direction=direction,
                has_sweep=bool(smc_meta["has_sweep"]),
                has_divergence=bool(pa_meta["has_divergence"]),
            )
            if not allowed:
                continue

            total_score = smc_score + vol_score + pa_score + regime_score
            if total_score < self.t.min_confluence_score:
                continue

            levels = self._compute_risk_levels(
                df_1h=df_1h,
                df_4h=df_4h,
                direction=direction,
                atr_val=current_atr,
                tick_size=meta.tick_size,
                smc_meta=smc_meta,
            )
            if levels is None:
                continue

            entry, sl, tp1, tp2, tp3, rr_tp2 = levels
            all_reasons = smc_reasons + vol_reasons + pa_reasons + regime_reasons

            candidate = TradeSignalCandidate(
                symbol=meta.symbol,
                display_name=meta.display_name,
                asset_class=meta.asset_class,
                direction=direction,
                timeframe="4H / 1H",
                regime_code=regime_code,
                regime_ar=regime_ar,
                score=total_score,
                school_scores={
                    "smc": smc_score,
                    "volume_vwap": vol_score,
                    "pa_divergence": pa_score,
                    "regime_mtf": regime_score,
                },
                entry_price=entry,
                stop_loss=sl,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                rr_tp2=rr_tp2,
                atr=current_atr,
                reasons_ar=all_reasons,
                tick_size=meta.tick_size,
                indicators_summary={
                    "adx_4h": round(adx_4h, 2),
                    "ema200_4h": round(ema200_4h, 4),
                    "rvol_1h": vol_meta["rvol"],
                    "vwap_1h": vol_meta["vwap"],
                    "vwap_diff_pct": vol_meta["vwap_diff_pct"],
                    "rsi_4h": pa_meta["rsi_4h"],
                    "divergence_tf": pa_meta["divergence_tf"],
                    "candle_pattern": pa_meta["candle_pattern"],
                    "has_sweep": smc_meta["has_sweep"],
                    "has_ob": smc_meta["matched_ob"] is not None,
                    "has_fvg": smc_meta["matched_fvg"] is not None,
                },
            )

            if best_candidate is None or candidate.score > best_candidate.score:
                best_candidate = candidate

        return best_candidate
