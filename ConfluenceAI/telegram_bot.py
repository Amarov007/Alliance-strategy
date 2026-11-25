"""Telegram notification service for Arabic signals, live trade reply updates, and performance reports."""

from __future__ import annotations

import html
import logging
import math
import re
import time
from typing import Any

import requests

from config import BotConfig
from strategy_engine import TradeSignalCandidate

logger = logging.getLogger(__name__)

REPLY_ENTRY_HIT = "تفعلت الصفقة ✅"
REPLY_TP1_HIT = "تحقق الهدف الأول 🎯 (يُفضل تأمين الصفقة ونقل الوقف لنقطة الدخول)"
REPLY_TP2_HIT = "تحقق الهدف الثاني 🏆"
REPLY_TP3_HIT = "تحقق الهدف الثالث 💎 (إغلاق الصفقة بالكامل)"
REPLY_SL_HIT = "ضرب وقف الخسارة ❌"
REPLY_CANCELLED_PRE_ENTRY = "تم إلغاء الصفقة ⚠️ (وصل السعر لمستوى الإبطال قبل التفعيل)"
REPLY_MISSED_ENTRY = "تم إلغاء الصفقة ⚠️ (تحرك السعر نحو الهدف دون تفعيل الدخول — لا تطارد السعر)"
REPLY_EXPIRED = "انتهت صلاحية الصفقة المعلقة ⌛ (لم يُفعَّل الدخول خلال المدة المحددة)"
REPLY_BREAKEVEN_STOP = "أُغلقت الصفقة عند نقطة الدخول 🛡️ (تم تأمين جزء من الأرباح مسبقاً)"
RISK_DISCLAIMER = "⚠️ <i>تنبيه: إشارات تعليمية وليست نصيحة مالية. التداول ينطوي على مخاطر عالية — لا تخاطر بأكثر من 1% من رأس المال.</i>"


def format_price(value: float, tick_size: float | None = None) -> str:
    """Format a price using the instrument's real tick precision (never scientific notation)."""
    if tick_size and tick_size > 0:
        decimals = 0 if tick_size >= 1 else min(8, max(0, int(round(-math.log10(tick_size)))))
        return f"{value:.{decimals}f}"
    if abs(value) >= 1000:
        text = f"{value:.2f}"
    elif abs(value) >= 1:
        text = f"{value:.4f}"
    else:
        text = f"{value:.8f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_signal_message_html(
    candidate: TradeSignalCandidate, rationale_ar: str
) -> str:
    """Format the trading signal in institutional Arabic using safe HTML escaping."""
    if candidate.direction == "LONG":
        dir_line = f"🟢 شراء (LONG) — <code>{html.escape(candidate.display_name)}</code>"
    else:
        dir_line = f"🔻 بيع (SHORT) — <code>{html.escape(candidate.display_name)}</code>"

    regime_esc = html.escape(candidate.regime_ar)
    tf_esc = html.escape(candidate.timeframe)
    reasons_esc = html.escape(rationale_ar.strip())

    tick = candidate.tick_size
    entry_s = format_price(candidate.entry_price, tick)
    sl_s = format_price(candidate.stop_loss, tick)
    tp1_s = format_price(candidate.tp1, tick)
    tp2_s = format_price(candidate.tp2, tick)
    tp3_s = format_price(candidate.tp3, tick)
    rr_s = f"{candidate.rr_tp2:.2f}".rstrip("0").rstrip(".")

    return (
        f"🔥 <b>إشارة عالية الدقة | تقييم القوة: {candidate.score}/100</b>\n"
        f"{dir_line}\n"
        f"📊 حالة السوق: {regime_esc} | الفريم: {tf_esc}\n\n"
        f"🔹 <b>الدخول:</b> <code>{entry_s}</code>\n"
        f"🛑 <b>وقف الخسارة:</b> <code>{sl_s}</code>\n"
        f"🎯 <b>الهدف الأول:</b> <code>{tp1_s}</code>\n"
        f"🏆 <b>الهدف الثاني:</b> <code>{tp2_s}</code>\n"
        f"💎 <b>الهدف الثالث:</b> <code>{tp3_s}</code>\n"
        f"⚖️ <b>نسبة العائد للمخاطرة:</b> <code>1:{rr_s}</code>\n\n"
        f"🧠 <b>تحليل المدارس المتوافقة:</b>\n"
        f"{reasons_esc}\n\n"
        f"💡 <i>إدارة الصفقة: جني 50% عند الهدف الأول ونقل الوقف لنقطة الدخول.</i>\n\n"
        f"{RISK_DISCLAIMER}"
    )


def format_performance_report_html(period_title_ar: str, stats: dict[str, Any]) -> str:
    """Format daily or weekly performance statistics in Arabic."""
    total_r = float(stats["total_r"])
    r_sign = "+" if total_r > 0 else ""
    best_r = float(stats["best_r"])
    worst_r = float(stats["worst_r"])

    return (
        f"📈 <b>تقرير الأداء {html.escape(period_title_ar)}</b>\n\n"
        f"📌 <b>الصفقات المغلقة:</b> <code>{stats['total_closed']}</code>\n"
        f"✅ <b>الصفقات الرابحة:</b> <code>{stats['wins']}</code>\n"
        f"❌ <b>الصفقات الخاسرة:</b> <code>{stats['losses']}</code>\n"
        f"🎯 <b>نسبة النجاح (Win Rate):</b> <code>{stats['win_rate_pct']:.1f}%</code>\n"
        f"💰 <b>صافي العائد (R-Multiples):</b> <code>{r_sign}{total_r:.2f}R</code>\n"
        f"📐 <b>معامل الربحية:</b> <code>{stats.get('profit_factor') if stats.get('profit_factor') is not None else '—'}</code> | "
        f"<b>متوسط R:</b> <code>{stats.get('avg_r', 0):+.2f}</code>\n"
        f"🏆 <b>أفضل صفقة:</b> <code>{best_r:+.2f}R</code> | "
        f"⚠️ <b>أسوأ صفقة:</b> <code>{worst_r:+.2f}R</code>\n\n"
        f"🔄 <b>الصفقات الجارية:</b> نشطة <code>{stats['active_trades']}</code> | "
        f"معلقة <code>{stats['pending_trades']}</code>"
    )


class TelegramNotifier:
    """Resilient Telegram Bot API client with 429 rate-limit backoff."""

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self._base_url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        self._session = requests.Session()

    def _send_message(
        self,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> int | None:
        if len(text) > 4000:
            text = text[:3990] + "…"
        payload: dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }

        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self._session.post(
                    f"{self._base_url}/sendMessage",
                    json=payload,
                    timeout=self.cfg.telegram_timeout_seconds,
                )
                if resp.status_code == 429:
                    retry_after = float(
                        resp.json().get("parameters", {}).get("retry_after", 3)
                    )
                    logger.warning("Telegram 429 rate limit; sleeping %.1fs", retry_after)
                    time.sleep(retry_after + 0.5)
                    continue

                if resp.status_code == 400 and parse_mode is not None:
                    # Safe fallback to plain text if Telegram rejects markup
                    logger.warning("Telegram 400 with parse_mode=%s; retrying plain text.", parse_mode)
                    payload.pop("parse_mode", None)
                    payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
                    parse_mode = None
                    continue

                resp.raise_for_status()
                body = resp.json()
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API error: {body.get('description')}")
                return int(body["result"]["message_id"])

            except requests.RequestException as exc:
                delay = self.cfg.retry_backoff_base ** attempt
                logger.warning(
                    "Telegram send failed (attempt %d/%d): %s. Retrying in %.2fs",
                    attempt,
                    self.cfg.max_retries,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
            except Exception as exc:
                logger.error("Telegram unexpected error: %s", exc)
                break

        return None

    def send_signal(
        self, candidate: TradeSignalCandidate, rationale_ar: str
    ) -> int | None:
        """Broadcast a formatted institutional signal and return its Telegram message_id."""
        msg = format_signal_message_html(candidate, rationale_ar)
        return self._send_message(msg, parse_mode="HTML")

    def send_trade_reply(self, reply_to_message_id: int | None, text: str) -> int | None:
        """Send an automated status reply linked to the original signal message."""
        return self._send_message(
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode="HTML",
        )

    def send_performance_report(
        self, period_title_ar: str, stats: dict[str, Any]
    ) -> int | None:
        msg = format_performance_report_html(period_title_ar, stats)
        return self._send_message(msg, parse_mode="HTML")

    def send_text(self, text: str) -> int | None:
        """Send an arbitrary HTML-formatted notice (startup, health alerts)."""
        return self._send_message(text, parse_mode="HTML")
