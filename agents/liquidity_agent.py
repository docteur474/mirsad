"""
liquidity_agent.py — وكيل عمق السوق ودفتر الأوامر
[قواعد حسابية حتمية — لا نموذج لغوي؛ أُزيلت تسمية MODEL المضللة]
إصلاحات المرحلة 1:
  • حارس التبعية: غياب ccxt.pro لا يسقط النظام — رجوع صامت إلى REST.
  • إعادة استخدام اتصال WebSocket واحد بدل فتح اتصال في كل استدعاء.
المرحلة 4:
  • 3 لقطات عبر ~12 ثانية لكشف التلاعب (Spoofing) بدل لقطة واحدة:
    جدار يظهر ثم يختفي = دعم/مقاومة زائفة.
"""
from __future__ import annotations

import asyncio
import statistics

import ccxt.async_support as ccxt

try:                                    # حارس التبعية الاختيارية
    import ccxt.pro as ccxtpro
except ImportError:                     # غياب ccxt.pro = مسار REST كامل
    ccxtpro = None

import config as cfg
from agents.base_agent import AgentReport, BaseAgent, Signal


class LiquidityOrderbookAgent(BaseAgent):
    NAME  = "liquidity"
    DEPTH = 20

    def __init__(self, weight: float) -> None:
        super().__init__(weight)
        self._exchange = ccxt.binance({
            "apiKey": cfg.BINANCE_API_KEY,
            "secret": cfg.BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},       # SPOT فقط
        })
        self._ws = None                               # اتصال واحد يُعاد استخدامه

    async def close(self) -> None:
        await self._drop_ws()
        await self._exchange.close()

    # ───────────────── التحليل الرئيسي ─────────────────
    async def analyze(self, symbol: str) -> AgentReport:
        snaps = [await self._snapshot(symbol)]
        for _ in range(cfg.SPOOF_SNAPSHOTS - 1):
            await asyncio.sleep(cfg.SPOOF_STEP_SEC)   # ~6s × لقطتين ≈ 12s
            snaps.append(await self._snapshot(symbol))

        bids, asks = snaps[-1]["bids"][: self.DEPTH], snaps[-1]["asks"][: self.DEPTH]
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid        = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid * 10_000

        # متوسط اختلال الميزان عبر اللقطات — أصدق من لقطة واحدة
        imbalances = []
        for s in snaps:
            bv = sum(a for _, a in s["bids"][: self.DEPTH])
            av = sum(a for _, a in s["asks"][: self.DEPTH])
            imbalances.append((bv - av) / max(bv + av, 1e-9))
        imbalance = statistics.mean(imbalances)

        # الجدران عبر الزمن: ثابت = حقيقي · متلاشٍ = تلاعب محتمل
        bid_walls = [self._detect_wall(s["bids"][: self.DEPTH]) for s in snaps]
        ask_walls = [self._detect_wall(s["asks"][: self.DEPTH]) for s in snaps]
        spoof_bid = self._spoofed(bid_walls)          # دعم زائف = فخ
        spoof_ask = self._spoofed(ask_walls)          # مقاومة زائفة = تخويف

        if spread_bps > 8:
            return self._veto(symbol, f"toxic spread {spread_bps:.1f} bps")
        if spoof_bid:
            return self._veto(symbol, "spoofed bid-wall vanished — fake support")

        score = imbalance * 1.4
        if bid_walls[-1] and not spoof_bid and bid_walls[-1]["distance_pct"] < 0.6:
            score += 0.25
        if ask_walls[-1] and ask_walls[-1]["distance_pct"] < 0.6:
            score -= 0.35
        if spoof_ask:
            score += 0.10                             # خوف مُصطنع يذوب = إيجابي حذر

        if score >= 0.50:
            signal = Signal.STRONG_BUY
        elif score >= 0.20:
            signal = Signal.BUY
        elif score <= -0.30:
            signal = Signal.AVOID
        else:
            signal = Signal.NEUTRAL

        return AgentReport(
            agent=self.NAME, symbol=symbol,
            score=max(-1.0, min(1.0, score)),
            confidence=min(0.50 + abs(imbalance), 0.95),
            signal=signal,
            reasoning=(
                f"imbalance {imbalance:+.2f} · spread {spread_bps:.1f}bps · "
                f"spoof bid/ask {spoof_bid}/{spoof_ask} · "
                f"snapshots {len(snaps)} @ {cfg.SPOOF_STEP_SEC}s"
            ),
            metadata={"mid": mid, "spread_bps": spread_bps, "imbalance": imbalance,
                      "spoof_bid": spoof_bid, "spoof_ask": spoof_ask,
                      "transport": "ws" if self._ws else "rest"},
        )

    # ───────── لقطة واحدة: WS معاد الاستخدام، ومعه احتياطي REST ─────────
    async def _snapshot(self, symbol: str) -> dict:
        if ccxtpro is not None:
            try:
                if self._ws is None:
                    self._ws = ccxtpro.binance({"options": {"defaultType": "spot"}})
                return await self._ws.watch_order_book(symbol, self.DEPTH)
            except Exception:                         # noqa: BLE001
                await self._drop_ws()                 # إسقاط المعطوب ثم REST
        return await self._exchange.fetch_order_book(symbol, self.DEPTH)

    async def _drop_ws(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:                             # noqa: BLE001
            pass
        self._ws = None

    # ───────── كشف الجدار والتلاعب ─────────
    @staticmethod
    def _detect_wall(levels: list, factor: float = 3.0) -> dict | None:
        if len(levels) < 5:
            return None
        mean = statistics.mean(amount for _, amount in levels)
        price, amount = max(levels, key=lambda lvl: lvl[1])
        if amount < mean * factor:
            return None
        return {"price": price, "amount": amount,
                "distance_pct": abs(price - levels[0][0]) / levels[0][0] * 100}

    @staticmethod
    def _spoofed(walls: list[dict | None]) -> bool:
        """تلاعب = جدار واضح في البداية اختفى أو ذاب حجمه 70%+ في النهاية."""
        first, last = walls[0], walls[-1]
        if not first:
            return False
        if last is None:
            return True
        same_zone = abs(last["price"] - first["price"]) / first["price"] < 0.004
        return same_zone and last["amount"] < first["amount"] * 0.30

    def _veto(self, symbol: str, why: str) -> AgentReport:
        return AgentReport(agent=self.NAME, symbol=symbol, score=-1.0,
                           confidence=1.0, signal=Signal.VETO, reasoning=why)
