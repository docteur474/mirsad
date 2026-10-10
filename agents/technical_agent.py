"""
technical_agent.py — الوكيل الفني الجديد (المرحلة 4)
[قواعد حسابية حتمية — باندا-ستايل نقي بلمكتبة القياسية فقط]
يقرأ: هيكل السعر (قيعان/قمم صاعدة)، المتوسطات (SMA20/50)، RSI(14)،
نسبة الحجم، وتوافق إطار 4h — مع فيتو صريح عند الاختراق الكاذب.
"""
from __future__ import annotations

import ccxt.async_support as ccxt

import config as cfg
from agents.base_agent import AgentReport, BaseAgent, Signal


def sma(xs: list[float], n: int) -> float:
    return sum(xs[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float:
    """RSI بسيط (Wilder-lite) على شموع مغلقة فقط."""
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / n
    avg_l = sum(losses) / n or 1e-9
    return 100 - 100 / (1 + avg_g / avg_l)


class TechnicalStructureAgent(BaseAgent):
    NAME = "technical"

    def __init__(self, weight: float) -> None:
        super().__init__(weight)
        self._ex = ccxt.binance({"enableRateLimit": True,
                                 "options": {"defaultType": "spot"}})

    async def close(self) -> None:
        await self._ex.close()

    # ───────────────── التحليل الرئيسي ─────────────────
    async def analyze(self, symbol: str) -> AgentReport:
        o1 = await self._ex.fetch_ohlcv(symbol, "1h", limit=120)
        o4 = await self._ex.fetch_ohlcv(symbol, cfg.CONFIRM_TIMEFRAME, limit=60)
        c1, c4 = o1[:-1], o4[:-1]                     # شموع مغلقة فقط

        closes = [c[4] for c in c1]
        vols   = [c[5] for c in c1]
        price  = closes[-1]

        ma20, ma50 = sma(closes, 20), sma(closes, 50)
        rsi14      = rsi(closes, 14)
        vol_ratio  = vols[-1] / max(sma(vols, 20), 1e-9)
        trend4h    = c4[-1][4] > sma([c[4] for c in c4], cfg.CONFIRM_SMA)
        hi_hi, hi_lo = self._structure(c1)

        # اختراق كاذب: ذيل فوق قمة 20 شمعة ثم إغلاق تحتها بحجم ضخم
        prev_high  = max(c[2] for c in c1[-21:-1])
        fake_break = c1[-1][2] > prev_high and price < prev_high and vol_ratio > 1.5

        if fake_break:
            return AgentReport(agent=self.NAME, symbol=symbol, score=-1.0,
                               confidence=0.9, signal=Signal.VETO,
                               reasoning="fake breakout above 20-bar high — spike faded")

        agree = 0
        score = 0.0
        score += 0.30 if price > ma20 > ma50 else (-0.30 if price < ma20 < ma50 else 0.0)
        agree += price > ma20 > ma50
        score += 0.25 if hi_lo and hi_hi else (-0.25 if not hi_lo and not hi_hi else 0.0)
        agree += hi_lo and hi_hi
        if rsi14 > 70:
            score -= 0.25                                # تشبّع شرائي = مطاردة خطرة
        elif rsi14 < 30:
            score += 0.15                                # ارتداد من إفراط بيعي
        elif 50 <= rsi14 <= 65:
            score += 0.20
            agree += 1
        score += 0.15 if vol_ratio > 1.3 else (-0.10 if vol_ratio < 0.6 else 0.0)
        score += 0.20 if trend4h else -0.20              # بوابة الإطار الأعلى
        agree += trend4h

        if score >= 0.55:
            signal = Signal.STRONG_BUY
        elif score >= 0.25:
            signal = Signal.BUY
        elif score <= -0.35:
            signal = Signal.AVOID
        else:
            signal = Signal.NEUTRAL

        return AgentReport(
            agent=self.NAME, symbol=symbol,
            score=max(-1.0, min(1.0, score)),
            confidence=min(0.45 + agree * 0.12, 0.95),
            signal=signal,
            reasoning=(f"ma-stack {'up' if price > ma20 > ma50 else 'mixed'} · "
                       f"RSI {rsi14:.0f} · vol x{vol_ratio:.1f} · "
                       f"4h {'aligned' if trend4h else 'against'}"),
            metadata={"rsi": rsi14, "vol_ratio": vol_ratio,
                      "trend_4h": trend4h, "structure": (hi_hi, hi_lo)},
        )

    # ───────── هيكل القمم والقيعان على محاور من 5 شموع ─────────
    @staticmethod
    def _structure(candles: list) -> tuple[bool, bool]:
        highs = [c[2] for c in candles[-60:]]
        lows  = [c[3] for c in candles[-60:]]
        piv_h = [highs[i] for i in range(2, len(highs) - 2)
                 if highs[i] == max(highs[i - 2:i + 3])]
        piv_l = [lows[i] for i in range(2, len(lows) - 2)
                 if lows[i] == min(lows[i - 2:i + 3])]
        hi_hi = len(piv_h) >= 2 and piv_h[-1] > piv_h[-2]
        hi_lo = len(piv_l) >= 2 and piv_l[-1] > piv_l[-2]
        return hi_hi, hi_lo
