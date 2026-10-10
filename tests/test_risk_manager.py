"""
tests/test_risk_manager.py — إلزام المرحلة 1 والـ ATR.
يشغّل:  pytest tests/test_risk_manager.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg                      # noqa: E402
from risk.manager import RiskManager, RiskViolation  # noqa: E402


def flat_bars(n: int, base: float = 100.0, spread: float = 10.0) -> list:
    """شموع مغلقة بنطاق ثابت: TR = spread دائماً تقريباً."""
    bars, price = [], base
    for _ in range(n):
        bars.append([0, price, price + spread / 2,
                     price - spread / 2, price + 1.0, 100.0])
        price += 1.0
    return bars


# ─────────── حساب ATR ───────────
def test_atr_constant_true_range():
    bars = flat_bars(cfg.ATR_PERIOD + 2, spread=10.0)
    atr = RiskManager.atr(bars)
    # النطاق + الإغلاق السابق المتزايد يجعل TR ≈ 11 لكل شمعة
    assert 10.0 <= atr <= 12.0


def test_atr_requires_enough_candles():
    with pytest.raises(RiskViolation):
        RiskManager.atr(flat_bars(cfg.ATR_PERIOD))   # شمعة ناقصة


# ─────────── سقف الحجم على الرصيد المتاح ───────────
def test_volume_cap_shrinks_to_available_balance():
    rm = RiskManager()
    proposal = rm.build_long_proposal(
        "BTC/USDT", equity=10_000.0, available_usdt=200.0,
        entry=100.0, atr=10.0,
    )
    assert proposal.capped is True
    assert proposal.size * proposal.entry <= 200.0 * (1 - cfg.FEE_RATE) + 1e-9
    assert any("capped" in n for n in proposal.notes)   # يُبلَّغ في البطاقة


def test_uncapped_when_balance_sufficient():
    rm = RiskManager()
    proposal = rm.build_long_proposal(
        "BTC/USDT", equity=10_000.0, available_usdt=50_000.0,
        entry=100.0, atr=10.0,
    )
    assert proposal.capped is False


def test_zero_balance_rejected():
    rm = RiskManager()
    with pytest.raises(RiskViolation):
        rm.build_long_proposal("BTC/USDT", 10_000.0, 0.0, 100.0, 10.0)


# ─────────── R:R الفعلي بعد الرسوم والانزلاق ───────────
def test_net_rr_meets_floor_on_normal_volatility():
    rm = RiskManager()
    p = rm.build_long_proposal("BTC/USDT", 10_000.0, 50_000.0, 100.0, 10.0)
    assert p.rr_net >= cfg.MIN_RISK_REWARD
    # الصافي دون الإجمالي دائماً — التكاليف حقيقية
    assert p.rr_net < p.rr_gross


def test_net_rr_rejects_when_fees_devour_edge():
    rm = RiskManager()
    # ATR متناهٍ: مسافة الوقف أصغر من التكاليف ⇒ R:R صافٍ كارثي ⇒ رفض
    with pytest.raises(RiskViolation):
        rm.build_long_proposal("BTC/USDT", 10_000.0, 50_000.0, 100.0, 0.0001)


def test_portfolio_ceiling_and_counters():
    rm = RiskManager()
    rm.build_long_proposal("BTC/USDT", 10_000.0, 50_000.0, 100.0, 10.0)
    assert rm.open_positions == 1
    rm.on_position_closed(rm.open_risk_usd)
    assert rm.open_positions == 0 and rm.open_risk_usd == 0.0
