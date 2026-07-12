"""
Финальный скоринг и торговые сигналы.
Взвешивает три столпа анализа → балл 0-100 → BUY / HOLD / SELL.
"""
from __future__ import annotations

import math
import statistics

from config import SIGNAL_HYSTERESIS, SIGNAL_THRESHOLDS, WEIGHTS


# ──────────────────────────────────────────────────────────────
# Взвешенный скор
# ──────────────────────────────────────────────────────────────

def compute_final_score(
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    weights: dict[str, float] | None = None,
    valid: dict[str, bool] | None = None,
) -> float:
    """
    Финальный балл от 0 до 100.
    Веса по умолчанию: фундаментал 35%, технический 35%, сентимент 30%.

    valid — какие столпы основаны на реальных данных. Столп, ушедший в фолбэк
    (нейтральный 50), ИСКЛЮЧАЕТСЯ, а веса оставшихся перенормируются. Иначе
    «нейтральные 50» тянут итог к середине и душат сигнал (особенно SELL).
    Если данных нет совсем — возвращаем 50.
    """
    if weights is None:
        weights = WEIGHTS
    if valid is None:
        valid = {"fundamental": True, "technical": True, "sentiment": True}

    pillars = {
        "fundamental": fundamental_score,
        "technical": technical_score,
        "sentiment": sentiment_score,
    }
    active = {k: weights[k] for k in pillars if valid.get(k, True)}
    total_w = sum(active.values())
    if total_w <= 0:
        return 50.0

    score = sum(pillars[k] * w for k, w in active.items()) / total_w
    return round(max(0.0, min(100.0, score)), 1)


def assess_confidence(
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    valid: dict[str, bool] | None = None,
) -> str:
    """
    Достоверность сигнала: high / medium / low.
    Учитывает (а) полноту данных (сколько столпов реальны) и
    (б) согласие столпов (разброс баллов). Конфликтующие или
    неполные сигналы помечаются как менее надёжные.
    """
    if valid is None:
        valid = {"fundamental": True, "technical": True, "sentiment": True}
    pillars = {
        "fundamental": fundamental_score,
        "technical": technical_score,
        "sentiment": sentiment_score,
    }
    active_scores = [pillars[k] for k in pillars if valid.get(k, True)]
    valid_count = len(active_scores)

    if valid_count <= 1:
        return "low"
    dispersion = statistics.pstdev(active_scores)
    if valid_count < 3 or dispersion > 20:
        return "low" if dispersion > 25 else "medium"
    if dispersion > 12:
        return "medium"
    return "high"


# ──────────────────────────────────────────────────────────────
# Торговые сигналы
# ──────────────────────────────────────────────────────────────

def get_signal(score: float, prev_signal: str | None = None) -> str:
    """
    BUY / SELL / HOLD по порогам из config.SIGNAL_THRESHOLDS, с гистерезисом.

    Вход в BUY/SELL — по основному порогу. Выход — только когда скор отходит
    от порога дальше чем на SIGNAL_HYSTERESIS: вчерашний BUY при скоре 59
    остаётся BUY (порог 60, полоса до 56). Без prev_signal (первый прогон,
    нет истории) — чистые пороги. Убирает хлопанье сигнала при дрожании
    скора на 1–2 пункта у порога (недетерминизм сентимента).
    """
    buy, sell = SIGNAL_THRESHOLDS["BUY"], SIGNAL_THRESHOLDS["SELL"]
    if score >= buy:
        return "BUY"
    if score <= sell:
        return "SELL"
    if prev_signal == "BUY" and score >= buy - SIGNAL_HYSTERESIS:
        return "BUY"
    if prev_signal == "SELL" and score <= sell + SIGNAL_HYSTERESIS:
        return "SELL"
    return "HOLD"


def get_signal_emoji(signal: str) -> str:
    """Эмодзи для торгового сигнала."""
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(signal, "⚪")


# ──────────────────────────────────────────────────────────────
# Целевая цена
# ──────────────────────────────────────────────────────────────

def get_target_price(
    current_price: float,
    score: float,
    volatility_pct: float | None = None,
    horizon_days: int = 28,
) -> float:
    """
    Целевая цена на горизонте 4 недели, масштабированная волатильностью.

    Размер движения = убеждённость × ожидаемая волатильность за горизонт.
      • убеждённость = (score−50)/50 ∈ [−1, 1]
      • σ за горизонт = годовая σ × √(дни/365)
    При score=100 цель ≈ +1σ движения за месяц (для низковолатильной бумаги —
    скромная цель, для волатильной — крупнее). Старая фикс. ±15% игнорировала
    риск актива. Ограничиваем ±25% как защиту от выбросов.
    """
    if current_price <= 0:
        return 0.0

    vol_annual = volatility_pct if (volatility_pct and volatility_pct > 0) else 30.0
    horizon_vol = vol_annual / 100.0 * math.sqrt(horizon_days / 365.0)
    conviction = (score - 50.0) / 50.0
    upside = max(-0.25, min(0.25, conviction * horizon_vol))
    return round(current_price * (1 + upside), 2)


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
    valid: dict[str, bool] | None = None,
    prev_signal: str | None = None,
) -> dict:
    """
    Собирает полный результат по одной акции в единый словарь.
    Используется для отчёта и сохранения в JSON.

    valid — флаги «столп на реальных данных» (для перенормировки весов и
    оценки достоверности). None → все три считаются валидными.
    prev_signal — сигнал прошлого прогона (для гистерезиса, см. get_signal).
    """
    final = compute_final_score(
        fundamental_score, technical_score, sentiment_score, valid=valid
    )
    signal = get_signal(final, prev_signal=prev_signal)
    target = get_target_price(current_price, final, indicators.get("volatility_pct"))
    upside = get_upside_pct(current_price, target)
    confidence = assess_confidence(
        fundamental_score, technical_score, sentiment_score, valid=valid
    )

    return {
        "ticker": ticker,
        "company": company_name,
        "price": current_price,
        "final_score": final,
        "signal": signal,
        "signal_emoji": get_signal_emoji(signal),
        "confidence": confidence,
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
            "fallback": bool(indicators.get("fallback", False)),
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
