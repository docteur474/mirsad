"""
orchestrator.py — المنسّق v2 (إشارات فقط — لا create_order في النظام كله).
الترتيب الصارم لكل رمز كل دورة:
  منع تكرار/cooldown ← فلتر نظام BTC ← الوكلاء بالتوازي ← فيتو ←
  إجماع موزون ← عتبة LONG ← تأكيد 4h ← فلتر الارتباط ← بوابة المخاطر
  (شموع مغلقة + صافي RR + سقف رصيد) ← تسجيل SQLite + بطاقة تيليجرام.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt

import config as cfg
from agents.base_agent import AgentReport, Signal
from agents.derivatives_agent import DerivativesReadOnlyAgent
from agents.liquidity_agent import LiquidityOrderbookAgent
from agents.sentiment_agent import NewsSentimentAgent
from agents.technical_agent import TechnicalStructureAgent
from agents.whale_agent import WhaleOnChainAgent
from risk.manager import RiskManager, RiskViolation
from telegram_notifier import TelegramNotifier
from tracker import TradeTracker, cooldown_ok
from universe import UniverseSelector

log = logging.getLogger("MIRSAD")


# ───────── وظائف نقية — معزولة لتُختبر مباشرة بـ pytest ─────────
def check_veto(reports: list[AgentReport]) -> AgentReport | None:
    """فيتو صريح أو درجة تحت العتبة = إسقاط فوري بصوت واحد."""
    return next(
        (r for r in reports
         if r.signal is Signal.VETO or r.score < cfg.VETO_THRESHOLD),
        None,
    )


def compute_consensus(reports: list[AgentReport]) -> float:
    """إجماع موزون score×confidence×weight + عقاب ضعف الثقة الجماعية."""
    raw = sum(r.weighted_score * cfg.AGENT_WEIGHTS.get(r.agent, 0.0)
              for r in reports)
    conf = sum(r.confidence for r in reports) / max(len(reports), 1)
    return raw * min(conf * 1.4, 1.0)


def pearson(x: list[float], y: list[float]) -> float:
    """ارتباط بيرسون للعوائد — يغذّي فلتر الارتباط بين الصفقات المفتوحة."""
    n = min(len(x), len(y))
    if n < 10:
        return 0.0
    x, y = x[-n:], y[-n:]
    mx, my = statistics.mean(x), statistics.mean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    denom = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return 0.0 if denom == 0 else sum(a * b for a, b in zip(dx, dy)) / denom


class MultiAgentOrchestrator:
    def __init__(self) -> None:
        self.agents = [
            WhaleOnChainAgent(cfg.AGENT_WEIGHTS["whale"]),
            NewsSentimentAgent(cfg.AGENT_WEIGHTS["sentiment"]),
            LiquidityOrderbookAgent(cfg.AGENT_WEIGHTS["liquidity"]),
            TechnicalStructureAgent(cfg.AGENT_WEIGHTS["technical"]),
            DerivativesReadOnlyAgent(cfg.AGENT_WEIGHTS["derivatives"]),
        ]
        self.risk      = RiskManager()
        self.notifier  = TelegramNotifier()
        self.market    = ccxt.binance({"enableRateLimit": True,
                                       "options": {"defaultType": "spot"}})
        self.tracker   = TradeTracker(self.market, self.notifier, self.risk)
        # المرحلة 2: الكون الديناميكي مع قائمة احتياطية
        self.universe  = UniverseSelector(self.market, n=cfg.UNIVERSE_SIZE,
                                          fallback=cfg.SYMBOLS)

        self._started_at       = time.time()
        self._running          = False
        self._tasks: list[asyncio.Task] = []
        self._known_universe:  list[str] = []
        self._last_signal_at:  dict[str, float] = {}
        self._last_veto_alert: dict[str, float] = {}
        self._agent_state:     dict[str, str] = {}   # أسوأ صوت لكل رمز (للتتبع)
        self._regime_cache:    tuple[float, bool] = (0.0, True)
        self._down_reported:   set[str] = set()
        self._last_heartbeat   = 0.0
        self._last_daily:      str = ""
        self._last_weekly:     int = -1

    # ═════════ دورة قرار رمز واحد ═════════
    async def decide_symbol(self, symbol: str) -> None:
        now = time.time()
        # (1) منع التكرار: صفقة مفتوحة أو cooldown 6 ساعات
        if await self.tracker.has_open(symbol):
            return
        if not cooldown_ok(self._last_signal_at.get(symbol), now,
                           cfg.SIGNAL_COOLDOWN_HOURS):
            return
        # (2) فلتر الاتجاه العام قبل حرق أربعة وكلاء (المرحلة 4)
        if not await self._btc_regime_ok():
            log.info("%s skipped — BTC below daily SMA50 regime", symbol)
            return

        # (3) الوكلاء بالتوازي — تقرير موحد، والفشل الآمن ضمن run()
        reports: list[AgentReport] = await asyncio.gather(
            *(agent.run(symbol) for agent in self.agents)
        )
        worst = min(reports, key=lambda r: r.score)
        self._agent_state[symbol] = worst.signal.value   # يقرأه tracker

        # (4) الفيتو — مع تجميع التنبيه: رسالة واحدة/رمز/ساعة
        veto = check_veto(reports)
        if veto:
            log.warning("VETO %s by %s :: %s", symbol, veto.agent, veto.reasoning)
            if now - self._last_veto_alert.get(symbol, 0.0) >=                     cfg.VETO_ALERT_THROTTLE_MIN * 60:
                self._last_veto_alert[symbol] = now
                await self.notifier.send_veto_alert(symbol, veto.agent, veto.reasoning)
            return

        # (5) الإجماع الموزون + العتبة
        consensus = compute_consensus(reports)
        log.info("%s consensus=%+.2f", symbol, consensus)
        if consensus < cfg.LONG_THRESHOLD:
            return

        # (6) تأكيد الإطار الأعلى (7) فلتر الارتباط
        if not await self._mtf_confirm(symbol):
            log.info("%s rejected — %s trend not aligned", symbol, cfg.CONFIRM_TIMEFRAME)
            return
        twin = await self._correlated_with_open(symbol)
        if twin:
            log.info("%s rejected — correlated %.2f with open %s", symbol, 1.0, twin)
            return

        # (8) بوابة المخاطر على شموع مغلقة فقط
        proposal = await self._risk_gate(symbol)
        if proposal is None:
            return

        # (9) تسجيل + إشعار —signals only، المتداول ينفذ يدوياً
        votes = {r.agent: {"score": r.score, "signal": r.signal.value} for r in reports}
        await self.tracker.add_signal(proposal, consensus, votes)
        await self.notifier.send_signal(symbol, consensus, reports, proposal)
        self._last_signal_at[symbol] = now

    # ═════════ بوابات الجودة (المرحلة 4) ═════════
    async def _btc_regime_ok(self) -> bool:
        """BTC تحت SMA50 اليومي = سوق دفاعي، لا شراء (قابل للتعطيل)."""
        if not cfg.BTC_REGIME_FILTER:
            return True
        ts, ok = self._regime_cache
        if time.time() - ts < 600:                       # كاش 10 دقائق
            return ok
        try:
            daily = await self.market.fetch_ohlcv("BTC/USDT", "1d", limit=60)
            closed = [c[4] for c in daily[:-1]]
            sma50 = sum(closed[-50:]) / 50
            ok = closed[-1] >= sma50
        except Exception:                                 # noqa: BLE001
            ok = True          # تعذّر الفحص = لا نجمّد السوق كله
        self._regime_cache = (time.time(), ok)
        return ok

    async def _mtf_confirm(self, symbol: str) -> bool:
        """اتجاه الإطار الأعلى يجب أن يوافق إشارة 1h."""
        try:
            bars = await self.market.fetch_ohlcv(
                symbol, cfg.CONFIRM_TIMEFRAME, limit=cfg.CONFIRM_SMA + 5)
            closes = [c[4] for c in bars[:-1]]
            return closes[-1] >= sum(closes[-cfg.CONFIRM_SMA:]) / cfg.CONFIRM_SMA
        except Exception:                                 # noqa: BLE001
            return False

    async def _correlated_with_open(self, symbol: str) -> str | None:
        """لا إشارتين لعملتين مترابطتين فوق CORR_LIMIT — تركيز مخفي = مخاطرة مضاعفة."""
        for other in await self.tracker.open_symbols():
            if other == symbol:
                continue
            try:
                a = [c[4] for c in (await self.market.fetch_ohlcv(symbol, "1h", limit=100))[:-1]]
                b = [c[4] for c in (await self.market.fetch_ohlcv(other,  "1h", limit=100))[:-1]]
                ra = [x / y - 1 for x, y in zip(a[1:], a[:-1])]
                rb = [x / y - 1 for x, y in zip(b[1:], b[:-1])]
                if pearson(ra, rb) > cfg.CORR_LIMIT:
                    return other
            except Exception:                             # noqa: BLE001
                continue
        return None

    # ═════════ بوابة المخاطر: شموع مغلقة فقط + رصيد متاح ═════════
    async def _risk_gate(self, symbol: str):
        ohlcv = await self.market.fetch_ohlcv(symbol, "1h",
                                              limit=cfg.ATR_PERIOD + 5)
        closed = ohlcv[:-1]               # إصلاح إلزامي: استبعاد الشمعة الجارية
        entry  = float(closed[-1][4])
        atr    = RiskManager.atr(closed)
        equity, available = await self._balances()
        try:
            return self.risk.build_long_proposal(
                symbol, equity, available, entry, atr)
        except RiskViolation as rv:
            log.info("risk gate blocked %s :: %s", symbol, rv)
            return None

    async def _balances(self) -> tuple[float, float]:
        """(القيمة الكلية، USDT المتاح الحر) — محاكاة افتراضية في DRY_RUN."""
        if cfg.DRY_RUN:
            return 10_000.0, 10_000.0
        bal = await self.market.fetch_balance()
        total = float(bal["total"].get(cfg.QUOTE, 0.0))
        free  = float(bal["free"].get(cfg.QUOTE, 0.0))
        return total, free

    # ═════════ حلقات التشغيل ═════════
    async def loop_forever(self) -> None:
        await self.tracker.init()
        self._running = True
        self._tasks = [
            asyncio.create_task(self.tracker.monitor_loop(self._agent_state)),
            asyncio.create_task(self._services_loop()),
        ]
        while self._running:
            cycle = time.perf_counter()
            symbols = await self.universe.get_symbols()
            if symbols != self._known_universe:
                if self._known_universe:                # لا تنبيه عند الإقلاع
                    await self.notifier.send_universe_update(symbols)
                self._known_universe = list(symbols)
            await asyncio.gather(
                *(self.decide_symbol(s) for s in symbols),
                return_exceptions=True,
            )
            await asyncio.sleep(max(cfg.LOOP_SECONDS - (time.perf_counter() - cycle), 5))

    async def _services_loop(self) -> None:
        """heartbeat ساعي + تقارير يومية/أسبوعية + كشف تعطّل الوكلاء."""
        while self._running:
            await asyncio.sleep(30)
            now = time.time()
            utc = datetime.now(timezone.utc)

            if now - self._last_heartbeat >= cfg.HEARTBEAT_MIN * 60:
                self._last_heartbeat = now
                await self.notifier.send_heartbeat(
                    self.agents, self._started_at, await self.tracker.counts())

            for a in self.agents:                        # صوت المتعطل ≠ صامت
                if not a.healthy and a.NAME not in self._down_reported:
                    self._down_reported.add(a.NAME)
                    await self.notifier.send_agent_down(a.NAME)
                elif a.healthy:
                    self._down_reported.discard(a.NAME)

            today = utc.date().isoformat()
            if utc.hour == cfg.DAILY_REPORT_HOUR_UTC and self._last_daily != today:
                self._last_daily = today
                await self.notifier.send_daily_report(
                    await self.tracker.stats_since(now - 86_400),
                    await self.tracker.agent_breakdown(now - 86_400))

            week = utc.isocalendar().week
            if (utc.weekday() == cfg.WEEKLY_REPORT_WEEKDAY
                    and utc.hour == cfg.DAILY_REPORT_HOUR_UTC
                    and self._last_weekly != week):
                self._last_weekly = week
                await self.notifier.send_weekly_report(
                    await self.tracker.stats_since(now - 7 * 86_400),
                    await self.tracker.agent_breakdown(now - 7 * 86_400))

    async def shutdown(self) -> None:
        self._running = False
        self.tracker.stop()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for a in self.agents:
            try:
                await a.close()
            except Exception:                             # noqa: BLE001
                pass
        await self.market.close()
