"""Market data ingestion for Binance USDT Perpetual Futures (ccxt) and Gold (yfinance / TwelveData)."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import pandas as pd
import requests

try:  # heavy / optional at import time so the engine can be unit-tested offline
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None  # type: ignore[assignment]

from config import BotConfig

logger = logging.getLogger(__name__)
T = TypeVar("T")

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

EXCLUDED_STABLE_BASES = {
    "USDC",
    "FDUSD",
    "TUSD",
    "USDE",
    "DAI",
    "BUSD",
    "USDP",
    "PYUSD",
    "EUR",
}


@dataclass(frozen=True)
class SymbolMetadata:
    """Normalized market precision and asset metadata."""

    symbol: str
    display_name: str
    asset_class: str  # 'CRYPTO' or 'GOLD'
    tick_size: float
    price_decimals: int


def _decimals_from_tick(tick_size: float) -> int:
    if tick_size <= 0:
        return 4
    if tick_size >= 1:
        return 0
    return max(0, min(8, int(round(-math.log10(tick_size)))))


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price cleanly to the asset's exact tick size."""
    if tick_size <= 0:
        return round(price, 4)
    decimals = _decimals_from_tick(tick_size)
    rounded = round(round(price / tick_size) * tick_size, decimals)
    return float(rounded)


class MarketDataFetcher:
    """Unified resilient market data client for Crypto Futures and Gold."""

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        if ccxt is None:
            raise RuntimeError("ccxt is not installed. Run: pip install -r requirements.txt")
        self._exchange = ccxt.binanceusdm(
            {
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                },
            }
        )
        self._markets_loaded = False
        self._symbol_meta_cache: dict[str, SymbolMetadata] = {}
        self._http = requests.Session()

    @staticmethod
    def _transient_exceptions() -> tuple[type[BaseException], ...]:
        excs: list[type[BaseException]] = [requests.RequestException]
        if ccxt is not None:
            excs.extend(
                [
                    ccxt.RateLimitExceeded,
                    ccxt.DDoSProtection,
                    ccxt.NetworkError,
                    ccxt.ExchangeNotAvailable,
                ]
            )
        return tuple(excs)

    def _with_retry(self, label: str, func: Callable[[], T]) -> T:
        transient = self._transient_exceptions()
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                return func()
            except transient as exc:
                last_exc = exc
                if attempt >= self.cfg.max_retries:
                    break  # do not sleep after the final failed attempt
                delay = self.cfg.retry_backoff_base ** attempt
                logger.warning(
                    "%s transient error (attempt %d/%d): %s. Retrying in %.2fs",
                    label,
                    attempt,
                    self.cfg.max_retries,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
            except Exception as exc:
                last_exc = exc
                logger.error("%s failed unexpectedly: %s", label, exc)
                break
        assert last_exc is not None
        raise last_exc

    def _ensure_crypto_markets(self) -> None:
        if self._markets_loaded:
            return
        self._with_retry("ccxt.load_markets", self._exchange.load_markets)
        self._markets_loaded = True

    def _extract_ccxt_tick_size(self, market: dict[str, Any]) -> float:
        # 1) Binance PRICE_FILTER is the authoritative tick size
        for f in (market.get("info", {}) or {}).get("filters", []) or []:
            if f.get("filterType") == "PRICE_FILTER" and f.get("tickSize"):
                try:
                    tick = float(f["tickSize"])
                    if tick > 0:
                        return tick
                except (TypeError, ValueError):
                    pass

        # 2) ccxt precision: a tick size in TICK_SIZE mode, a digit count otherwise
        precision_price = (market.get("precision", {}) or {}).get("price")
        if isinstance(precision_price, (int, float)) and precision_price > 0:
            tick_mode = getattr(ccxt, "TICK_SIZE", 4) if ccxt is not None else 4
            if getattr(self._exchange, "precisionMode", tick_mode) == tick_mode:
                return float(precision_price)
            return float(10 ** (-int(precision_price)))

        limits_min = ((market.get("limits", {}) or {}).get("price", {}) or {}).get("min")
        if isinstance(limits_min, (int, float)) and limits_min > 0:
            return float(limits_min)

        return 0.0001

    def get_top_crypto_symbols(self, limit: int | None = None) -> list[SymbolMetadata]:
        """Return top N Binance USDT-M Perpetual symbols sorted by 24h quote volume."""
        top_n = limit or self.cfg.top_crypto_count
        self._ensure_crypto_markets()
        tickers = self._with_retry("ccxt.fetch_tickers", self._exchange.fetch_tickers)

        candidates: list[tuple[float, SymbolMetadata]] = []
        for symbol, market in self._exchange.markets.items():
            if not market.get("active", False):
                continue
            if not (market.get("swap") and market.get("linear")):
                continue
            if market.get("quote") != "USDT":
                continue
            base = str(market.get("base", "")).upper()
            if base in EXCLUDED_STABLE_BASES:
                continue

            ticker = tickers.get(symbol, {})
            quote_vol = ticker.get("quoteVolume")
            if quote_vol is None:
                info_vol = ticker.get("info", {}).get("quoteVolume")
                try:
                    quote_vol = float(info_vol) if info_vol is not None else 0.0
                except (TypeError, ValueError):
                    quote_vol = 0.0
            else:
                quote_vol = float(quote_vol)

            if quote_vol <= 0:
                continue

            tick_size = self._extract_ccxt_tick_size(market)
            clean_name = f"{base}USDT"
            meta = SymbolMetadata(
                symbol=symbol,
                display_name=clean_name,
                asset_class="CRYPTO",
                tick_size=tick_size,
                price_decimals=_decimals_from_tick(tick_size),
            )
            self._symbol_meta_cache[symbol] = meta
            self._symbol_meta_cache[clean_name] = meta
            candidates.append((quote_vol, meta))

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = [meta for _, meta in candidates[:top_n]]
        logger.info("Selected top %d Binance USDT perpetual pairs by 24h volume.", len(selected))
        return selected

    def get_gold_metadata(self) -> SymbolMetadata:
        meta = SymbolMetadata(
            symbol="XAUUSD",
            display_name="XAUUSD",
            asset_class="GOLD",
            tick_size=0.01,
            price_decimals=2,
        )
        self._symbol_meta_cache["XAUUSD"] = meta
        return meta

    def get_symbol_metadata(self, symbol: str, asset_class: str) -> SymbolMetadata:
        if symbol in self._symbol_meta_cache:
            return self._symbol_meta_cache[symbol]
        if asset_class == "GOLD" or symbol == "XAUUSD":
            return self.get_gold_metadata()

        self._ensure_crypto_markets()
        if symbol in self._exchange.markets:
            market = self._exchange.markets[symbol]
            tick_size = self._extract_ccxt_tick_size(market)
            base = str(market.get("base", symbol)).upper()
            meta = SymbolMetadata(
                symbol=symbol,
                display_name=f"{base}USDT",
                asset_class="CRYPTO",
                tick_size=tick_size,
                price_decimals=_decimals_from_tick(tick_size),
            )
            self._symbol_meta_cache[symbol] = meta
            return meta

        return SymbolMetadata(
            symbol=symbol,
            display_name=symbol,
            asset_class=asset_class,
            tick_size=0.0001,
            price_decimals=4,
        )

    @staticmethod
    def _normalize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        required = ["timestamp", "open", "high", "low", "close", "volume"]
        out = out[required]
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
        out["volume"] = out["volume"].fillna(0.0)
        out = out.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        out = out.reset_index(drop=True)
        return out

    def _drop_forming_candle(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Remove the still-open last bar so signals never repaint on unclosed candles."""
        if df.empty or not self.cfg.drop_forming_candle:
            return df
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe)
        if tf_seconds is None:
            return df
        last_ts = pd.Timestamp(df["timestamp"].iloc[-1])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        closes_at = last_ts + pd.Timedelta(seconds=tf_seconds)
        if closes_at > pd.Timestamp(datetime.now(timezone.utc)):
            return df.iloc[:-1].reset_index(drop=True)
        return df

    def fetch_crypto_ohlcv(
        self, symbol: str, timeframe: str, limit: int | None = None
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from Binance USDT Perpetual Futures."""
        self._ensure_crypto_markets()
        fetch_limit = limit or self.cfg.ohlcv_limit

        def _call() -> list[list[float]]:
            time.sleep(self.cfg.ccxt_pause_seconds)
            return self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=fetch_limit)

        raw = self._with_retry(f"ccxt.fetch_ohlcv({symbol},{timeframe})", _call)
        if not raw:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return self._drop_forming_candle(self._normalize_ohlcv_df(df), timeframe)

    def fetch_crypto_live_price(self, symbol: str) -> float:
        """Fetch current live price for a Binance USDT-M symbol."""
        self._ensure_crypto_markets()

        def _call() -> dict[str, Any]:
            time.sleep(self.cfg.ccxt_pause_seconds)
            return self._exchange.fetch_ticker(symbol)

        ticker = self._with_retry(f"ccxt.fetch_ticker({symbol})", _call)
        for key in ("last", "close", "mark"):
            val = ticker.get(key)
            if val is not None and float(val) > 0:
                return float(val)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            return (float(bid) + float(ask)) / 2.0
        raise ValueError(f"Unable to extract live price for {symbol}")

    def _fetch_gold_yfinance(self, timeframe: str) -> pd.DataFrame:
        if yf is None:
            raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt")
        ticker_symbol = self.cfg.gold_yfinance_symbol

        def _download(period: str, interval: str) -> pd.DataFrame:
            data = yf.download(
                tickers=ticker_symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if data is None or data.empty:
                return pd.DataFrame()
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = [str(c[0]).lower() for c in data.columns]
            else:
                data.columns = [str(c).lower() for c in data.columns]
            data = data.reset_index()
            ts_col = "datetime" if "datetime" in data.columns else "date"
            if ts_col not in data.columns:
                ts_col = data.columns[0]
            data = data.rename(columns={ts_col: "timestamp"})
            return self._normalize_ohlcv_df(data)

        if timeframe == "1d":
            return self._with_retry("yfinance.gold.1d", lambda: _download("2y", "1d"))
        if timeframe == "1h":
            return self._with_retry("yfinance.gold.1h", lambda: _download("60d", "1h"))
        if timeframe == "4h":
            # yfinance does not natively serve 4h intervals; resample clean 1h bars to 4h
            df_1h = self._with_retry("yfinance.gold.1h_for_4h", lambda: _download("90d", "1h"))
            if df_1h.empty:
                return df_1h
            indexed = df_1h.set_index("timestamp")
            resampled = (
                indexed.resample("4h")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna(subset=["open", "high", "low", "close"])
                .reset_index()
            )
            return self._normalize_ohlcv_df(resampled)

        raise ValueError(f"Unsupported Gold timeframe for yfinance: {timeframe}")

    def _fetch_gold_twelvedata(self, timeframe: str, limit: int) -> pd.DataFrame:
        tf_map = {"1h": "1h", "4h": "4h", "1d": "1day"}
        td_interval = tf_map.get(timeframe)
        if td_interval is None:
            raise ValueError(f"Unsupported Gold timeframe for TwelveData: {timeframe}")

        def _call() -> dict[str, Any]:
            resp = self._http.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol": self.cfg.gold_twelvedata_symbol,
                    "interval": td_interval,
                    "outputsize": min(limit, 500),
                    "apikey": self.cfg.twelvedata_api_key,
                    "timezone": "UTC",
                },
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") == "error":
                raise RuntimeError(f"TwelveData error: {payload.get('message')}")
            return payload

        payload = self._with_retry(f"twelvedata.gold.{timeframe}", _call)
        values = payload.get("values") or []
        if not values:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        rows = []
        for bar in values:
            rows.append(
                {
                    "timestamp": bar["datetime"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar.get("volume", 0.0),
                }
            )
        return self._normalize_ohlcv_df(pd.DataFrame(rows))

    def fetch_gold_ohlcv(self, timeframe: str, limit: int | None = None) -> pd.DataFrame:
        """Fetch Gold (XAUUSD) OHLCV using the configured provider."""
        fetch_limit = limit or self.cfg.ohlcv_limit
        if self.cfg.gold_data_provider == "twelvedata":
            df = self._fetch_gold_twelvedata(timeframe, fetch_limit)
        else:
            df = self._fetch_gold_yfinance(timeframe)
        return self._drop_forming_candle(df, timeframe)

    def fetch_gold_live_price(self) -> float:
        """Fetch latest Gold price from configured provider."""
        if self.cfg.gold_data_provider == "twelvedata":
            def _call() -> dict[str, Any]:
                resp = self._http.get(
                    "https://api.twelvedata.com/price",
                    params={
                        "symbol": self.cfg.gold_twelvedata_symbol,
                        "apikey": self.cfg.twelvedata_api_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                payload = resp.json()
                if "price" not in payload:
                    raise RuntimeError(f"TwelveData price error: {payload}")
                return payload

            data = self._with_retry("twelvedata.gold.price", _call)
            return float(data["price"])

        def _yf_price() -> float:
            if yf is None:
                raise RuntimeError("yfinance is not installed.")
            ticker = yf.Ticker(self.cfg.gold_yfinance_symbol)
            hist = ticker.history(period="2d", interval="1m")
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[-1])
            fast = getattr(ticker, "fast_info", None)
            if fast and fast.get("lastPrice"):
                return float(fast["lastPrice"])
            raise ValueError("Unable to retrieve Gold live price from yfinance.")

        return self._with_retry("yfinance.gold.price", _yf_price)

    def fetch_multi_timeframe(
        self, symbol: str, asset_class: str
    ) -> dict[str, pd.DataFrame]:
        """Fetch 1d, 4h, and 1h OHLCV frames for either Crypto or Gold."""
        if asset_class == "GOLD":
            return {
                "1d": self.fetch_gold_ohlcv("1d"),
                "4h": self.fetch_gold_ohlcv("4h"),
                "1h": self.fetch_gold_ohlcv("1h"),
            }
        return {
            "1d": self.fetch_crypto_ohlcv(symbol, "1d"),
            "4h": self.fetch_crypto_ohlcv(symbol, "4h"),
            "1h": self.fetch_crypto_ohlcv(symbol, "1h"),
        }

    def fetch_live_price(self, symbol: str, asset_class: str) -> float:
        if asset_class == "GOLD" or symbol == "XAUUSD":
            return self.fetch_gold_live_price()
        return self.fetch_crypto_live_price(symbol)

    def fetch_price_window(
        self, symbol: str, asset_class: str, since: datetime
    ) -> tuple[float, float, float]:
        """Return (low, high, last) traded since `since` using 1m candles.

        Polling only the last price can miss wicks that touch SL/TP between two checks;
        1m extremes make trade tracking faithful to what the market really did.
        Falls back to the live price when candle data is unavailable.
        """
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        floor = datetime.now(timezone.utc).timestamp() - 16 * 3600
        since_ts = max((since.timestamp() // 60) * 60, floor)

        try:
            if asset_class == "GOLD" or symbol == "XAUUSD":
                if self.cfg.gold_data_provider == "yfinance" and yf is not None:
                    hist = self._with_retry(
                        "yfinance.gold.1m",
                        lambda: yf.Ticker(self.cfg.gold_yfinance_symbol).history(
                            period="1d", interval="1m"
                        ),
                    )
                    if hist is not None and not hist.empty:
                        idx = pd.to_datetime(hist.index, utc=True)
                        mask = idx.astype("int64") // 10**9 >= since_ts
                        window = hist[mask]
                        if not window.empty:
                            return (
                                float(window["Low"].min()),
                                float(window["High"].max()),
                                float(window["Close"].iloc[-1]),
                            )
            else:
                self._ensure_crypto_markets()

                def _call() -> list[list[float]]:
                    time.sleep(self.cfg.ccxt_pause_seconds)
                    return self._exchange.fetch_ohlcv(
                        symbol, timeframe="1m", since=int(since_ts * 1000), limit=1000
                    )

                raw = self._with_retry(f"ccxt.fetch_ohlcv({symbol},1m)", _call)
                if raw:
                    lows = [float(r[3]) for r in raw]
                    highs = [float(r[2]) for r in raw]
                    return min(lows), max(highs), float(raw[-1][4])
        except Exception as exc:  # fall through to the live price
            logger.warning("1m window unavailable for %s (%s); using live price.", symbol, exc)

        price = self.fetch_live_price(symbol, asset_class)
        return price, price, price
