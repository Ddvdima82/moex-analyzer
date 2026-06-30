"""
Финальный скоринг и торговые сигналы.
Взвешивает три столпа анализа → балл 0-100 → BUY / HOLD / SELL.
"""
from __future__ import annotations

from config import SIGNAL_THRESHOLDS, WEIGHTS


# ──────────────────────────────────────────────────────────────
# Взвешенный скор
# ──────────────────────────────────────────────────────────────

def compute_final_score(
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    weights: dict[str, float] | None = None,
) -> float:
    """
    Финальный балл от 0 до 100.
    Веса по умолчанию: фундаментал 35%, технический 35%, сентимент 30%.
    """
    if weights is None:
        weights = WEIGHTS

    score = (
        weights["fundamental"] * fundamental_score
        + weights["technical"] * technical_score
        + weights["sentiment"] * sentiment_score
    )
    # Гарантируем диапазон [0, 100]
    return round(max(0.0, min(100.0, score)), 1)


# ──────────────────────────────────────────────────────────────
# Торговые сигналы
# ──────────────────────────────────────────────────────────────

def get_signal(score: float) -> str:
    """BUY (≥70) / SELL (≤30) / HOLD (всё остальное)."""
    if score >= SIGNAL_THRESHOLDS["BUY"]:
        return "BUY"
    elif score <= SIGNAL_THRESHOLDS["SELL"]:
        return "SELL"
    else:
        return "HOLD"


def get_signal_emoji(signal: str) -> str:
    """Эмодзи для торгового сигнала."""
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(signal, "⚪")


# ──────────────────────────────────────────────────────────────
# Целевая цена
# ──────────────────────────────────────────────────────────────

def get_target_price(current_price: float, score: float) -> float:
    """
    Целевая цена на горизонте 4 недели.
    При score=100 → +15%, при score=0 → -15%, при score=50 → без изменений.
    """
    if current_price <= 0:
        return 0.0

    # Линейная интерполяция: score 0..100 → upside -15%..+15%
    upside = (score - 50) / 100 * 0.30
    target = current_price * (1 + upside)
    return round(target, 2)


def get_upside_pct(current_price: float, target_price: float) -> float:
    """Потенциал роста/падения в процентах."""
    if current_price <= 0:
        return 0.0
    return round((target_price / current_price - 1) * 100, 1)


# ──────────────────────────────────────────────────────────────
# Полная сборка результата по одной акции
# ──────────────────────────────────────────────────────────────

def build_stock_result(
    ticker: str,
    company_name: str,
    current_price: float,
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    indicators: dict,
    fundamental_data: dict,
    sentiment_data: dict,
) -> dict:
    """
    Собирает полный результат по одной акции в единый словарь.
    Используется для отчёта и сохранения в JSON.
    """
    final = compute_final_score(fundamental_score, technical_score, sentiment_score)
    signal = get_signal(final)
    target = get_target_price(current_price, final)
    upside = get_upside_pct(current_price, target)

    return {
        "ticker": ticker,
        "company": company_name,
        "price": current_price,
        "final_score": final,
        "signal": signal,
        "signal_emoji": get_signal_emoji(signal),
        "target_price": target,
        "upside_pct": upside,
        "scores": {
            "fundamental": fundamental_score,
            "technical": technical_score,
            "sentiment": sentiment_score,
        },
        "indicators": {
            "rsi": indicators.get("rsi"),
            "macd_histogram": indicators.get("macd_histogram"),
            "above_sma200": indicators.get("above_sma200"),
            "above_sma50": indicators.get("above_sma50"),
            "volume_trend_pct": indicators.get("volume_trend_pct"),
            "position_52w": indicators.get("position_52w"),
            "volatility_pct": indicators.get("volatility_pct"),
        },
        "fundamental": {
            "pe_ratio": fundamental_data.get("pe_ratio"),
            "debt_ebitda": fundamental_data.get("debt_ebitda"),
            "roe_pct": fundamental_data.get("roe_pct"),
            "div_yield_pct": fundamental_data.get("div_yield_pct"),
            "sector": fundamental_data.get("sector"),
            "ex_date": fundamental_data.get("ex_date"),
            "next_div_amount": fundamental_data.get("next_div_amount"),
        },
        "sentiment": {
            "overall": sentiment_data.get("overall_sentiment"),
            "score": sentiment_data.get("sentiment_score"),
            "key_event": sentiment_data.get("key_event"),
            "positive_count": sentiment_data.get("positive_count", 0),
            "negative_count": sentiment_data.get("negative_count", 0),
        },
    }
