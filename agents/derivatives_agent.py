"""
derivatives_agent.py — وكيل بيانات المشتقات (قراءة فقط — المرحلة 4)
يقرأ Funding Rate و Open Interest كفلاتر مخاطرة نفسية/تمويلية للسوق.
قيود صارمة: لا تداول مشتقات إطلاقاً — القراءة لخدمة إشارات SPOT فقط،
وفيتو عند تطرّف الـ funding (ازدحام مراكز رافعة = انعكاس وشيك).
"""
from __future__ import annotations

import ccxt.async_support as ccxt

import config as cfg
from agents.base_agent import AgentReport, BaseAgent, Signal

FUNDING_EXTREME = 0.001    # 0.10% كل 8 ساعات = طول/شراء مكتظ بالرافعة
FUNDING_HOT     = 0.0005   # 0.05% — ساخن لكنه لا يستحق الفيتو وحده


class DerivativesReadOnlyAgent(BaseAgent):
    NAME = "derivatives"

    def __init__(self, weight: float) -> None:
        super().__init__(weight)
        # عميل عام للقراءة فقط — نفس رمز Binance الافتراضي للمشتقات للبيانات
        self._ex = ccxt.binanceusdm({"enableRateLimit": True})

    async def close(self) -> None:
        await self._ex.close()

    # ───────────────── التحليل الرئيسي ─────────────────
    async def analyze(self, symbol: str) -> AgentReport:
        funding = await self._funding(symbol)
        oi_chg, px_chg = await self._oi_and_price(symbol)

        score = 0.0
        # 1) التمويل: موجب مرتفع = الجميع شراء بالرافعة = خطر انعكاس
        if funding >= FUNDING_EXTREME:
            return AgentReport(agent=self.NAME, symbol=symbol, score=-1.0,
                               confidence=0.9, signal=Signal.VETO,
                               reasoning=f"extreme funding {funding * 100:+.3f}% — crowded longs")
        if funding >= FUNDING_HOT:
            score -= 0.40
        elif funding <= -FUNDING_EXTREME:
            score += 0.25      # شورت مكتظ: وقود عصر محتمل — إيجابية محدودة
        elif abs(funding) < 0.0001:
            score += 0.10      # تمويل محايد = سوق نظيف

        # 2) OI × السعر: صعود الاثنين صحي · OI يصعد والسعر جامد = ضغط انفجار
        if oi_chg > 0.05 and px_chg > 0.01:
            score += 0.35
        elif oi_chg > 0.05 and abs(px_chg) <= 0.01:
            score -= 0.30
        elif oi_chg < -0.05 and px_chg < -0.01:
            score -= 0.20      # تصفية مراكز = الاتجاه يفقد وقوده

        if score >= 0.35:
            signal = Signal.BUY
        elif score <= -0.30:
            signal = Signal.AVOID
        else:
            signal = Signal.NEUTRAL

        return AgentReport(
            agent=self.NAME, symbol=symbol,
            score=max(-1.0, min(1.0, score)),
            confidence=0.65,
            signal=signal,
            reasoning=(f"funding {funding * 100:+.3f}% · "
                       f"OI {oi_chg * 100:+.1f}% · px {px_chg * 100:+.1f}%"),
            metadata={"funding": funding, "oi_change": oi_chg, "px_change": px_chg},
        )

    # ───────── funding الحالي (قراءة فقط) ─────────
    async def _funding(self, symbol: str) -> float:
        try:
            data = await self._ex.fetch_funding_rate(symbol)
            return float(data.get("fundingRate") or 0.0)
        except Exception:                             # noqa: BLE001
            return 0.0    # غياب البيانات = محايد لا إسقاط

    # ───────── تغير OI والسعر خلال 24 ساعة ─────────
    async def _oi_and_price(self, symbol: str) -> tuple[float, float]:
        try:
            hist = await self._ex.fetch_open_interest_history(
                symbol, timeframe="1h", limit=25
            )
            oi_now  = float(hist[-1]["openInterestValue"])
            oi_prev = float(hist[0]["openInterestValue"])
            oi_chg  = (oi_now - oi_prev) / max(oi_prev, 1e-9)
        except Exception:                             # noqa: BLE001
            oi_chg = 0.0  # بعض الأزواج بلا تاريخ OI عام — TODO(verify) التغطية

        bars = await self._ex.fetch_ohlcv(symbol, "1h", limit=25)
        px_chg = (bars[-1][4] - bars[0][4]) / max(bars[0][4], 1e-9)
        return oi_chg, px_chg
