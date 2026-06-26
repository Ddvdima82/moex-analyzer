"""Тесты технических индикаторов (analysis/technical.py)."""
import pandas as pd

from analysis.technical import (
    _empty_indicators,
    compute_indicators,
    compute_macd,
    compute_rsi,
    compute_sma,
    score_technical,
)


def test_rsi_insufficient_data_neutral():
    assert compute_rsi(pd.Series([1, 2, 3])) == 50.0


def test_rsi_strictly_increasing_is_max():
    # Только рост → нет потерь → RSI = 100
    assert compute_rsi(pd.Series(range(1, 40))) == 100.0


def test_rsi_strictly_decreasing_is_min():
    assert compute_rsi(pd.Series(range(40, 1, -1))) == 0.0


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert compute_sma(s, 5) == 8.0          # среднее последних 5
    assert compute_sma(s, 20) is None        # период больше длины


def test_macd_insufficient_data_zeros():
    out = compute_macd(pd.Series([1, 2, 3]))
    assert out == {"macd": 0.0, "signal": 0.0, "histogram": 0.0}


def test_compute_indicators_empty_df():
    assert compute_indicators(pd.DataFrame()) == _empty_indicators()


def test_score_technical_bounds():
    # Любой набор индикаторов → балл в [0, 100]
    assert 0.0 <= score_technical(_empty_indicators()) <= 100.0
    bullish = {
        "rsi": 45, "macd_histogram": 2.0, "above_sma200": True,
        "above_sma50": True, "above_sma20": True, "volume_trend_pct": 30,
        "position_52w": 0.2,
    }
    bearish = {
        "rsi": 80, "macd_histogram": -2.0, "above_sma200": False,
        "above_sma50": False, "above_sma20": False, "volume_trend_pct": -30,
        "position_52w": 0.95,
    }
    sb, ss = score_technical(bullish), score_technical(bearish)
    assert 0.0 <= ss <= sb <= 100.0          # бычий набор не ниже медвежьего


def test_compute_indicators_real_series():
    closes = [100 + i for i in range(250)]
    df = pd.DataFrame({"CLOSE": closes, "VOLUME": [1000] * 250})
    ind = compute_indicators(df)
    # При 250 точках SMA200 считается (раньше была всегда None из-за пагинации)
    assert ind["sma200"] is not None
    assert ind["above_sma200"] is True       # растущий ряд выше средней
