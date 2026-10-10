"""
telegram_notifier.py — قناة الاتصال v2.
قاعدة صلبة: كل رسالة تُختم بتنبيه مخاطرة — لا وعود بربح، والتنفيذ يدوي.
الجديد: تنبيهات الخروج/الجزئي/المبكر، تغيّر الكون، التقارير اليومية
والأسبوعية، الـ heartbeat، وتنبيه تعطّل الوكلاء.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

import config as cfg

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

RISK_NOTE = (
    "\n\n<i>تنبيه مخاطرة: إشارة تحليلية وليست توصية أو وعداً بربح —"
    " التنفيذ اليدوي قرارك ومسؤوليتك.</i>"
)


class TelegramNotifier:
    def __init__(self) -> None:
        self._url = API_URL.format(token=cfg.TELEGRAM_BOT_TOKEN)

    # ───────── إرسال خام مع إعادة محاولة أسّية ×3 ─────────
    async def send_text(self, html: str) -> None:
        payload = {
            "chat_id": cfg.TELEGRAM_CHAT_ID,
            "text": html + RISK_NOTE,              # القيد 3: تنبيه إلزامي
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        timeout = aiohttp.ClientTimeout(total=10)
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.post(self._url, json=payload) as r:
                        if r.status == 200:
                            return
            except Exception:                           # noqa: BLE001
                pass
            await asyncio.sleep(2 ** attempt)

    # ───────── بطاقة الإشارة (تعرض الصافي بعد الرسوم والسقف) ─────────
    async def send_signal(self, symbol: str, consensus: float,
                          reports: list, proposal) -> None:
        votes = "\n".join(
            f"• <b>{r.agent:>11}</b> │ {r.signal.value:<10} │ {r.score:+.2f}"
            for r in reports
        )
        cap_line = ("\nتنبيه الحجم: قُلّص ليتوافق مع الرصيد المتاح"
                    if proposal.capped else "")
        notes = "".join(f"\n• {n}" for n in proposal.notes)
        html = (
            f"<b>⟡ MIRSAD · إشارة شراء — {symbol}</b>\n"
            f"{'─' * 28}\n{votes}\n{'─' * 28}\n"
            f"الإجماع الموزون  : <b>{consensus:+.2f}</b>\n"
            f"الدخول           : <code>{proposal.entry:,.4f}</code>\n"
            f"الوقف (1.5×ATR)  : <code>{proposal.stop_loss:,.4f}</code>\n"
            f"الهدف            : <code>{proposal.take_profit:,.4f}</code>\n"
            f"R:R صافٍ بعد التكاليف: <b>1 : {proposal.rr_net:.2f}</b>\n"
            f"المخاطرة الصافية : {proposal.risk_usd:,.0f} USDT\n"
            f"الحجم            : {proposal.size:.5f}"
            f"{cap_line}{notes}"
        )
        await self.send_text(html)

    # ───────── تنبيهات التتبّع (المرحلة 3) ─────────
    async def send_exit_alert(self, sig: dict, reason: str,
                              exit_price: float, result_r: float) -> None:
        label = {"TARGET": "بلوغ الهدف", "STOP": "ضرب الوقف",
                 "BREAKEVEN": "وقف التعادل", "TIMEOUT": "انتهاء المهلة"}[reason]
        mark  = "+" if result_r >= 0 else ""
        html = (
            f"<b>[EXIT] بِع الآن — {sig['symbol']}</b>\n"
            f"السبب   : {label}\n"
            f"الخروج  : <code>{exit_price:,.4f}</code>\n"
            f"النتيجة : <b>{mark}{result_r:.2f}R</b>\n"
            f"المدة   : {(time.time() - sig['opened_at']) / 3600:.1f} ساعة"
        )
        await self.send_text(html)

    async def send_partial_tp(self, sig: dict, price: float) -> None:
        await self.send_text(
            f"<b>[+1R] جني جزئي — {sig['symbol']}</b>\n"
            f"السعر بلغ <code>{price:,.4f}</code> = +1R\n"
            f"الإجراء المقترح: بِع 50% وانقل الوقف إلى التعادل "
            f"(<code>{sig['entry']:,.4f}</code>) — تم تسجيل ذلك."
        )

    async def send_early_warning(self, sig: dict, worst: str, price: float) -> None:
        await self.send_text(
            f"<b>[WARN] فكّر بالخروج المبكر — {sig['symbol']}</b>\n"
            f"وكيل انقلب إلى <b>{worst}</b> والصفقة مفتوحة.\n"
            f"السعر الحالي: <code>{price:,.4f}</code> │ الدخول: "
            f"<code>{sig['entry']:,.4f}</code>"
        )

    # ───────── الفيتو (مُجمّع من المنسّق: رسالة/رمز/ساعة كحد أقصى) ─────────
    async def send_veto_alert(self, symbol: str, agent: str, why: str) -> None:
        await self.send_text(
            f"<b>[VETO] منع الإشارة — {symbol}</b>\n"
            f"الوكيل: <code>{agent}</code>\nالسبب: {why}"
        )

    # ───────── المرحلة 2: تغيّر الكون ─────────
    async def send_universe_update(self, symbols: list[str]) -> None:
        rows = "\n".join(f"  {i}. <code>{s}</code>" for i, s in enumerate(symbols, 1))
        await self.send_text(
            f"<b>[UNIVERSE] كون المراقبة تحدّث</b>\n{rows}"
        )

    # ───────── المرحلة 5: التقارير والنبض ─────────
    async def send_daily_report(self, stats: dict, breakdown: dict) -> None:
        lines = "\n".join(
            f"• {name}: دقة {a['acc']}% عبر {a['n']} صفقة"
            for name, a in breakdown.items()
        ) or "لا بيانات كافية بعد"
        await self.send_text(
            f"<b>[DAILY] التقرير اليومي</b>\n"
            f"إشارات مغلقة: {stats['n']} │ رابحة: {stats['wins']}\n"
            f"المجموع: <b>{stats['sum_r']:+.2f}R</b> │ المتوسط: {stats['avg_r']:+.2f}R\n"
            f"{'─' * 24}\n{lines}"
        )

    async def send_weekly_report(self, stats: dict, breakdown: dict) -> None:
        await self.send_daily_report(stats, breakdown)   # نفس البنية بعنوان أسبوعي

    async def send_heartbeat(self, agents: list, started: float, counts: dict) -> None:
        rows = "\n".join(
            f"  {'●' if a.healthy else '○'} {a.NAME} ({a.weight})"
            for a in agents
        )
        uptime_h = (time.time() - started) / 3600
        await self.send_text(
            f"<b>[PING] النظام حي</b> — uptime {uptime_h:.1f}h\n"
            f"{rows}\nمفتوحة: {counts['open']} │ مغلقة: {counts['closed']}"
            f" │ مجموع R: {counts['sum_r']:+.2f}"
        )

    async def send_agent_down(self, name: str) -> None:
        await self.send_text(
            f"<b>[DOWN] وكيل متعثر: <code>{name}</code></b>\n"
            f"تجاوز نافذة الصحة — صوته حيال بثقة صفرية، لا قرار صامت."
        )

    async def send_error(self, where: str, exc: Exception) -> None:
        await self.send_text(
            f"<b>[ERR] خطأ في {where}</b>\n<code>{type(exc).__name__}: {exc}</code>"
        )
