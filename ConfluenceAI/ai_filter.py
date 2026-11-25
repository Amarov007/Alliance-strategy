"""Optional AI Confirmation Layer (Anthropic Claude / OpenAI) with smart Arabic rule-based fallback."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from config import BotConfig
from strategy_engine import TradeSignalCandidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIFilterDecision:
    """Result of the final logic verification and Arabic rationale generation."""

    approved: bool
    rationale_ar: str
    used_ai: bool
    provider: str | None = None
    rejection_reason: str | None = None


def generate_rule_based_arabic_rationale(candidate: TradeSignalCandidate) -> str:
    """Build a crisp, institutional Arabic rationale from the aligned trading schools."""
    lines: list[str] = []
    for reason in candidate.reasons_ar:
        lines.append(f"• {reason}")

    s = candidate.school_scores
    breakdown = (
        f"• توزيع النقاط: SMC ({s['smc']}/35) | "
        f"السيولة وVWAP ({s['volume_vwap']}/25) | "
        f"البرايس أكشن ({s['pa_divergence']}/20) | "
        f"فلتر النظام ({s['regime_mtf']}/20)."
    )
    lines.append(breakdown)
    return "\n".join(lines)


class AIConfirmationFilter:
    """Validates candidate setups via Claude or OpenAI when AI_API_KEY is configured."""

    SYSTEM_PROMPT = (
        "You are a Chief Quantitative Risk Officer & Institutional Smart Money Analyst. "
        "You receive a compact JSON summary of a multi-school trading setup (SMC/ICT, Volume/VWAP, "
        "Price Action/Divergence, and Market Regime/MTF). "
        "Your job is two-fold:\n"
        "1. Perform a strict logic & contradiction check (e.g., verify direction matches regime rules, "
        "R:R to TP2 >= 2.0, VWAP discount/premium makes sense, and no obvious structural contradiction exists).\n"
        "2. Return ONLY valid JSON with exact keys:\n"
        '{"approved": true|false, "reason_ar": "<concise 2-4 bullet institutional rationale in Arabic>", '
        '"rejection_reason": "<short English reason if rejected, else empty string>"}\n'
        "Never include markdown outside JSON or invent prices."
    )

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()

    def _resolve_provider(self) -> str:
        if self.cfg.ai_provider in {"anthropic", "openai"}:
            return self.cfg.ai_provider
        key = self.cfg.ai_api_key
        if key.startswith("sk-ant"):
            return "anthropic"
        return "openai"

    @staticmethod
    def _extract_json_object(raw_text: str) -> dict[str, Any]:
        cleaned = raw_text.strip()
        fence_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(0)
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("AI response is not a JSON object")
        return parsed

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "approved"}
        return bool(value)

    @staticmethod
    def _coerce_rationale(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "\n".join(f"• {str(v).lstrip('•- ').strip()}" for v in value if str(v).strip())
        return str(value or "").strip()

    def _call_anthropic(self, payload_json: str) -> dict[str, Any]:
        model = self.cfg.ai_model or "claude-sonnet-4-6"
        resp = self._session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.cfg.ai_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 600,
                "system": self.SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Evaluate this trade setup JSON:\n{payload_json}",
                    }
                ],
            },
            timeout=self.cfg.ai_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        return self._extract_json_object("\n".join(text_blocks))

    def _call_openai(self, payload_json: str) -> dict[str, Any]:
        model = self.cfg.ai_model or "gpt-4o-mini"
        resp = self._session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.cfg.ai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Evaluate this trade setup JSON:\n{payload_json}",
                    },
                ],
            },
            timeout=self.cfg.ai_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return self._extract_json_object(content)

    def confirm_and_explain(
        self, candidate: TradeSignalCandidate
    ) -> AIFilterDecision:
        """Verify setup with AI if AI_API_KEY is set; otherwise use rule-based Arabic rationale."""
        fallback_rationale = generate_rule_based_arabic_rationale(candidate)

        if not self.cfg.ai_api_key:
            return AIFilterDecision(
                approved=True,
                rationale_ar=fallback_rationale,
                used_ai=False,
            )

        provider = self._resolve_provider()
        compact_json = json.dumps(candidate.to_ai_payload(), ensure_ascii=False)

        try:
            if provider == "anthropic":
                result = self._call_anthropic(compact_json)
            else:
                result = self._call_openai(compact_json)

            approved = self._coerce_bool(result.get("approved", False))
            reason_ar = self._coerce_rationale(result.get("reason_ar")) or fallback_rationale
            rejection_reason = str(result.get("rejection_reason", "")).strip() or None

            if not approved:
                logger.info(
                    "AI (%s) rejected %s %s: %s",
                    provider,
                    candidate.display_name,
                    candidate.direction,
                    rejection_reason or "Logic contradiction detected",
                )
            return AIFilterDecision(
                approved=approved,
                rationale_ar=reason_ar,
                used_ai=True,
                provider=provider,
                rejection_reason=rejection_reason,
            )
        except Exception as exc:
            logger.warning(
                "AI confirmation (%s) unavailable (%s); falling back to rule-based Arabic rationale.",
                provider,
                exc,
            )
            return AIFilterDecision(
                approved=True,
                rationale_ar=fallback_rationale,
                used_ai=False,
                provider=provider,
            )
