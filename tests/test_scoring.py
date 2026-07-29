"""Тесты финального скоринга и сигналов (scoring/final_score.py)."""
from datetime import timedelta

from config import (
    BEAR_BUY_EXTRA,
    BEAR_TREND_STRONG_MULT,
    BEAR_TREND_WEAK_MULT,
    SIGNAL_THRESHOLDS,
    today_msk,
)
from scoring.final_score import (
    adjust_target_for_dividend,
    assess_confidence,
    build_stock_result,
    compute_final_score,
    get_signal,
    get_signal_emoji,
    get_target_price,
    get_upside_pct,
)


# ── Режимный фильтр рынка ────────────────────────────────────────────────────

def test_get_signal_bear_regime_raises_buy_bar():
    buy = SIGNAL_THRESHOLDS["BUY"]
    edge_score = buy + BEAR_BUY_EXTRA / 2          # 62.5 при 60/+5
    assert get_signal(edge_score) == "BUY"          # обычный режим — BUY
    assert get_signal(edge_score, regime="bear") == "HOLD"  # bear — порог выше
    assert get_signal(buy + BEAR_BUY_EXTRA, regime="bear") == "BUY"


def test_get_signal_bear_does_not_touch_sell():
    sell = SIGNAL_THRESHOLDS["SELL"]
    assert get_signal(sell, regime="bear") == "SELL"
    assert get_signal(sell, regime="bull") == "SELL"


def test_get_signal_bull_neutral_unchanged():
    buy = SIGNAL_THRESHOLDS["BUY"]
    assert get_signal(buy, regime="bull") == "BUY"
    assert get_signal(buy, regime="neutral") == "BUY"
    assert get_signal(buy, regime=None) == "BUY"


def test_get_signal_bear_graduated_by_trend_strength():
    buy, extra = SIGNAL_THRESHOLDS["BUY"], BEAR_BUY_EXTRA
    weak_bar = buy + extra * BEAR_TREND_WEAK_MULT
    moderate_bar = buy + extra           # множитель 1.0 по умолчанию
    strong_bar = buy + extra * BEAR_TREND_STRONG_MULT

    # Слабый тренд (боковик) — надбавка меньше базовой
    assert get_signal(weak_bar - 0.01, regime="bear", trend_strength="weak") == "HOLD"
    assert get_signal(weak_bar, regime="bear", trend_strength="weak") == "BUY"

    # Умеренный тренд — как раньше (flat BEAR_BUY_EXTRA)
    assert get_signal(moderate_bar, regime="bear", trend_strength="moderate") == "BUY"

    # Сильный подтверждённый тренд — надбавка удвоена
    assert get_signal(strong_bar - 0.01, regime="bear", trend_strength="strong") == "HOLD"
    assert get_signal(strong_bar, regime="bear", trend_strength="strong") == "BUY"


def test_get_signal_bear_unknown_trend_strength_uses_default_extra():
    buy = SIGNAL_THRESHOLDS["BUY"]
    assert get_signal(buy + BEAR_BUY_EXTRA, regime="bear", trend_strength=None) == "BUY"
    assert get_signal(buy + BEAR_BUY_EXTRA, regime="bear", trend_strength="unknown") == "BUY"


def test_get_signal_bear_hysteresis_shifts_with_threshold():
    # bear-порог 65, гистерезис 4 → прошлый BUY держится при 61+
    buy = SIGNAL_THRESHOLDS["BUY"] + BEAR_BUY_EXTRA
    assert get_signal(buy - 2, prev_signal="BUY", regime="bear") == "BUY"
    assert get_signal(buy - 10, prev_signal="BUY", regime="bear") == "HOLD"


# ── Поправка цели на дивидендный гэп ─────────────────────────────────────────

def test_adjust_target_ex_date_in_horizon():
    ex = (today_msk() + timedelta(days=5)).strftime("%Y-%m-%d")
    assert adjust_target_for_dividend(300.0, ex, 30.0) == 270.0


def test_adjust_target_ex_date_beyond_horizon():
    ex = (today_msk() + timedelta(days=60)).strftime("%Y-%m-%d")
    assert adjust_target_for_dividend(300.0, ex, 30.0) == 300.0


def test_adjust_target_ex_date_past():
    ex = (today_msk() - timedelta(days=3)).strftime("%Y-%m-%d")
    assert adjust_target_for_dividend(300.0, ex, 30.0) == 300.0


def test_adjust_target_missing_or_broken_data():
    assert adjust_target_for_dividend(300.0, None, 30.0) == 300.0
    assert adjust_target_for_dividend(300.0, "2026-08-01", None) == 300.0
    assert adjust_target_for_dividend(300.0, "август", 30.0) == 300.0
    # дивиденд больше цели → пол 0.01, не отрицательная цена
    ex = (today_msk() + timedelta(days=2)).strftime("%Y-%m-%d")
    assert adjust_target_for_dividend(5.0, ex, 30.0) == 0.01


def test_assess_confidence():
    # Согласованные столпы, все валидны → high
    assert assess_confidence(70, 72, 68) == "high"
    # Сильный разнобой столпов → не high
    assert assess_confidence(90, 30, 60) in ("medium", "low")
    # Один валидный столп → low (нет подтверждения)
    assert assess_confidence(80, 50, 50, valid={"fundamental": True,
                                                "technical": False, "sentiment": False}) == "low"


def test_compute_final_score_bounds():
    assert compute_final_score(100, 100, 100) == 100.0
    assert compute_final_score(0, 0, 0) == 0.0
    # Клампинг: значения вне [0,100] не вылезают за диапазон
    assert 0.0 <= compute_final_score(-50, 200, 50) <= 100.0


def test_compute_final_score_weights():
    # Дефолтные веса 0.35/0.35/0.30 → одинаковые входы дают тот же балл
    assert compute_final_score(60, 60, 60) == 60.0
    # Кастомные веса
    score = compute_final_score(100, 0, 0, weights={"fundamental": 1.0, "technical": 0.0, "sentiment": 0.0})
    assert score == 100.0


def test_get_signal_thresholds():
    assert get_signal(60) == "BUY"
    assert get_signal(85) == "BUY"
    assert get_signal(59.9) == "HOLD"
    assert get_signal(50) == "HOLD"
    assert get_signal(36) == "HOLD"
    assert get_signal(35) == "SELL"
    assert get_signal(10) == "SELL"


def test_get_signal_hysteresis():
    # Вчерашний BUY при скоре в полосе [56, 60) удерживается
    assert get_signal(59, prev_signal="BUY") == "BUY"
    assert get_signal(56, prev_signal="BUY") == "BUY"
    # Вышел из полосы → HOLD
    assert get_signal(55.9, prev_signal="BUY") == "HOLD"
    # Без прошлого BUY полоса не работает — вход только по основному порогу
    assert get_signal(59, prev_signal="HOLD") == "HOLD"
    assert get_signal(59) == "HOLD"
    # Симметрично для SELL (порог 35, полоса до 39)
    assert get_signal(38, prev_signal="SELL") == "SELL"
    assert get_signal(39.1, prev_signal="SELL") == "HOLD"
    # Основные пороги всегда главнее прошлого сигнала
    assert get_signal(60, prev_signal="SELL") == "BUY"
    assert get_signal(35, prev_signal="BUY") == "SELL"


def test_signal_emoji():
    assert get_signal_emoji("BUY") == "🟢"
    assert get_signal_emoji("SELL") == "🔴"
    assert get_signal_emoji("HOLD") == "🟡"
    assert get_signal_emoji("???") == "⚪"


def test_target_price():
    # score=50 → нейтрально, цель = текущая цена при любой волатильности
    assert get_target_price(100, 50, volatility_pct=40.0) == 100.0
    assert get_target_price(0, 80) == 0.0                       # нет цены
    # Размер цели масштабируется волатильностью: выше σ → крупнее движение
    t_low = get_target_price(100, 100, volatility_pct=20.0)
    t_high = get_target_price(100, 100, volatility_pct=60.0)
    assert t_low > 100.0 and t_high > t_low                     # обе вверх, σ↑ → дальше
    # Симметрия: score 0 зеркалит score 100 относительно цены
    up = get_target_price(100, 100, volatility_pct=40.0) - 100.0
    down = 100.0 - get_target_price(100, 0, volatility_pct=40.0)
    assert abs(up - down) < 1e-6
    # Кап ±25% от выбросов волатильности
    assert get_target_price(100, 100, volatility_pct=500.0) == 125.0


def test_compute_final_score_renormalizes_on_fallback():
    # Фолбэк-столп исключается, веса перенормируются. Только fundamental валиден:
    score = compute_final_score(80, 50, 50, valid={"fundamental": True,
                                                    "technical": False, "sentiment": False})
    assert score == 80.0
    # Без сентимента: 0.35/0.35 → 0.5/0.5
    score2 = compute_final_score(80, 40, 50, valid={"fundamental": True,
                                                     "technical": True, "sentiment": False})
    assert score2 == 60.0
    # Все невалидны → нейтраль
    assert compute_final_score(10, 10, 10, valid={"fundamental": False,
                                                  "technical": False, "sentiment": False}) == 50.0


def test_upside_pct():
    assert get_upside_pct(100, 115) == 15.0
    assert get_upside_pct(100, 85) == -15.0
    assert get_upside_pct(0, 50) == 0.0


def test_build_stock_result_shape():
    res = build_stock_result(
        ticker="SBER",
        company_name="Сбербанк",
        current_price=300.0,
        fundamental_score=80,
        technical_score=70,
        sentiment_score=60,
        indicators={"rsi": 55, "macd_histogram": 1.0},
        fundamental_data={"pe_ratio": 4.0, "sector": "banking"},
        sentiment_data={"overall_sentiment": "positive", "sentiment_score": 60},
    )
    # Контракт: ключи, потребляемые отчётами
    for key in ("ticker", "company", "price", "final_score", "signal",
                "signal_emoji", "confidence", "target_price", "upside_pct", "scores",
                "indicators", "fundamental", "sentiment"):
        assert key in res
    assert res["signal"] in ("BUY", "HOLD", "SELL")
    assert 0.0 <= res["final_score"] <= 100.0


# ── upside_pct не штрафуется дважды за один дивиденд ─────────────────────────

def test_build_stock_result_upside_excludes_dividend_double_count():
    """
    Держатель через отсечку получает и цену после гэпа, и сам дивиденд
    деньгами. target_price (отображаемый) — после вычета, но upside_pct
    должен отражать полную доходность (как если бы дивиденд не вычитался).
    """
    ex_date = (today_msk() + timedelta(days=3)).strftime("%Y-%m-%d")

    with_div = build_stock_result(
        ticker="SBER", company_name="Сбербанк", current_price=300.0,
        fundamental_score=90, technical_score=90, sentiment_score=90,
        indicators={"rsi": 55, "volatility_pct": 30.0},
        fundamental_data={"pe_ratio": 4.0, "sector": "banking",
                          "ex_date": ex_date, "next_div_amount": 20.0},
        sentiment_data={"overall_sentiment": "positive", "sentiment_score": 90},
    )
    without_div = build_stock_result(
        ticker="SBER", company_name="Сбербанк", current_price=300.0,
        fundamental_score=90, technical_score=90, sentiment_score=90,
        indicators={"rsi": 55, "volatility_pct": 30.0},
        fundamental_data={"pe_ratio": 4.0, "sector": "banking"},
        sentiment_data={"overall_sentiment": "positive", "sentiment_score": 90},
    )
    # Отображаемая цель — ниже (дивиденд вычтен, честная будущая котировка)
    assert with_div["target_price"] < without_div["target_price"]
    # Но полная доходность (upside_pct) — одинаковая: дивиденд не штрафует дважды
    assert with_div["upside_pct"] == without_div["upside_pct"]
