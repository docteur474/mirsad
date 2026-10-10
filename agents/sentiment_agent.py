"""
sentiment_agent.py — وكيل الأخبار والمعنويات اللحظية
[نموذج لغوي قابل للضبط من .env عبر SENTIMENT_MODEL — الافتراضي Claude 3.5 Sonnet]
إصلاحات المرحلة 1:
  • اسم النموذج من config مع معالجة خطأ واضحة عند غياب الحزمة/المفتاح.
  • كاش الأخبار ونتيجة LLM لكل رمز NEWS_CACHE_MIN دقيقة (خفض التكلفة).
  • كشف فخ الثيران: trap_risk مرتفع = VETO.
"""
from __future__ import annotations

import json
import time

import aiohttp

try:                                    # الحزمة اختيارية البيئة
    import anthropic
except ImportError:                     # رسالة واضحة بدل ImportError صامت
    anthropic = None

import config as cfg
from agents.base_agent import AgentReport, BaseAgent, Signal

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"

PROMPT_TEMPLATE = """You are a crypto market-psychology engine. SPOT signals only.
Read the headlines and return STRICT JSON only:
{"sentiment": float in [-1,1], "trap_risk": float in [0,1],
 "hype": float in [0,1], "note": "one line justification"}
HEADLINES:
__HEADLINES__"""


class NewsSentimentAgent(BaseAgent):
    NAME = "sentiment"

    def __init__(self, weight: float) -> None:
        super().__init__(weight)
        if not cfg.SENTIMENT_MODEL:
            raise ValueError("SENTIMENT_MODEL غير مضبوط — عرّفه في .env")
        if anthropic is None:
            raise RuntimeError(
                "حزمة anthropic غير مثبتة — pip install anthropic "
                "أو بدّل الموفر عبر SENTIMENT_MODEL"
            )
        self._model = cfg.SENTIMENT_MODEL
        self._llm   = anthropic.AsyncAnthropic()
        # كاش لكل رمز: (وقت الانتهاء, العناوين, التقييم) — يخفض التكلفة جذرياً
        self._cache: dict[str, tuple[float, list[str], dict]] = {}

    # ───────────────── التحليل الرئيسي ─────────────────
    async def analyze(self, symbol: str) -> AgentReport:
        base = symbol.split("/")[0]
        headlines, graded = await self._headlines_and_grade(base)

        sentiment = float(graded.get("sentiment", 0.0))
        trap      = float(graded.get("trap_risk", 1.0))
        hype      = float(graded.get("hype", 0.0))

        # خصم قاسٍ من رائحة الفخ — معنويات إيجابية مع فخ محتمل = اختراق زائف
        score = sentiment * (1.0 - trap * 0.7)

        if trap >= 0.75:
            signal = Signal.VETO
        elif score >= 0.65 and hype < 0.60:
            signal = Signal.STRONG_BUY
        elif score >= 0.40 and hype < 0.80:
            signal = Signal.BUY
        elif score <= -0.35:
            signal = Signal.AVOID
        else:
            signal = Signal.NEUTRAL

        return AgentReport(
            agent=self.NAME, symbol=symbol,
            score=max(-1.0, min(1.0, score)),
            confidence=0.90 - trap * 0.40,
            signal=signal,
            reasoning=graded.get("note", "no-note"),
            metadata={"trap_risk": trap, "hype": hype,
                      "headlines_used": len(headlines),
                      "model": self._model, "cached": base in self._cache},
        )

    # ───────── جلب مع كاش 5–10 دقائق لكل رمز ─────────
    async def _headlines_and_grade(self, base: str) -> tuple[list[str], dict]:
        now = time.time()
        hit = self._cache.get(base)
        if hit and hit[0] > now:                       # كاش حي
            return hit[1], hit[2]

        headlines = await self._fetch_headlines(base)
        graded    = await self._llm_grade(headlines)
        self._cache[base] = (now + cfg.NEWS_CACHE_MIN * 60, headlines, graded)
        return headlines, graded

    async def _fetch_headlines(self, base: str) -> list[str]:
        params  = {"auth_token": cfg.CRYPTOPANIC_KEY, "currencies": base,
                   "filter": "rising", "kind": "news"}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(CRYPTOPANIC_URL, params=params) as r:
                data = await r.json()
        return [p["title"] for p in data.get("results", [])[:25]]

    async def _llm_grade(self, headlines: list[str]) -> dict:
        prompt = PROMPT_TEMPLATE.replace(
            "__HEADLINES__", "\n".join(headlines) or "N/A"
        )
        try:
            msg = await self._llm.messages.create(
                model=self._model, max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:                       # خطأ الموفر = فشل آمن واضح
            raise RuntimeError(
                f"sentiment provider error ({self._model}): {exc}"
            ) from exc

        text  = msg.content[0].text
        start = text.find("{")
        end   = text.rfind("}") + 1
        return json.loads(text[start:end]) if start >= 0 else {}
