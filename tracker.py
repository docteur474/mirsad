"""
tracker.py — المرحلة 3: تتبّع الإشارات المفتوحة حتى الخروج.
  • التخزين في SQLite (بدل jsonl): الإشارة + الأصوات + نتيجة R وسبب الإغلاق.
  • حلقة async تراقب السعر كل TRACK_SECONDS ثانية.
  • +1R: تنبيه جني جزئي 50% ونقل الوقف إلى التعادل تلقائياً.
  • "فكّر بالخروج المبكر" عند انقلاب وكيل إلى VETO/AVOID أثناء صفقة مفتوحة.
  • تحرير عدّادات المخاطر عبر risk.on_position_closed عند كل إغلاق.
ملاحظة التصميم: poll_once() مفصولة كي تكون قابلة لاختبار pytest مباشرة.
"""
from __future__ import annotations

import asyncio
import json
import time

import aiosqlite

import config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  entry REAL NOT NULL,
  stop REAL NOT NULL,
  target REAL NOT NULL,
  size REAL NOT NULL,
  r_unit REAL NOT NULL,          -- مسافة المخاطرة للوحدة (entry − stop0)
  risk_usd REAL NOT NULL,
  consensus REAL NOT NULL,
  votes_json TEXT NOT NULL,      -- أصوات الوكلاء لحظة الفتح (لتقرير الأداء)
  opened_at REAL NOT NULL,
  partial INTEGER DEFAULT 0,     -- تم جني 50% عند +1R؟
  be_moved INTEGER DEFAULT 0,    -- نُقل الوقف للتعادل؟
  early_warned INTEGER DEFAULT 0,
  status TEXT DEFAULT 'open',    -- open / closed
  close_reason TEXT,             -- TARGET / STOP / BREAKEVEN / TIMEOUT
  exit_price REAL,
  result_r REAL,
  closed_at REAL
);
"""


def cooldown_ok(last_ts: float | None, now: float, hours: float) -> bool:
    """منع التكرار: أول إشارة تمر، والتالية بعد انقضاء نافذة الهدوء."""
    return last_ts is None or (now - last_ts) >= hours * 3600


class TradeTracker:
    def __init__(self, market, notifier, risk, db_path: str = cfg.DB_PATH,
                 poll_seconds: int = cfg.TRACK_SECONDS) -> None:
        self.market   = market
        self.notifier = notifier
        self.risk     = risk
        self.db_path  = db_path
        self.poll_seconds = poll_seconds
        self._running = False

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ───────────────────── CRUD الإشارات ─────────────────────
    async def add_signal(self, proposal, consensus: float, votes: dict) -> int:
        r_unit = proposal.entry - proposal.stop_loss
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO signals
                   (symbol, entry, stop, target, size, r_unit, risk_usd,
                    consensus, votes_json, opened_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (proposal.symbol, proposal.entry, proposal.stop_loss,
                 proposal.take_profit, proposal.size, r_unit,
                 proposal.risk_usd, consensus,
                 json.dumps(votes, ensure_ascii=False), time.time()),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def has_open(self, symbol: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND status='open' LIMIT 1",
                (symbol,),
            )
            return await cur.fetchone() is not None

    async def open_signals(self) -> list[dict]:
        return await self._fetch("SELECT * FROM signals WHERE status='open'")

    async def open_symbols(self) -> list[str]:
        return [s["symbol"] for s in await self.open_signals()]

    async def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]

    # ───────────────────── حساب النتيجة بوحدة R ─────────────────────
    @staticmethod
    def _result_r(sig: dict, exit_price: float) -> float:
        base_r = (exit_price - sig["entry"]) / sig["r_unit"]
        # خصم تكلفة الذهاب والإياب بوحدة R — صدق محاسبي في التقارير
        cost_r = ((cfg.FEE_RATE + cfg.SLIPPAGE_BPS / 10_000)
                  * (sig["entry"] + exit_price)) / sig["r_unit"]
        tail_r = base_r - cost_r
        if sig["partial"]:                     # نصف الكمية خرج عند +1R مسبقاً
            return 0.5 * cfg.PARTIAL_TP_R + 0.5 * tail_r
        return tail_r

    # ───────────────────── حلقة المراقبة ─────────────────────
    async def monitor_loop(self, agent_state: dict) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_once(agent_state)
            except Exception:                             # noqa: BLE001
                pass    # أعطال لحظية لا تُسقط المراقب — تُعالج الدورة التالية
            await asyncio.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False

    async def poll_once(self, agent_state: dict) -> None:
        for sig in await self.open_signals():
            try:
                price = float((await self.market.fetch_ticker(sig["symbol"]))["last"])
            except Exception:                             # noqa: BLE001
                continue

            # 1) +1R: جني جزئي 50% + نقل الوقف إلى التعادل
            r1 = sig["entry"] + cfg.PARTIAL_TP_R * sig["r_unit"]
            if not sig["partial"] and price >= r1:
                await self.notifier.send_partial_tp(sig, price)
                await self._mark_partial(sig)
                sig["partial"], sig["be_moved"] = 1, 1
                sig["stop"] = sig["entry"]

            # 2) شروط الخروج القاطعة
            if price <= sig["stop"]:
                reason = "BREAKEVEN" if sig["be_moved"] else "STOP"
                await self._close(sig, reason, sig["stop"])
            elif price >= sig["target"]:
                await self._close(sig, "TARGET", sig["target"])
            elif time.time() - sig["opened_at"] >= cfg.TIMEOUT_HOURS * 3600:
                await self._close(sig, "TIMEOUT", price)
            else:
                # 3) انقلاب وكيل أثناء صفقة مفتوحة = إنذار خروج مبكر (مرة واحدة)
                worst = agent_state.get(sig["symbol"])
                if worst in ("VETO", "AVOID") and not sig["early_warned"]:
                    await self.notifier.send_early_warning(sig, worst, price)
                    await self._mark_warned(sig)
                    sig["early_warned"] = 1

    async def _close(self, sig: dict, reason: str, exit_price: float) -> None:
        result_r = self._result_r(sig, exit_price)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE signals SET status='closed', close_reason=?,
                   exit_price=?, result_r=?, closed_at=? WHERE id=?""",
                (reason, exit_price, result_r, time.time(), sig["id"]),
            )
            await db.commit()
        self.risk.on_position_closed(sig["risk_usd"])    # تحرير العدّادات
        await self.notifier.send_exit_alert(sig, reason, exit_price, result_r)

    async def _mark_partial(self, sig: dict) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE signals SET partial=1, be_moved=1, stop=? WHERE id=?",
                (sig["entry"], sig["id"]),
            )
            await db.commit()

    async def _mark_warned(self, sig: dict) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE signals SET early_warned=1 WHERE id=?",
                             (sig["id"],))
            await db.commit()

    # ───────────────────── إحصاءات التقارير ─────────────────────
    async def counts(self) -> dict:
        rows = await self._fetch(
            "SELECT status, COUNT(*) c, IFNULL(SUM(result_r),0) r FROM signals GROUP BY status"
        )
        out = {"open": 0, "closed": 0, "sum_r": 0.0}
        for row in rows:
            out[row["status"]] = row["c"]
            if row["status"] == "closed":
                out["sum_r"] = round(row["r"], 2)
        return out

    async def stats_since(self, since: float) -> dict:
        rows = await self._fetch(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN result_r > 0 THEN 1 ELSE 0 END) wins,
                      IFNULL(SUM(result_r),0) sum_r,
                      IFNULL(AVG(result_r),0) avg_r
               FROM signals WHERE status='closed' AND closed_at >= ?""",
            (since,),
        )
        row = rows[0]
        return {"n": row["n"], "wins": row["wins"] or 0,
                "sum_r": round(row["sum_r"], 2), "avg_r": round(row["avg_r"], 2)}

    async def agent_breakdown(self, since: float) -> dict[str, dict]:
        """دقة تصويت كل وكيل مقابل نتيجة الصفقة — يغذّي التقرير الأسبوعي."""
        rows = await self._fetch(
            "SELECT votes_json, result_r FROM signals WHERE status='closed' AND closed_at >= ?",
            (since,),
        )
        acc: dict[str, dict] = {}
        for row in rows:
            votes = json.loads(row["votes_json"])
            for name, v in votes.items():
                a = acc.setdefault(name, {"n": 0, "ok": 0})
                a["n"] += 1
                if (float(v["score"]) > 0) == (row["result_r"] > 0):
                    a["ok"] += 1
        for a in acc.values():
            a["acc"] = round(a["ok"] / max(a["n"], 1) * 100, 1)
        return acc
