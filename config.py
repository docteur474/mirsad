"""
═══════════════════════════════════════════════════════════════
  MIRSAD · مرصاد — Multi-Agent SIGNALS System (SPOT · SIGNALS-ONLY)
  config.py — v2: كل ثابت قابل للضبط من مكان واحد، والأسرار من .env
═══════════════════════════════════════════════════════════════
"""
import os

from dotenv import load_dotenv

load_dotenv()  # لا سرّ داخل الكود إطلاقاً

# ─────────────────────── مفاتيح المنصات ───────────────────────
BINANCE_API_KEY     = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET  = os.getenv("BINANCE_API_SECRET", "")
ARKHAM_API_KEY      = os.getenv("ARKHAM_API_KEY", "")
WHALE_ALERT_KEY     = os.getenv("WHALE_ALERT_API_KEY", "")
CRYPTOPANIC_KEY     = os.getenv("CRYPTOPANIC_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────── النماذج اللغوية المعتمدة فعلاً ───────────
# إصلاح المرحلة 1: اسم النموذج يُضبط من .env مع قيمة افتراضية صالحة
SENTIMENT_MODEL = os.getenv("SENTIMENT_MODEL", "claude-3-5-sonnet-20241022")
MODEL_CODER     = "qwen3-coder-480b-a35b-instruct"   # مهندس الهيكلة (توثيقي)
# ملاحظة: whale / liquidity / technical / derivatives وكلاء قواعد حسابية
# حتمية ولا يستهلكون نموذجاً لغوياً — أُزيلت تسمية MODEL المضللة عنها.

# ─────────────────────── التداول (إشارات فقط) ───────────────────────
SYMBOLS   = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
QUOTE     = "USDT"
SPOT_ONLY = True     # قفل صارم: لا Margin ولا Futures ولا شورت
DRY_RUN   = True     # توثيقي: النظام لا يستدعي create_order في كل الأحوال

# ─────────────────── قواعد إدارة المخاطر الصارمة ───────────────────
RISK_PER_TRADE    = 0.01    # مخاطرة 1% من رأس المال لكل إشارة
MAX_TOTAL_RISK    = 0.05    # سقف المخاطرة المفتوحة الإجمالية
MAX_POSITIONS     = 3
ATR_PERIOD        = 14
ATR_SL_MULTIPLIER = 1.5     # الوقف = الدخول − (1.5 × ATR14)
MIN_RISK_REWARD   = 2.5     # الأرضية الصلبة — تُقاس بعد الرسوم والانزلاق
RR_SAFETY_BUFFER  = 0.20    # الهدف الإجمالي 2.7R ليبقى الصافي ≥ 2.5
FEE_RATE          = 0.001   # رسوم Spot لكل جهة = 0.1%
SLIPPAGE_BPS      = 5       # انزلاق مقدّر = 5 نقاط أساس للجهة
TIMEOUT_HOURS     = 48

# ─────────────────── الإجماع — 5 وكلاء، الأوزان مجموعها 1.0 ───────────────────
AGENT_WEIGHTS = {
    "whale":       0.20,
    "sentiment":   0.20,
    "liquidity":   0.25,
    "technical":   0.25,   # وكيل فني جديد (المرحلة 4)
    "derivatives": 0.10,   # قراءة فقط: funding / open interest
}
LONG_THRESHOLD = 0.65    # عتبة مبدئية — تُعاير من نتائج walk-forward
VETO_THRESHOLD = -0.60
LOOP_SECONDS   = 60

# ─────────── المرحلة 1: صحة التشغيل ───────────
NEWS_CACHE_MIN         = 8    # كاش الأخبار ونتيجة LLM لكل رمز (5–10 دقائق)
SIGNAL_COOLDOWN_HOURS  = 6    # منع التكرار: لا إشارة جديدة لرمز لديه إشارة/حديثة
VETO_ALERT_THROTTLE_MIN = 60  # تجميع الفيتو: رسالة واحدة لكل رمز كل ساعة

# ─────────── المرحلة 2: الكون الديناميكي ───────────
UNIVERSE_SIZE        = 5
UNIVERSE_REFRESH_MIN = 15

# ─────────── المرحلة 3: تتبّع الصفقات ───────────
TRACK_SECONDS = 30      # مراقبة السعر كل 30 ثانية
PARTIAL_TP_R  = 1.0     # جني جزئي 50% عند +1R
BREAKEVEN_R   = 1.0     # نقل الوقف إلى التعادل عند +1R
DB_PATH       = "mirsad.db"

# ─────────── المرحلة 4: جودة الإشارة ───────────
BTC_REGIME_FILTER = True   # لا شراء وBTC تحت SMA50 اليومي (قابل للتعطيل)
CONFIRM_TIMEFRAME = "4h"   # تأكيد اتجاه الإطار الأعلى
CONFIRM_SMA       = 20
CORR_LIMIT        = 0.90   # منع إشارتين لعملتين مترابطتين فوق هذا الحد
SPOOF_SNAPSHOTS   = 3      # لقطات دفتر الأوامر لكشف التلاعب
SPOOF_STEP_SEC    = 6      # فارق اللقطات ≈ 12–18 ثانية إجمالاً

# ─────────── المرحلة 5: القياس والتشغيل ───────────
HEARTBEAT_MIN         = 60    # نبضة حياة كل ساعة
DAILY_REPORT_HOUR_UTC = 8     # موعد التقرير اليومي (UTC)
WEEKLY_REPORT_WEEKDAY = 0     # التقرير الأسبوعي: الاثنين
BACKTEST_DAYS         = 370   # عمق تاريخي ≥ سنة للاختبار
