"""SQLite persistence layer for tracking signals, active trades, and performance stats."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeRecord:
    """In-memory representation of a stored trade signal."""

    id: int
    symbol: str
    asset_class: str
    direction: str
    timeframe: str
    regime: str
    score: int
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr_ratio: float
    rationale_ar: str
    status: str
    entry_hit: bool
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    sl_hit: bool
    realized_r: float
    outcome: str | None
    telegram_message_id: int | None
    created_at: str
    activated_at: str | None
    closed_at: str | None

    @property
    def effective_stop(self) -> float:
        """Stop actually in force: entry (breakeven) once TP1 has been banked."""
        return self.entry_price if self.tp1_hit else self.stop_loss

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "direction": self.direction,
            "timeframe": self.timeframe,
            "regime": self.regime,
            "score": self.score,
            "entry": self.entry_price,
            "stop_loss": self.stop_loss,
            "effective_stop": self.effective_stop,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "rr": self.rr_ratio,
            "rationale": self.rationale_ar,
            "status": self.status,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "tp3_hit": self.tp3_hit,
            "sl_hit": self.sl_hit,
            "realized_r": self.realized_r,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "closed_at": self.closed_at,
        }


class TradeDatabase:
    """Thread-safe SQLite repository for signal lifecycle management."""

    def __init__(self, db_path: str = "trades.db") -> None:
        self.db_path = db_path
        parent = Path(db_path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL CHECK(asset_class IN ('CRYPTO', 'GOLD')),
                    direction TEXT NOT NULL CHECK(direction IN ('LONG', 'SHORT')),
                    timeframe TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    rr_ratio REAL NOT NULL,
                    rationale_ar TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE', 'CLOSED')),
                    entry_hit INTEGER NOT NULL DEFAULT 0,
                    tp1_hit INTEGER NOT NULL DEFAULT 0,
                    tp2_hit INTEGER NOT NULL DEFAULT 0,
                    tp3_hit INTEGER NOT NULL DEFAULT 0,
                    sl_hit INTEGER NOT NULL DEFAULT 0,
                    realized_r REAL NOT NULL DEFAULT 0.0,
                    outcome TEXT CHECK(outcome IN ('WIN', 'LOSS', 'BREAKEVEN', 'CANCELLED') OR outcome IS NULL),
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    closed_at TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trades_symbol_status
                ON trades(symbol, status);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trades_closed_at
                ON trades(closed_at);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=int(row["id"]),
            symbol=str(row["symbol"]),
            asset_class=str(row["asset_class"]),
            direction=str(row["direction"]),
            timeframe=str(row["timeframe"]),
            regime=str(row["regime"]),
            score=int(row["score"]),
            entry_price=float(row["entry_price"]),
            stop_loss=float(row["stop_loss"]),
            tp1=float(row["tp1"]),
            tp2=float(row["tp2"]),
            tp3=float(row["tp3"]),
            rr_ratio=float(row["rr_ratio"]),
            rationale_ar=str(row["rationale_ar"]),
            status=str(row["status"]),
            entry_hit=bool(row["entry_hit"]),
            tp1_hit=bool(row["tp1_hit"]),
            tp2_hit=bool(row["tp2_hit"]),
            tp3_hit=bool(row["tp3_hit"]),
            sl_hit=bool(row["sl_hit"]),
            realized_r=float(row["realized_r"]),
            outcome=str(row["outcome"]) if row["outcome"] is not None else None,
            telegram_message_id=(
                int(row["telegram_message_id"])
                if row["telegram_message_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            activated_at=str(row["activated_at"]) if row["activated_at"] else None,
            closed_at=str(row["closed_at"]) if row["closed_at"] else None,
        )

    def has_open_trade(self, symbol: str) -> bool:
        """Prevent duplicate signals while a symbol has a PENDING or ACTIVE trade."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT 1 FROM trades
                WHERE symbol = ? AND status IN ('PENDING', 'ACTIVE')
                LIMIT 1;
                """,
                (symbol,),
            )
            return cur.fetchone() is not None

    def create_trade(
        self,
        *,
        symbol: str,
        asset_class: str,
        direction: str,
        timeframe: str,
        regime: str,
        score: int,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        tp3: float,
        rr_ratio: float,
        rationale_ar: str,
        telegram_message_id: int | None = None,
    ) -> int:
        """Insert a newly triggered signal in PENDING state and return its ID."""
        now_iso = self._utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    symbol, asset_class, direction, timeframe, regime, score,
                    entry_price, stop_loss, tp1, tp2, tp3, rr_ratio,
                    rationale_ar, status, telegram_message_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?);
                """,
                (
                    symbol,
                    asset_class,
                    direction,
                    timeframe,
                    regime,
                    score,
                    entry_price,
                    stop_loss,
                    tp1,
                    tp2,
                    tp3,
                    rr_ratio,
                    rationale_ar,
                    telegram_message_id,
                    now_iso,
                ),
            )
            trade_id = int(cur.lastrowid)
            logger.info("Stored trade #%d (%s %s) as PENDING", trade_id, symbol, direction)
            return trade_id

    def set_telegram_message_id(self, trade_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET telegram_message_id = ? WHERE id = ?;",
                (message_id, trade_id),
            )

    def get_open_trades(self) -> list[TradeRecord]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM trades
                WHERE status IN ('PENDING', 'ACTIVE')
                ORDER BY id ASC;
                """
            )
            return [self._row_to_trade(row) for row in cur.fetchall()]

    def get_trade_by_id(self, trade_id: int) -> TradeRecord | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM trades WHERE id = ?;", (trade_id,))
            row = cur.fetchone()
            return self._row_to_trade(row) if row else None

    @staticmethod
    def _target_r(entry: float, sl: float, target: float) -> float:
        risk = abs(entry - sl)
        if risk <= 0:
            return 0.0
        return abs(target - entry) / risk

    def mark_entry_hit(self, trade_id: int) -> None:
        now_iso = self._utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET status = 'ACTIVE',
                    entry_hit = 1,
                    activated_at = COALESCE(activated_at, ?)
                WHERE id = ? AND status = 'PENDING';
                """,
                (now_iso, trade_id),
            )

    def mark_tp1_hit(self, trade_id: int) -> None:
        trade = self.get_trade_by_id(trade_id)
        if trade is None or trade.tp1_hit:
            return
        r_tp1 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp1)
        # 50% scale-out at TP1
        realized = round(0.50 * r_tp1, 4)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET tp1_hit = 1,
                    realized_r = ?
                WHERE id = ?;
                """,
                (realized, trade_id),
            )

    def mark_tp2_hit(self, trade_id: int) -> None:
        trade = self.get_trade_by_id(trade_id)
        if trade is None or trade.tp2_hit:
            return
        r_tp1 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp1)
        r_tp2 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp2)
        # 50% at TP1 + 25% at TP2
        realized = round((0.50 * r_tp1) + (0.25 * r_tp2), 4)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET tp1_hit = 1,
                    tp2_hit = 1,
                    realized_r = ?
                WHERE id = ?;
                """,
                (realized, trade_id),
            )

    def mark_tp3_hit(self, trade_id: int) -> None:
        trade = self.get_trade_by_id(trade_id)
        if trade is None or trade.tp3_hit:
            return
        r_tp1 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp1)
        r_tp2 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp2)
        r_tp3 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp3)
        # 50% at TP1 + 25% at TP2 + 25% at TP3
        realized = round((0.50 * r_tp1) + (0.25 * r_tp2) + (0.25 * r_tp3), 4)
        now_iso = self._utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET tp1_hit = 1,
                    tp2_hit = 1,
                    tp3_hit = 1,
                    realized_r = ?,
                    status = 'CLOSED',
                    outcome = 'WIN',
                    closed_at = ?
                WHERE id = ?;
                """,
                (realized, now_iso, trade_id),
            )

    def mark_sl_hit(self, trade_id: int) -> None:
        trade = self.get_trade_by_id(trade_id)
        if trade is None or trade.status == "CLOSED":
            return

        now_iso = self._utc_now_iso()
        if not trade.entry_hit:
            # Invalidated before entry triggered
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE trades
                    SET status = 'CLOSED',
                        outcome = 'CANCELLED',
                        realized_r = 0.0,
                        closed_at = ?
                    WHERE id = ?;
                    """,
                    (now_iso, trade_id),
                )
            return

        # After TP1, SL management moves stop to entry (breakeven on remaining 50%),
        # unless original stop is hit before TP1 (-1.0 R).
        r_tp1 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp1)
        r_tp2 = self._target_r(trade.entry_price, trade.stop_loss, trade.tp2)
        if trade.tp2_hit:
            realized = round((0.50 * r_tp1) + (0.25 * r_tp2), 4)
            outcome = "WIN"
        elif trade.tp1_hit:
            realized = round(0.50 * r_tp1, 4)
            outcome = "WIN"
        else:
            realized = -1.0
            outcome = "LOSS"

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET sl_hit = 1,
                    status = 'CLOSED',
                    realized_r = ?,
                    outcome = ?,
                    closed_at = ?
                WHERE id = ?;
                """,
                (realized, outcome, now_iso, trade_id),
            )

    # ------------------------------------------------------------------
    # Lifecycle guards (cooldown, capacity, cancellation, rollback)
    # ------------------------------------------------------------------

    def count_open_trades(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM trades WHERE status IN ('PENDING', 'ACTIVE');"
            ).fetchone()
            return int(row["c"])

    def in_cooldown(self, symbol: str, hours: float) -> bool:
        """True if the symbol had a trade created/closed within the cooldown window."""
        if hours <= 0:
            return False
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM trades
                WHERE symbol = ?
                  AND outcome IS NOT 'CANCELLED'
                  AND (COALESCE(closed_at, created_at) >= ?)
                LIMIT 1;
                """,
                (symbol, since_iso),
            ).fetchone()
            return row is not None

    def cancel_trade(self, trade_id: int) -> None:
        """Close a never-filled trade as CANCELLED (no R impact)."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET status = 'CLOSED', outcome = 'CANCELLED', realized_r = 0.0, closed_at = ?
                WHERE id = ? AND status = 'PENDING';
                """,
                (self._utc_now_iso(), trade_id),
            )

    def delete_trade(self, trade_id: int) -> None:
        """Roll back a trade whose Telegram broadcast failed (so stats stay honest)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM trades WHERE id = ?;", (trade_id,))

    # ------------------------------------------------------------------
    # Read models for the dashboard / API
    # ------------------------------------------------------------------

    def get_recent_trades(self, limit: int = 50, status: str | None = None) -> list[TradeRecord]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            if status in {"PENDING", "ACTIVE", "CLOSED"}:
                cur = conn.execute(
                    "SELECT * FROM trades WHERE status = ? ORDER BY id DESC LIMIT ?;",
                    (status, limit),
                )
            else:
                cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?;", (limit,))
            return [self._row_to_trade(r) for r in cur.fetchall()]

    def get_equity_curve(self, days: int = 90) -> list[dict[str, Any]]:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, outcome, realized_r, closed_at FROM trades
                WHERE status = 'CLOSED' AND outcome IN ('WIN', 'LOSS', 'BREAKEVEN')
                  AND closed_at >= ?
                ORDER BY closed_at ASC;
                """,
                (since_iso,),
            ).fetchall()
        curve: list[dict[str, Any]] = []
        cumulative = 0.0
        for r in rows:
            cumulative += float(r["realized_r"])
            curve.append(
                {
                    "id": int(r["id"]),
                    "symbol": str(r["symbol"]),
                    "outcome": str(r["outcome"]),
                    "r": round(float(r["realized_r"]), 3),
                    "cum_r": round(cumulative, 3),
                    "closed_at": str(r["closed_at"]),
                }
            )
        return curve

    def get_breakdown(self, days: int = 90) -> dict[str, Any]:
        """Win-rate / R by asset class and by symbol (top 10)."""
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, asset_class, outcome, realized_r FROM trades
                WHERE status = 'CLOSED' AND outcome IN ('WIN', 'LOSS', 'BREAKEVEN')
                  AND closed_at >= ?;
                """,
                (since_iso,),
            ).fetchall()

        def _agg(items: list[sqlite3.Row]) -> dict[str, Any]:
            n = len(items)
            w = sum(1 for i in items if i["outcome"] == "WIN")
            r = sum(float(i["realized_r"]) for i in items)
            return {
                "trades": n,
                "wins": w,
                "win_rate_pct": round(100.0 * w / n, 1) if n else 0.0,
                "total_r": round(r, 2),
            }

        by_class: dict[str, list[sqlite3.Row]] = {}
        by_symbol: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_class.setdefault(str(row["asset_class"]), []).append(row)
            by_symbol.setdefault(str(row["symbol"]), []).append(row)

        symbols = sorted(
            ({"symbol": k, **_agg(v)} for k, v in by_symbol.items()),
            key=lambda d: d["total_r"],
            reverse=True,
        )[:10]
        return {
            "by_class": {k: _agg(v) for k, v in by_class.items()},
            "top_symbols": symbols,
        }

    def get_performance_stats(self, days: int = 1) -> dict[str, Any]:
        """Compute win rate and R-multiple performance over the last `days` days."""
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            closed_rows = conn.execute(
                """
                SELECT outcome, realized_r
                FROM trades
                WHERE status = 'CLOSED'
                  AND outcome IN ('WIN', 'LOSS', 'BREAKEVEN')
                  AND closed_at >= ?;
                """,
                (since_iso,),
            ).fetchall()

            open_counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) AS active_cnt,
                    SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending_cnt
                FROM trades;
                """
            ).fetchone()

        total_closed = len(closed_rows)
        wins = sum(1 for r in closed_rows if r["outcome"] == "WIN")
        losses = sum(1 for r in closed_rows if r["outcome"] == "LOSS")
        breakevens = sum(1 for r in closed_rows if r["outcome"] == "BREAKEVEN")
        r_values = [float(r["realized_r"]) for r in closed_rows]
        total_r = round(sum(r_values), 2)
        win_rate = round((wins / total_closed) * 100.0, 2) if total_closed > 0 else 0.0
        best_r = round(max(r_values), 2) if r_values else 0.0
        worst_r = round(min(r_values), 2) if r_values else 0.0
        gross_win = sum(v for v in r_values if v > 0)
        gross_loss = -sum(v for v in r_values if v < 0)
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if gross_win == 0 else 999.0)

        return {
            "days": days,
            "total_closed": total_closed,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "win_rate_pct": win_rate,
            "total_r": total_r,
            "best_r": best_r,
            "worst_r": worst_r,
            "profit_factor": profit_factor,
            "avg_r": round(total_r / total_closed, 3) if total_closed else 0.0,
            "active_trades": int(open_counts["active_cnt"] or 0),
            "pending_trades": int(open_counts["pending_cnt"] or 0),
        }

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT value FROM bot_meta WHERE key = ?;", (key,))
            row = cur.fetchone()
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )
