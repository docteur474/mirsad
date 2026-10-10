"""
whale_agent.py — وكيل الحيتان والتدفقات On-Chain
[قواعد حسابية حتمية — لا نموذج لغوي؛ أُزيلت تسمية MODEL المضللة]
المصادر: Arkham Intelligence + Whale Alert API
يكشف: التجميع المؤسسي، تدفقات USDT/USDC، وضغط التوزيع على المنصات.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

import config as cfg
from agents.base_agent import AgentReport, BaseAgent, Signal

# TODO(verify): تحقّق من نقطة النهاية والمعاملات مقابل التوثيق الرسمي لـ Arkham
# قبل التشغيل الحقيقي — قد تختلف المسارات وأسماء الحقول بين إصدارات الـ API.
ARKHAM_URL = "https://api.arkhamintelligence.com/transfers"

# TODO(verify): تحقّق من معاملات min_value / currency / start في توثيق
# Whale Alert v1 — قد يتطلب الحساب المجاني خططاً أو حقولاً إضافية.
WHALE_URL = "https://api.whale-alert.io/v1/transactions"


class WhaleOnChainAgent(BaseAgent):
    NAME = "whale"

    # ───────────────── التحليل الرئيسي ─────────────────
    async def analyze(self, symbol: str) -> AgentReport:
        base = symbol.split("/")[0]
        async with aiohttp.ClientSession() as http:
            flows, alerts = await asyncio.gather(
                self._arkham_exchange_flows(http, base),
                self._whale_alert_feed(http, base),
            )

        accumulating = flows["net_exchange_outflow_usd"] > 0
        stable_power = flows["stablecoin_inflow_usd"]   # قوة شرائية واردة
        big_moves    = alerts["large_tx_count"]
        dump_risk    = alerts["exchange_inflow_usd"]    # حيتان → منصات = بيع محتمل

        # — قواعد التسجيل الحتمية —
        score = 0.0
        score += 0.45 if accumulating else -0.25
        score += min(stable_power / 500_000_000, 1.0) * 0.35
        score += min(big_moves / 10, 1.0) * 0.20
        score -= min(dump_risk / 300_000_000, 1.0) * 0.60
        score = max(-1.0, min(1.0, score))

        if dump_risk > 300_000_000:                     # تفريغ حاد = فيتو
            signal = Signal.VETO
        elif score >= 0.55:
            signal = Signal.STRONG_BUY
        elif score >= 0.20:
            signal = Signal.BUY
        elif score <= -0.30:
            signal = Signal.AVOID
        else:
            signal = Signal.NEUTRAL

        return AgentReport(
            agent=self.NAME, symbol=symbol, score=score,
            confidence=0.60 + min(big_moves / 20, 0.40),
            signal=signal,
            reasoning=(
                f"netflow {flows['net_exchange_outflow_usd'] / 1e6:+.1f}M · "
                f"stables-in +{stable_power / 1e6:.0f}M · whale-tx {big_moves}"
            ),
            metadata={**flows, **alerts},
        )

    # ───────── تدفقات Arkham بين المحافظ والمنصات ─────────
    async def _arkham_exchange_flows(self, http, base: str) -> dict:
        # TODO(verify): أسماء حقول الاستجابة (usdValue/toExchange/fromExchange/unit)
        params  = {"base": base, "flow": "all", "limit": 200}
        headers = {"API-Key": cfg.ARKHAM_API_KEY}
        async with http.get(self.ARKHAM_URL, params=params, headers=headers) as r:
            data = await r.json()

        transfers = data.get("transfers", [])
        inflow  = sum(t["usdValue"] for t in transfers if t["toExchange"])
        outflow = sum(t["usdValue"] for t in transfers if t["fromExchange"])
        stables = sum(
            t["usdValue"] for t in transfers
            if t["unit"] in ("USDT", "USDC") and t["toExchange"]
        )
        return {
            "net_exchange_outflow_usd": outflow - inflow,
            "stablecoin_inflow_usd":    stables,
        }

    # ───────── إنذارات الحيتان اللحظية ─────────
    async def _whale_alert_feed(self, http, base: str) -> dict:
        params = {
            "api_key":   cfg.WHALE_ALERT_KEY,
            "min_value": 500_000,
            "currency":  base.lower(),
            "start":     int(time.time()) - 3600,
        }
        async with http.get(self.WHALE_URL, params=params) as r:
            data = await r.json()

        txs = data.get("transactions", [])
        to_exchange = sum(
            t["amount_usd"] for t in txs
            if t.get("to", {}).get("owner_type") == "exchange"
        )
        return {"large_tx_count": len(txs), "exchange_inflow_usd": to_exchange}
