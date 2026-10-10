"""
backtest.py — المرحلة 5: اختبار تاريخي محافظ (walk-forward).
  • بيانات 1h لمدة BACKTEST_DAYS يوماً (≥ سنة) مع رسوم 0.1% وانزلاق 5bps.
  • إدارة صفقة مطابقة للنظام: وقف ATR، هدف إجمالي، تعادل عند +1R،
    جني جزئي 50%، ومهلة زمنية — النتائج بوحدة R الصافية.
  • تقسيم walk-forward: معايرة العتبة على Train ثم تقييم Test المتداول،
    ولا تدريب وتقييم على البيانات نفسها إطلاقاً.
  • حدود موثّقة: بيانات الحيتان/الأخبار التاريخية غير متاحة مجاناً بعمق سنة،
    لذا يقود المحاكاةَ وكيلٌ فني بديل (TODO قرار المستخدم: اشتراك بيانات).
التشغيل:  python backtest.py
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time

import ccxt.async_support as ccxt

import config as cfg

# ──────────────────── جلب التاريخ مع ترقيم الصفحات ────────────────────
async def fetch_history(market, symbol: str, tf: str = "1h",
                        days: int = cfg.BACKTEST_DAYS) -> list[list[float]]:
    since = int((time.time() - days * 86_400) * 1000)
    out: list[list[float]] = []
    while True:
        batch = await market.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        out.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    return out


# ──────────────────── مؤشرات نقية على نوافذ مغلقة ────────────────────
def sma(xs: list[float], n: int) -> float:
    return sum(xs[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float:
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n or 1e-9
    return 100 - 100 / (1 + ag / al)


def tech_score(closes: list[float], vols: list[float], i: int) -> float:
    """نفس منطق الوكيل الفني تقريباً على النافذة المنتهية عند i."""
    w  = closes[:i]
    wv = vols[:i]
    if len(w) < 60:
        return 0.0
    price = w[-1]
    score = 0.0
    score += 0.30 if price > sma(w, 20) > sma(w, 50) else 0.0
    r = rsi(w)
    score += 0.20 if 50 <= r <= 65 else (-0.25 if r > 70 else 0.0)
    v = wv[-1] / max(sma(wv, 20), 1e-9)
    score += 0.15 if v > 1.3 else (-0.10 if v < 0.6 else 0.0)
    score += 0.20 if price > sma(w, min(200, len(w) - 1)) else -0.20
    return score


# ──────────────────── محاكاة الصفقات بقواعد النظام ────────────────────
def simulate(bars: list[list[float]], threshold: float) -> list[float]:
    results: list[float] = []
    closes = [b[4] for b in bars]
    vols   = [b[5] for b in bars]
    i      = 60
    while i < len(bars) - cfg.TIMEOUT_HOURS - 2:
        if tech_score(closes, vols, i) < threshold:
            i += 1
            continue

        closed = bars[max(0, i - cfg.ATR_PERIOD - 1):i + 1]
        trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4]))
               for p, c in zip(closed[:-1], closed[1:])]
        atr   = sum(trs[-cfg.ATR_PERIOD:]) / cfg.ATR_PERIOD
        entry = bars[i + 1][1]                       # الدخول بافتتاح الشمعة التالية
        stop  = entry - cfg.ATR_SL_MULTIPLIER * atr
        r_unit = entry - stop
        target = entry + (cfg.MIN_RISK_REWARD + cfg.RR_SAFETY_BUFFER) * r_unit
        cost_f = cfg.FEE_RATE + cfg.SLIPPAGE_BPS / 10_000

        partial = False
        outcome: float | None = None
        for j in range(i + 1, i + 1 + cfg.TIMEOUT_HOURS):
            low, high = bars[j][3], bars[j][2]
            if not partial and high >= entry + r_unit:
                partial = True
                stop = entry                          # وقف التعادل
            if low <= stop:
                out_p = stop
                outcome = (-(r_unit) / r_unit) - (2 * cost_f * entry / r_unit)
                if partial:
                    outcome = 0.5 * 1.0 + 0.5 * outcome
                break
            if high >= target:
                tail = (target - entry) / r_unit - (2 * cost_f * entry / r_unit)
                outcome = 0.5 * 1.0 + 0.5 * tail if partial else tail
                break
        if outcome is None:                            # مهلة زمنية
            out_p = bars[i + 1 + cfg.TIMEOUT_HOURS][4]
            tail = (out_p - entry) / r_unit - (2 * cost_f * entry / r_unit)
            outcome = 0.5 * 1.0 + 0.5 * tail if partial else tail

        results.append(round(outcome, 3))
        i = i + 1 + cfg.TIMEOUT_HOURS                  # لا تداخل صفقات بالمحاكاة
    return results


# ──────────────────── المقاييس المطلوبة ────────────────────
def metrics(results: list[float], span_days: float) -> dict:
    n = len(results)
    if n < 5:
        return {"n": n}
    wins    = sum(1 for r in results if r > 0)
    avg_r   = statistics.mean(results)
    equity, peak, max_dd = 0.0, 0.0, 0.0               # تراجع أقصى على منحنى R
    for r in results:
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    std = statistics.stdev(results) or 1e-9
    sharpe = avg_r / std * math.sqrt(n / max(span_days / 365, 1e-9))
    return {"n": n, "win": round(wins / n * 100, 1),
            "avg_r": round(avg_r, 3), "max_dd": round(max_dd, 2),
            "sharpe": round(sharpe, 2), "sum_r": round(sum(results), 2)}


# ──────────────────── walk-forward: معايرة ثم اختبار على غير المرئي ────────────────────
GRID = [0.55, 0.60, 0.65, 0.70]


def walk_forward(bars: list[list[float]], folds: int = 4) -> list[dict]:
    rows: list[dict] = []
    seg = len(bars) // (folds + 2)
    for k in range(folds):
        train = bars[: seg * (k + 2)]
        test  = bars[seg * (k + 2): seg * (k + 3)]
        # المعايرة على بيانات التدريب فقط
        best = max(GRID, key=lambda t: statistics.mean(simulate(train, t) or [0])
                   if len(simulate(train, t)) >= 8 else -9)
        out  = simulate(test, best)
        m    = metrics(out, span_days=len(test) / 24)
        m["fold"], m["thr"] = k + 1, best
        rows.append(m)
    return rows


WARN = """╔══════════════════════════════════════════════════════════════╗
║  تحذير إفراط الملاءمة (OVERFITTING)                          ║
║  الأوزان والعتبات المقترحة إرشادية من عينة محدودة، وقد لا    ║
║  تصمد خارج العينة. لا تعتمدها قبل تشغيل forward-test أسبوعين. ║
╚══════════════════════════════════════════════════════════════╝"""


async def main() -> None:
    market = ccxt.binance({"enableRateLimit": True})
    chosen: list[float] = []
    try:
        for symbol in cfg.SYMBOLS[:3]:
            bars = await fetch_history(market, symbol)
            print(f"\n══ {symbol} — {len(bars)} شمعة 1h ══")
            rows = walk_forward(bars)
            for r in rows:
                print(f"  fold {r['fold']} thr={r['thr']:.2f} │ "
                      f"n={r.get('n')} win={r.get('win')}% "
                      f"avgR={r.get('avg_r')} maxDD={r.get('max_dd')} "
                      f"sharpe={r.get('sharpe')} sumR={r.get('sum_r')}")
                chosen.append(r["thr"])
        if chosen:
            print(f"\nاقتراح معاير: LONG_THRESHOLD ≈ "
                  f"{statistics.median(chosen):.2f} (إرشادي فقط)")
        print("\n" + WARN)
    finally:
        await market.close()


if __name__ == "__main__":
    asyncio.run(main())
