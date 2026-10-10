"""
tests/test_tracker.py — التتبع: وقف/هدف/مهلة/تعادل + منع تكرار الاشارات المفتوحة.
تعمل دون شبكة: سوق ومرسل مزيّفان، وقاعدة SQLite مؤقتة.
"""
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg                       # noqa: E402
from risk.manager import RiskManager       # noqa: E402
from tracker import TradeTracker           # noqa: E402


@dataclass
class FakeProposal:
    symbol: str = "BTC/USDT"
    entry: float = 100.0
    stop_loss: float = 90.0                 # r_unit = 10
    take_profit: float = 127.0              # 2.7R إجمالي
    size: float = 1.0
    risk_usd: float = 100.0


class FakeMarket:
    price = 100.0

    async def fetch_ticker(self, symbol):
        return {"last": self.price}


class FakeNotifier:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def rec(*a, **k):
            self.calls.append(name)
        return rec


def run(coro):
    return asyncio.run(coro)


def make_tracker(tmp_path):
    rm   = RiskManager()
    rm.open_positions, rm.open_risk_usd = 1, 100.0
    mkt  = FakeMarket()
    note = FakeNotifier()
    tr   = TradeTracker(mkt, note, rm, db_path=str(tmp_path / "t.db"),
                        poll_seconds=9999)
    run(tr.init())
    votes = {"whale": {"score": 0.5, "signal": "BUY"}}
    run(tr.add_signal(FakeProposal(), 0.66, votes))
    return tr, mkt, note, rm


# ─────────── منع التكرار: إشارة مفتوحة لنفس الرمز ───────────
def test_open_signal_blocks_duplicate(tmp_path):
    tr, *_ = make_tracker(tmp_path)
    assert run(tr.has_open("BTC/USDT")) is True
    assert run(tr.has_open("ETH/USDT")) is False


# ─────────── ضرب الوقف = بِع الآن بنتيجة سالبة ≈ -1R ───────────
def test_stop_hit_closes_with_minus_one_r(tmp_path):
    tr, mkt, note, rm = make_tracker(tmp_path)
    mkt.price = 89.0
    run(tr.poll_once({}))
    assert run(tr.has_open("BTC/USDT")) is False     # أُغلقت
    assert "send_exit_alert" in note.calls           # وصل تنبيه البيع
    assert rm.open_positions == 0                     # تحرّرت العدّادات


# ─────────── بلوغ الهدف = نتيجة موجبة ≥ 2.4R صافية للجزئي ───────────
def test_target_hit_records_profit(tmp_path):
    tr, mkt, note, _ = make_tracker(tmp_path)
    mkt.price = 130.0
    run(tr.poll_once({}))
    rows = run(tr._fetch("SELECT close_reason, result_r FROM signals"))
    assert rows[0]["close_reason"] == "TARGET"
    assert 1.7 < rows[0]["result_r"] < 1.85


# ─────────── +1R: جني جزئي ووقف تعادل قبل أي خروج ───────────
def test_partial_and_breakeven_move(tmp_path):
    tr, mkt, note, _ = make_tracker(tmp_path)
    mkt.price = 111.0                                 # فوق +1R مباشرة
    run(tr.poll_once({}))
    assert "send_partial_tp" in note.calls
    row = run(tr._fetch("SELECT partial, be_moved, stop FROM signals"))[0]
    assert row["partial"] == 1 and row["be_moved"] == 1
    assert row["stop"] == 100.0                       # الوقف صار نقطة الدخول


def test_breakeven_stop_after_partial(tmp_path):
    tr, mkt, note, _ = make_tracker(tmp_path)
    mkt.price = 111.0
    run(tr.poll_once({}))
    mkt.price = 99.5                                  # رجوع تحت التعادل
    run(tr.poll_once({}))
    row = run(tr._fetch("SELECT status, close_reason, result_r FROM signals"))[0]
    assert row["status"] == "closed" and row["close_reason"] == "BREAKEVEN"
    assert row["result_r"] > 0.4                      # حصّنّا نصف الربح على الأقل


# ─────────── المهلة الزمنية تُغلق الصفقة الراكدة ───────────
def test_timeout_closes_stale_signal(tmp_path):
    tr, mkt, note, _ = make_tracker(tmp_path)
    run(tr._fetch("SELECT 1"))                        # ضمان فتح القاعدة
    import aiosqlite  # noqa: E402 — تعديل مباشر لعمر الإشارة
    async def age():
        async with aiosqlite.connect(tr.db_path) as db:
            await db.execute(
                "UPDATE signals SET opened_at=?",
                (time.time() - (cfg.TIMEOUT_HOURS + 1) * 3600,))
            await db.commit()
    run(age())
    mkt.price = 104.0
    run(tr.poll_once({}))
    row = run(tr._fetch("SELECT close_reason FROM signals"))[0]
    assert row["close_reason"] == "TIMEOUT"


# ─────────── انقلاب وكيل = إنذار خروج مبكر مرة واحدة ───────────
def test_early_warning_fires_once(tmp_path):
    tr, mkt, note, _ = make_tracker(tmp_path)
    mkt.price = 103.0
    run(tr.poll_once({"BTC/USDT": "VETO"}))
    run(tr.poll_once({"BTC/USDT": "VETO"}))
    assert note.calls.count("send_early_warning") == 1
