"""Тесты финального скоринга и сигналов (scoring/final_score.py)."""
from scoring.final_score import (
    build_stock_result,
    compute_final_score,
    get_signal,
    get_signal_emoji,
    get_target_price,
    get_upside_pct,
)


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
    assert get_signal(70) == "BUY"
    assert get_signal(85) == "BUY"
    assert get_signal(69.9) == "HOLD"
    assert get_signal(50) == "HOLD"
    assert get_signal(31) == "HOLD"
    assert get_signal(30) == "SELL"
    assert get_signal(10) == "SELL"


def test_signal_emoji():
    assert get_signal_emoji("BUY") == "🟢"
    assert get_signal_emoji("SELL") == "🔴"
    assert get_signal_emoji("HOLD") == "🟡"
    assert get_signal_emoji("???") == "⚪"


def test_target_price():
    assert get_target_price(100, 50) == 100.0          # нейтрально
    assert get_target_price(100, 100) == 115.0         # +15%
    assert get_target_price(100, 0) == 85.0            # -15%
    assert get_target_price(0, 80) == 0.0              # нет цены


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
                "signal_emoji", "target_price", "upside_pct", "scores",
                "indicators", "fundamental", "sentiment"):
        assert key in res
    assert res["signal"] in ("BUY", "HOLD", "SELL")
    assert 0.0 <= res["final_score"] <= 100.0
