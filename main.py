"""
main.py — نقطة الإطلاق (إشارات فقط — لا create_order في الكود كله).
التشغيل:
  1) cp .env.example .env وعبّئ المفاتيح
  2) pip install -r requirements.txt
  3) pytest -q            # اختبارات المخاطر والإجماع والتتبع قبل أي تشغيل
  4) python main.py
"""
import asyncio
import logging
import signal

import config as cfg
from orchestrator import MultiAgentOrchestrator
from telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
)


async def main() -> None:
    notifier     = TelegramNotifier()
    orchestrator = MultiAgentOrchestrator()

    await notifier.send_text(
        "⟡ MIRSAD online — SIGNALS ONLY · SPOT · "
        f"universe={cfg.UNIVERSE_SIZE} · heartbeat={cfg.HEARTBEAT_MIN}m"
    )

    # إيقاف آمن: إلغاء المهام، إغلاق القنوات، رسالة وداع
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.ensure_future(orchestrator.shutdown())
        )

    try:
        await orchestrator.loop_forever()
    finally:
        await notifier.send_text("⟡ MIRSAD offline — إيقاف آمن مكتمل")


if __name__ == "__main__":
    asyncio.run(main())
