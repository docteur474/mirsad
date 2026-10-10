"""
base_agent.py — العقد الأساسي لجميع الوكلاء (واجهة محفوظة التوافق).
يوحّد التقرير، يفرض المهلة، يطبّق الفشل الآمن، ويضيف نبض صحة للمراقبة.
"""
from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Signal(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY        = "BUY"
    NEUTRAL    = "NEUTRAL"
    AVOID      = "AVOID"
    VETO       = "VETO"   # صوت واحد كافٍ لإسقاط الإشارة


@dataclass
class AgentReport:
    """وحدة القياس الموحّدة بين الوكلاء والمنسّق."""
    agent:      str
    symbol:     str
    score:      float                 # [-1.0, +1.0]
    confidence: float                 # [0.0, 1.0]
    signal:     Signal
    reasoning:  str
    latency_ms: float = 0.0
    metadata:   dict[str, Any] = field(default_factory=dict)

    @property
    def weighted_score(self) -> float:
        return self.score * self.confidence


class BaseAgent(abc.ABC):
    """run() تغلّف analyze() بمهلة وحماية — أي عطل = حياد بثقة صفرية."""

    NAME:        str = "base"
    TIMEOUT_SEC: int = 30
    HEALTHY_WINDOW_SEC: int = 300     # نافذة اعتبار الوكيل "حياً" للـ heartbeat

    def __init__(self, weight: float) -> None:
        self.weight = weight
        self.last_ok_at: float = 0.0  # آخر نجاح — يغذّي مراقب الأعطال

    @property
    def healthy(self) -> bool:
        """حي = نجح خلال النافذة؛ يستخدمه المنسّق لتنبيه تعطّل الوكلاء."""
        return (time.time() - self.last_ok_at) < self.HEALTHY_WINDOW_SEC

    async def run(self, symbol: str) -> AgentReport:
        started = time.perf_counter()
        try:
            report = await asyncio.wait_for(
                self.analyze(symbol), timeout=self.TIMEOUT_SEC
            )
            self.last_ok_at = time.time()
        except Exception as exc:                       # noqa: BLE001
            report = self._failover(symbol, exc)
        report.latency_ms = (time.perf_counter() - started) * 1000
        return report

    async def close(self) -> None:
        """نقطة تحرير الموارد — تُعاد كتابتها عند الحاجة (WebSocket مثلاً)."""

    @abc.abstractmethod
    async def analyze(self, symbol: str) -> AgentReport:
        raise NotImplementedError

    def _failover(self, symbol: str, exc: Exception) -> AgentReport:
        return AgentReport(
            agent=self.NAME, symbol=symbol,
            score=0.0, confidence=0.0, signal=Signal.NEUTRAL,
            reasoning=f"FAILOVER :: {type(exc).__name__}: {exc}",
        )
