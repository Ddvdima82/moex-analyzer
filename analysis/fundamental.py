"""
Фундаментальный анализ акций.
Данные берутся из статического файла data/fundamentals.json.
Дивидендная доходность рассчитывается через MOEX API.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from config import FUNDAMENTALS_FILE, FUNDAMENTALS_MAX_AGE_DAYS, today_msk

logger = logging.getLogger(__name__)

# Числовые поля, обязательные для скоринга (div_yield добавляется в рантайме)
_REQUIRED_NUMERIC = ("pe_ratio", "debt_ebitda", "roe_pct", "net_margin_pct")


# ──────────────────────────────────────────────────────────────
# Загрузка и валидация данных
# ──────────────────────────────────────────────────────────────

def _validate_entry(ticker: str, data: Any) -> bool:
    """Проверяет одну запись fundamentals. True — запись пригодна для скоринга."""
    if not isinstance(data, dict):
        logger.warning("fundamentals[%s]: не объект — пропускаем", ticker)
        return False

    ok = True
    for field in _REQUIRED_NUMERIC:
        val = data.get(field)
        if not isinstance(val, (int, float)):
            logger.warning("fundamentals[%s]: поле '%s' отсутствует или не число", ticker, field)
            ok = False

    if not data.get("sector"):
        logger.warning("fundamentals[%s]: не задан 'sector' — будет 'unknown'", ticker)

    # Проверка свежести (не блокирующая)
    last = data.get("last_updated")
    if last:
        try:
            age = (today_msk() - datetime.strptime(last, "%Y-%m-%d").date()).days
            if age > FUNDAMENTALS_MAX_AGE_DAYS:
                logger.warning(
                    "fundamentals[%s]: данные устарели (%d дн., last_updated=%s)",
                    ticker, age, last,
                )
        except ValueError:
            logger.warning("fundamentals[%s]: некорректный формат last_updated=%s", ticker, last)
    else:
        logger.warning("fundamentals[%s]: нет поля last_updated", ticker)

    return ok


def load_fundamentals() -> dict[str, dict[str, Any]]:
    """
    Читает и валидирует fundamentals.json. Возвращает {} при ошибке чтения.
    Записи без обязательных числовых полей отбрасываются (тикер уйдёт на
    нейтральный фундаментальный скор в main), битые данные не искажают итог.
    """
    try:
        with open(FUNDAMENTALS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.error("Ошибка загрузки fundamentals.json: %s", exc)
        return {}

    if not isinstance(raw, dict):
        logger.error("fundamentals.json: ожидался объект, получено %s", type(raw).__name__)
        return {}

    valid = {t: d for t, d in raw.items() if _validate_entry(t, d)}
    dropped = len(raw) - len(valid)
    if dropped:
        logger.warning("fundamentals.json: отброшено %d невалидных записей из %d", dropped, len(raw))
    return valid


def get_sector_medians(fundamentals: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    """
    Вычисляет медианы P/E и ROE по секторам.
    Возвращает: {"banking": {"pe": 4.0, "roe": 20.0}, ...}
    """
    from collections import defaultdict
    import statistics

    sector_pe: dict[str, list[float]] = defaultdict(list)
    sector_roe: dict[str, list[float]] = defaultdict(list)

    for data in fundamentals.values():
        sector = data.get("sector", "unknown")
        pe = data.get("pe_ratio")
        roe = data.get("roe_pct")
        if pe and pe > 0:
            sector_pe[sector].append(float(pe))
        if roe is not None:
            sector_roe[sector].append(float(roe))

    medians: dict[str, dict[str, float]] = {}
    all_sectors = set(list(sector_pe.keys()) + list(sector_roe.keys()))
    for sector in all_sectors:
        medians[sector] = {
            "pe": statistics.median(sector_pe[sector]) if sector_pe[sector] else 8.0,
            "roe": statistics.median(sector_roe[sector]) if sector_roe[sector] else 15.0,
        }

    return medians


# ──────────────────────────────────────────────────────────────
# Скоринг 0–100
# ──────────────────────────────────────────────────────────────

def score_fundamental(data: dict[str, Any], sector_medians: dict[str, dict[str, float]]) -> float:
    """
    Взвешенный фундаментальный скор от 0 до 100.

    Веса:
      P/E           20
      Долг/EBITDA   20
      Дивдоходность 20
      ROE           20
      Рост выручки  10
      Маржа         10
    """
    score = 0.0
    sector = data.get("sector", "unknown")

    # 1. P/E (вес 20) — чем ниже относительно медианы по сектору, тем лучше
    sector_pe = sector_medians.get(sector, {}).get("pe", 8.0)
    pe_raw = data.get("pe_ratio")
    if pe_raw is None or float(pe_raw) <= 0:
        # Отрицательный P/E = убыток; нет данных = неизвестность → минимальный скор
        pe_score = 0.0
    else:
        pe_ratio = float(pe_raw)
        pe_score = max(0.0, 1.0 - (pe_ratio / sector_pe - 0.5))
    score += 20 * min(pe_score, 1.0)

    # 2. Долг/EBITDA (вес 20) — идеал < 1x, красный флаг > 3x
    debt = float(data.get("debt_ebitda") or 0.0)
    debt_score = max(0.0, 1.0 - debt / 3.0)
    score += 20 * min(debt_score, 1.0)

    # 3. Дивидендная доходность (вес 20) — данные добавляются снаружи
    div_yield = float(data.get("div_yield_pct") or 0.0)
    # Идеал: 10–15%. >20% — подозрительно (может быть разовая выплата)
    if div_yield > 20:
        div_score = 0.8  # подозрительно высокая
    else:
        div_score = min(div_yield / 12.0, 1.0)
    score += 20 * div_score

    # 4. ROE (вес 20) — идеал > 20%, < 5% плохо
    roe = float(data.get("roe_pct") or 0.0)
    roe_score = min(roe / 25.0, 1.0)
    score += 20 * max(roe_score, 0.0)

    # 5. Рост выручки (вес 10) — идеал 10–20% в год
    growth = float(data.get("revenue_growth_yoy_pct") or 0.0)
    growth_score = min(max(growth, 0.0) / 15.0, 1.0)
    score += 10 * growth_score

    # 6. Чистая маржа (вес 10) — идеал > 20%
    margin = float(data.get("net_margin_pct") or 0.0)
    margin_score = min(max(margin, 0.0) / 20.0, 1.0)
    score += 10 * margin_score

    return round(score, 1)
