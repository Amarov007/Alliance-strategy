"""Walk-forward backtester for the Confluence engine.

Replays history bar by bar (no look-ahead: each decision only sees candles that had closed),
then simulates the SAME trade management the live bot uses:
  50% off at TP1 -> stop to breakeven -> 25% at TP2 -> 25% at TP3,
  conservative intrabar ordering (if SL and a target print in one candle, SL wins).

Usage:
    python backtest.py --csv data/BTCUSDT_1h.csv --symbol BTCUSDT --tick 0.1
    python backtest.py --exchange BTC/USDT:USDT --bars 6000          # needs ccxt + internet
    python backtest.py --csv ... --min-score 70 --step 1 --max-hold 96

CSV columns: timestamp (ISO or ms), open, high, low, close, volume  (1-hour candles).
4H and 1D frames are derived from the 1H data, so only one file is needed.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

import numpy as np
import pandas as pd

from config import BotConfig
from data_fetcher import SymbolMetadata
from strategy_engine import ConfluenceEngine, TradeSignalCandidate

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = df["timestamp"]
    df["timestamp"] = pd.to_datetime(ts, unit="ms", utc=True) if pd.api.types.is_numeric_dtype(ts) else pd.to_datetime(ts, utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].sort_values("timestamp").reset_index(drop=True)


def load_ccxt(symbol: str, bars: int) -> pd.DataFrame:  # pragma: no cover - needs network
    import ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since = ex.milliseconds() - bars * 3600_000
    rows: list[list[float]] = []
    while len(rows) < bars:
        chunk = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        if not chunk:
            break
        rows.extend(chunk)
        since = chunk[-1][0] + 3600_000
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates("timestamp").iloc[:-1].reset_index(drop=True)  # drop forming bar


def frames_at(df1h: pd.DataFrame, end: int) -> dict[str, pd.DataFrame]:
    h1 = df1h.iloc[: end + 1]
    ix = h1.set_index("timestamp")
    h4 = ix.resample("4h").agg(AGG).dropna().reset_index()
    d1 = ix.resample("1D").agg(AGG).dropna().reset_index()
    # only CLOSED higher-timeframe bars (mirrors DROP_FORMING_CANDLE in live mode)
    last = h1["timestamp"].iloc[-1] + pd.Timedelta(hours=1)
    if len(h4) and h4["timestamp"].iloc[-1] + pd.Timedelta(hours=4) > last:
        h4 = h4.iloc[:-1]
    if len(d1) and d1["timestamp"].iloc[-1] + pd.Timedelta(days=1) > last:
        d1 = d1.iloc[:-1]
    return {"1h": h1.reset_index(drop=True), "4h": h4.reset_index(drop=True), "1d": d1.reset_index(drop=True)}


def simulate(c: TradeSignalCandidate, future: pd.DataFrame, max_hold: int) -> tuple[float, str, int] | None:
    """Return (realized_R, outcome, bars_held) or None when the entry never fills."""
    long = c.direction == "LONG"
    risk = abs(c.entry_price - c.stop_loss)
    r = lambda lvl: abs(lvl - c.entry_price) / risk  # noqa: E731
    hi, lo, cl = future["high"].to_numpy(), future["low"].to_numpy(), future["close"].to_numpy()

    filled_at = None
    for i in range(min(3, len(future))):  # entry must fill within 3 bars
        if (lo[i] <= c.entry_price) if long else (hi[i] >= c.entry_price):
            filled_at = i
            break
    if filled_at is None:
        return None

    tp1 = tp2 = False
    banked = 0.0
    for i in range(filled_at, min(len(future), filled_at + max_hold)):
        hit = lambda lvl: (hi[i] >= lvl) if long else (lo[i] <= lvl)  # noqa: E731
        stop_hit = lambda lvl: (lo[i] <= lvl) if long else (hi[i] >= lvl)  # noqa: E731
        progressed = False
        if not tp1:
            if stop_hit(c.stop_loss):
                return -1.0, "LOSS", i - filled_at
            if hit(c.tp1):
                tp1, banked, progressed = True, 0.5 * r(c.tp1), True
            else:
                continue
        if not tp2 and hit(c.tp2):
            tp2, banked, progressed = True, banked + 0.25 * r(c.tp2), True
        if tp2 and hit(c.tp3):
            return banked + 0.25 * r(c.tp3), "WIN", i - filled_at
        if not progressed and stop_hit(c.entry_price):
            return banked, "WIN", i - filled_at

    # time stop: close the remainder at market
    end = min(len(future), filled_at + max_hold) - 1
    remaining = 0.5 if not tp1 else (0.25 if tp2 else 0.5)
    unreal = ((cl[end] - c.entry_price) if long else (c.entry_price - cl[end])) / risk
    total = banked + remaining * unreal
    return total, ("WIN" if total > 0 else "LOSS"), end - filled_at


def run(df: pd.DataFrame, meta: SymbolMetadata, cfg: BotConfig, step: int, max_hold: int, cooldown: int) -> pd.DataFrame:
    engine = ConfluenceEngine(cfg)
    trades: list[dict] = []
    warmup = 215 * 4  # enough 4H bars for EMA200
    i, blocked_until = warmup, -1
    while i < len(df) - 2:
        if i > blocked_until:
            cand = engine.evaluate(meta, frames_at(df, i), now_utc=df["timestamp"].iloc[i].to_pydatetime())
            if cand is not None:
                res = simulate(cand, df.iloc[i + 1 :].reset_index(drop=True), max_hold)
                if res is not None:
                    realized, outcome, held = res
                    trades.append({"time": df["timestamp"].iloc[i], "dir": cand.direction, "score": cand.score,
                                   "rr": cand.rr_tp2, "R": round(realized, 3), "outcome": outcome, "bars": held})
                    blocked_until = i + held + cooldown  # one trade at a time per symbol + cooldown
        i += step
    return pd.DataFrame(trades)


def report(t: pd.DataFrame) -> None:
    if t.empty:
        print("No trades were generated for this period / threshold.")
        return
    wins, n = int((t["R"] > 0).sum()), len(t)
    gw, gl = t.loc[t["R"] > 0, "R"].sum(), -t.loc[t["R"] < 0, "R"].sum()
    curve = t["R"].cumsum()
    dd = float((curve.cummax() - curve).max())
    print(f"Trades: {n} | Win rate: {100 * wins / n:.1f}% | Total: {t['R'].sum():+.2f}R | Avg: {t['R'].mean():+.3f}R")
    print(f"Profit factor: {gw / gl if gl > 0 else float('inf'):.2f} | Max drawdown: {dd:.2f}R | Avg bars held: {t['bars'].mean():.1f}")
    for d, g in t.groupby("dir"):
        print(f"  {d:5s}: {len(g):3d} trades | {100 * (g['R'] > 0).mean():.0f}% win | {g['R'].sum():+.2f}R")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--exchange", help="ccxt symbol, e.g. BTC/USDT:USDT")
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--symbol", default="TEST")
    ap.add_argument("--tick", type=float, default=0.01)
    ap.add_argument("--min-score", type=int)
    ap.add_argument("--min-rr", type=float)
    ap.add_argument("--step", type=int, default=1, help="evaluate every N hourly bars")
    ap.add_argument("--max-hold", type=int, default=96)
    ap.add_argument("--cooldown", type=int, default=4)
    ap.add_argument("--out", help="write trades to CSV")
    a = ap.parse_args()

    if not a.csv and not a.exchange:
        ap.error("provide --csv or --exchange")
    df = load_csv(a.csv) if a.csv else load_ccxt(a.exchange, a.bars)

    base = BotConfig()
    th = base.thresholds
    th = dataclasses.replace(th, **{k: v for k, v in {"min_confluence_score": a.min_score, "min_rr_tp2": a.min_rr}.items() if v is not None})
    cfg = dataclasses.replace(base, thresholds=th)
    meta = SymbolMetadata(a.symbol, a.symbol, "CRYPTO", a.tick, 2)

    print(f"Backtesting {a.symbol}: {len(df)} hourly bars | min_score={th.min_confluence_score} min_rr={th.min_rr_tp2}")
    trades = run(df, meta, cfg, a.step, a.max_hold, a.cooldown)
    report(trades)
    if a.out and not trades.empty:
        trades.to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
