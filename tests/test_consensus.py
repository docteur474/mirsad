"""
tests/test_consensus.py — منطق الفيتو والإجماع ومنع التكرار (cooldown).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg                                # noqa: E402
from agents.base_agent import AgentReport, Signal   # noqa: E402
from orchestrator import check_veto, compute_consensus  # noqa: E402
from tracker import cooldown_ok                     # noqa: E402


def rep(agent: str, score: float, conf: float = 1.0,
        signal: Signal = Signal.NEUTRAL) -> AgentReport:
    return AgentReport(agent=agent, symbol="BTC/USDT", score=score,
                       confidence=conf, signal=signal, reasoning="t")


# ─────────── الفيتو: صوت واحد يكفي ───────────
def test_explicit_veto_blocks_everything():
    reports = [rep("whale", 0.9), rep("technical", 0.9),
               rep("liquidity", -0.9, signal=Signal.VETO)]
    assert check_veto(reports).agent == "liquidity"


def test_score_below_threshold_is_veto():
    reports = [rep("whale", 0.9), rep("sentiment", cfg.VETO_THRESHOLD - 0.01)]
    assert check_veto(reports).agent == "sentiment"


def test_no_veto_on_borderline():
    reports = [rep("whale", cfg.VETO_THRESHOLD)]      # بالضبط على الحد = ليس فيتو
    assert check_veto(reports) is None


# ─────────── الإجماع الموزون وعقاب الثقة ───────────
def test_weighted_consensus_math():
    reports = [
        rep("whale",     0.5, 0.8),
        rep("sentiment", 0.5, 0.8),
        rep("liquidity", 0.5, 0.8),
        rep("technical", 0.5, 0.8),
        rep("derivatives", 0.5, 0.8),
    ]
    c = compute_consensus(reports)
    # raw = 0.5×0.8×(0.20+0.20+0.25+0.25+0.10) = 0.40 · عقاب الثقة يُغطى فوق 1
    assert abs(c - 0.40) < 1e-9


def test_zero_weight_agent_ignored():
    reports = [rep("ghost", 1.0, 1.0), rep("whale", 0.5, 1.0)]
    # ghost ليس في AGENT_WEIGHTS ⇒ وزنه صفر ولا يؤثر
    assert abs(compute_consensus(reports) - 0.5 * 1.0 * 0.20 * 1.0) < 0.03


def test_low_confidence_penalizes_consensus():
    hi = compute_consensus([rep("whale", 0.8, 1.0), rep("technical", 0.8, 1.0)])
    lo = compute_consensus([rep("whale", 0.8, 0.2), rep("technical", 0.8, 0.2)])
    assert lo < hi


# ─────────── منع التكرار: نافذة الهدوء ───────────
def test_cooldown_windows():
    now = time.time()
    assert cooldown_ok(None, now, 6) is True                       # أول مرة
    assert cooldown_ok(now - 5 * 3600, now, 6) is False            # داخل النافذة
    assert cooldown_ok(now - 7 * 3600, now, 6) is True             # بعدها
