"""Тесты финального скоринга и сигналов (scoring/final_score.py)."""
from scoring.final_score import (
    assess_confidence,
    build_stock_result,
    compute_final_score,
    get_signal,
    get_signal_emoji,
    get_target_price,
    get_upside_pct,
)


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
