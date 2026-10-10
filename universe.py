"""
universe.py — المرحلة 2: الانتقاء الديناميكي لكون التداول.
يرتّب أزواج USDT الفورية حسب السيولة (حجم التداول) ويستبعد الرموز
المستقرة والعملات ذات الرافعة الوهمية، مع قائمة احتياطية عند أي عطل.
"""
from __future__ import annotations

import time

import config as cfg

# قواعد استبعاد صارمة: عملات مستقرة/ورقية ورموز الرافعة الوهمية
BASE_BLACKLIST = {
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "TRY", "BRL", "AEUR",
}
LEVERAGED_SUFFIX = ("UP", "DOWN", "BEAR", "BULL")


class UniverseSelector:
    def __init__(self, market, n: int = cfg.UNIVERSE_SIZE,
                 fallback: list[str] | None = None,
                 refresh_min: int = cfg.UNIVERSE_REFRESH_MIN) -> None:
        self.market   = market
        self.n        = n
        self.fallback = list(fallback or cfg.SYMBOLS)
        self.refresh_sec = refresh_min * 60
        self._symbols: list[str] = []
        self._last_refresh = 0.0

    # ───────── الواجهة التي يستهلكها المنسّق كل دورة ─────────
    async def get_symbols(self) -> list[str]:
        if self._symbols and time.time() - self._last_refresh < self.refresh_sec:
            return self._symbols
        try:
            ranked = await self._rank_by_liquidity()
            self._symbols = ranked[: self.n] if ranked else list(self.fallback)
        except Exception:                             # noqa: BLE001
            self._symbols = list(self.fallback)       # عطل الجلب = الاحتياطي
        self._last_refresh = time.time()
        return self._symbols

    # ───────── ترتيب USDT pairs حسب الحجم مع الفلاتر ─────────
    async def _rank_by_liquidity(self) -> list[str]:
        tickers = await self.market.fetch_tickers()
        rows: list[tuple[float, str]] = []
        for sym, t in tickers.items():
            if not sym.endswith(f"/{cfg.QUOTE}"):
                continue
            base = sym.split("/")[0]
            if base in BASE_BLACKLIST or base.endswith(LEVERAGED_SUFFIX):
                continue
            volume = float(t.get("quoteVolume") or 0.0)
            rows.append((volume, sym))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [sym for _, sym in rows]
