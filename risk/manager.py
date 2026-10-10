"""
risk/manager.py — حارس رأس المال v2 (إصلاحات المرحلة 1 إلزامية)
  1) سقف الحجم: القيمة الاسمية لا تتجاوز رصيد USDT المتاح بعد احتياطي الرسوم.
  2) ATR والدخول على شموع مغلقة فقط (الاستبعاد يتم عند المُستدعي بالمنسّق).
  3) R:R فعلي بعد الرسوم والانزلاق، ورفض صريح تحت MIN_RISK_REWARD.
البنية والواجهات محفوظة التوافق (TradeProposal / RiskManager / RiskViolation).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config as cfg


class RiskViolation(Exception):
    """يُرفع عند كسر أي قاعدة — الإشارة تُقتل في المهد، لا استثناءات."""


@dataclass
class TradeProposal:
    symbol:      str
    side:        str = "LONG"      # SPOT: شراء فقط — لا شورت في المعمارية
    entry:       float = 0.0
    stop_loss:   float = 0.0
    take_profit: float = 0.0
    size:        float = 0.0
    risk_usd:    float = 0.0       # صافي المخاطرة بعد التكاليف
    reward_usd:  float = 0.0       # صافي العائد بعد التكاليف
    rr_gross:    float = 0.0
    rr_net:      float = 0.0       # الفعلي بعد الرسوم والانزلاق
    est_fees:    float = 0.0       # تقدير الرسوم ذهاباً وإياباً
    atr:         float = 0.0
    capped:      bool = False      # هل قُلّص الحجم بسبب الرصيد؟
    notes:       list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self) -> None:
        self.open_risk_usd  = 0.0
        self.open_positions = 0

    # ───────── ATR من True Range — يتوقع شموع مغلقة فقط ─────────
    @staticmethod
    def atr(ohlcv: list[list[float]], period: int = cfg.ATR_PERIOD) -> float:
        if len(ohlcv) < period + 1:
            raise RiskViolation("not enough closed candles for ATR")
        trs: list[float] = []
        for prev, cur in zip(ohlcv[-period - 1: -1], ohlcv[-period:]):
            high, low, prev_close = cur[2], cur[3], prev[4]
            trs.append(max(high - low,
                           abs(high - prev_close),
                           abs(low  - prev_close)))
        return sum(trs) / len(trs)

    @staticmethod
    def _cost_frac(price: float, size: float) -> float:
        """رسوم + انزلاق جهة واحدة (شراء أو بيع)."""
        return price * size * (cfg.FEE_RATE + cfg.SLIPPAGE_BPS / 10_000)

    # ───────── بناء مقترح يجتاز كل البوابات أو لا يولد ─────────
    def build_long_proposal(self, symbol: str, equity: float,
                            available_usdt: float,
                            entry: float, atr: float) -> TradeProposal:
        notes: list[str] = []
        if not cfg.SPOT_ONLY:
            raise RiskViolation("SPOT_ONLY guard breached")
        if self.open_positions >= cfg.MAX_POSITIONS:
            raise RiskViolation("max open positions reached")
        if atr <= 0 or entry <= 0:
            raise RiskViolation("invalid ATR / entry price")

        stop      = entry - cfg.ATR_SL_MULTIPLIER * atr
        risk_dist = entry - stop
        # (ج) نبني الهدف الإجمالي بوسادة فوق الأرضية ثم نتحقق من الصافي
        gross_rr  = cfg.MIN_RISK_REWARD + cfg.RR_SAFETY_BUFFER
        target    = entry + gross_rr * risk_dist

        # تحجيم أولي على مخاطرة 1% من المحفظة
        size = (equity * cfg.RISK_PER_TRADE) / risk_dist

        # (أ) سقف الحجم على الرصيد المتاح مع احتياطي الرسوم 0.1%
        max_notional = max(available_usdt * (1.0 - cfg.FEE_RATE), 0.0)
        if size * entry > max_notional:
            size   = max_notional / entry
            capped = True
            notes.append("capped: reduced to fit available USDT balance")
        else:
            capped = False
        if size <= 0:
            raise RiskViolation("insufficient available balance")

        # (ج) العائد والمخاطرة صافيةً بعد رسوم وانزلاق الذهاب والإياب
        cost_in   = self._cost_frac(entry, size)
        cost_tp   = self._cost_frac(target, size)
        cost_sl   = self._cost_frac(stop, size)
        reward_net = (target - entry) * size - cost_in - cost_tp
        risk_net   = risk_dist * size + cost_in + cost_sl
        rr_net     = reward_net / risk_net

        if rr_net < cfg.MIN_RISK_REWARD:               # الرفض الصريح الإلزامي
            raise RiskViolation(
                f"net R:R {rr_net:.2f} below floor {cfg.MIN_RISK_REWARD} after fees"
            )
        if self.open_risk_usd + risk_net > equity * cfg.MAX_TOTAL_RISK:
            raise RiskViolation("portfolio risk ceiling breached")

        proposal = TradeProposal(
            symbol=symbol, entry=entry, stop_loss=stop, take_profit=target,
            size=size, risk_usd=risk_net, reward_usd=reward_net,
            rr_gross=gross_rr, rr_net=rr_net,
            est_fees=cost_in + cost_tp, atr=atr, capped=capped, notes=notes,
        )
        self.open_risk_usd  += risk_net
        self.open_positions += 1
        return proposal

    def on_position_closed(self, risk_usd: float) -> None:
        """يحرّر العدّادات — يستدعيه tracker عند إغلاق كل صفقة."""
        self.open_risk_usd  = max(0.0, self.open_risk_usd - risk_usd)
        self.open_positions = max(0, self.open_positions - 1)
