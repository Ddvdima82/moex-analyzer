"""Тесты технических индикаторов (analysis/technical.py)."""
import pandas as pd

from analysis.technical import (
    _empty_indicators,
    adx_trend_strength,
    compute_adx,
    compute_indicators,
    compute_macd,
    compute_market_regime,
    compute_rsi,
    compute_sma,
    score_technical,
    trim_price_gap,
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


# ── ADX/DMI ───────────────────────────────────────────────────────────────

def _trend_df(direction: int, n: int = 60, step: float = 2.0, start: float = 100.0) -> pd.DataFrame:
    """Синтетический чистый тренд: HIGH/LOW симметрично вокруг CLOSE."""
    closes = [start + direction * step * i for i in range(n)]
    return pd.DataFrame({
        "HIGH": [c + 1.0 for c in closes],
        "LOW": [c - 1.0 for c in closes],
        "CLOSE": closes,
    })


def _choppy_df(n: int = 60) -> pd.DataFrame:
    """Пилообразный ряд без устойчивого направления — слабый ADX."""
    closes = [100.0 + (i % 2) for i in range(n)]
    return pd.DataFrame({
        "HIGH": [c + 0.5 for c in closes],
        "LOW": [c - 0.5 for c in closes],
        "CLOSE": closes,
    })


def test_compute_adx_strong_uptrend():
    out = compute_adx(_trend_df(1))
    assert out is not None
    assert out["plus_di"] > out["minus_di"]
    assert out["adx"] > 40


def test_compute_adx_strong_downtrend():
    out = compute_adx(_trend_df(-1))
    assert out is not None
    assert out["minus_di"] > out["plus_di"]
    assert out["adx"] > 40


def test_compute_adx_choppy_market_is_weak():
    out = compute_adx(_choppy_df())
    assert out is not None
    assert out["adx"] < 20


def test_compute_adx_insufficient_data():
    assert compute_adx(_trend_df(1, n=10)) is None


def test_compute_adx_missing_columns():
    assert compute_adx(pd.DataFrame({"CLOSE": [100.0] * 60})) is None


def test_adx_trend_strength_thresholds():
    assert adx_trend_strength(None) == "unknown"
    assert adx_trend_strength(10.0) == "weak"
    assert adx_trend_strength(19.9) == "weak"
    assert adx_trend_strength(20.0) == "moderate"
    assert adx_trend_strength(39.9) == "moderate"
    assert adx_trend_strength(40.0) == "strong"
    assert adx_trend_strength(70.0) == "strong"


# ── ADX-демпфер контрарной 52w-компоненты (score_technical) ──────────────────

def test_score_technical_adx_dampens_strong_confirmed_downtrend():
    base = _ind(rsi=20, position_52w=0.05)
    damped = _ind(rsi=20, position_52w=0.05, adx=50.0, plus_di=10.0, minus_di=40.0)
    assert score_technical(damped) < score_technical(base)


def test_score_technical_adx_weak_trend_no_dampen():
    """Слабый тренд (боковик) — контрарная ставка не глушится, даже если −DI>+DI."""
    base = _ind(rsi=20, position_52w=0.05)
    weak = _ind(rsi=20, position_52w=0.05, adx=10.0, plus_di=15.0, minus_di=20.0)
    assert score_technical(weak) == score_technical(base)


def test_score_technical_adx_no_dampen_when_uptrend_confirmed():
    """+DI>−DI (бычье направление) — демпфер не применяется даже при высоком ADX."""
    base = _ind(rsi=20, position_52w=0.05)
    bullish = _ind(rsi=20, position_52w=0.05, adx=50.0, plus_di=40.0, minus_di=10.0)
    assert score_technical(bullish) == score_technical(base)


def test_score_technical_adx_missing_no_dampen():
    base = _ind(rsi=20, position_52w=0.05)
    no_di = _ind(rsi=20, position_52w=0.05, adx=50.0)  # plus_di/minus_di отсутствуют
    assert score_technical(no_di) == score_technical(base)


# ── Режим рынка по индексу ───────────────────────────────────────────────────

def test_market_regime_bull():
    df = pd.DataFrame({"CLOSE": [1000 + i for i in range(250)]})
    out = compute_market_regime(df)
    assert out["regime"] == "bull"
    assert out["index_close"] is not None and out["index_sma200"] is not None


def test_market_regime_bear():
    df = pd.DataFrame({"CLOSE": [3000 - 5 * i for i in range(250)]})
    assert compute_market_regime(df)["regime"] == "bear"


def test_market_regime_bear_with_strong_trend_strength():
    df = _trend_df(-1, n=250, step=5.0, start=3000.0)
    out = compute_market_regime(df)
    assert out["regime"] == "bear"
    assert out["trend_strength"] == "strong"
    assert out["adx"] is not None and out["adx"] > 40


def test_market_regime_no_adx_data_unknown_strength():
    """CLOSE без HIGH/LOW (нет данных для ADX) — сила тренда unknown, но
    режим по SMA200 всё равно считается."""
    df = pd.DataFrame({"CLOSE": [3000 - 5 * i for i in range(250)]})
    out = compute_market_regime(df)
    assert out["regime"] == "bear"
    assert out["trend_strength"] == "unknown"
    assert out["adx"] is None


def test_market_regime_insufficient_data_neutral():
    df = pd.DataFrame({"CLOSE": [100.0] * 50})
    assert compute_market_regime(df)["regime"] == "neutral"
    assert compute_market_regime(pd.DataFrame())["regime"] == "neutral"


# ── Детекция ценового разрыва (сплит/корпособытие) ───────────────────────────

def test_trim_price_gap_reverse_split():
    """Реверс-сплит (стиль VTBR 5000:1): остаётся только пост-сплитовое окно."""
    closes = [0.02] * 60 + [100.0] * 40
    df = pd.DataFrame({"CLOSE": closes, "VOLUME": [1000] * 100})
    out, gap = trim_price_gap(df)
    assert gap is True
    assert len(out) == 40
    assert (out["CLOSE"] == 100.0).all()


def test_trim_price_gap_normal_series_untouched():
    df = pd.DataFrame({"CLOSE": [100 + i for i in range(50)]})
    out, gap = trim_price_gap(df)
    assert gap is False
    assert len(out) == 50


def test_trim_price_gap_dividend_gap_untouched():
    """Дивидендный гэп −15% ниже порога — история не режется."""
    closes = [100.0] * 30 + [85.0] * 30
    df = pd.DataFrame({"CLOSE": closes})
    out, gap = trim_price_gap(df)
    assert gap is False
    assert len(out) == 60


def test_trim_price_gap_multiple_gaps_uses_last():
    """Два разрыва → окно от последнего."""
    closes = [100.0] * 20 + [10.0] * 20 + [200.0] * 10
    df = pd.DataFrame({"CLOSE": closes})
    out, gap = trim_price_gap(df)
    assert gap is True
    assert len(out) == 10
    assert (out["CLOSE"] == 200.0).all()


def test_trim_price_gap_zero_price_bar_not_a_gap():
    """Единичный битый бар CLOSE=0 (дефект данных) — не разрыв, история цела."""
    closes = [100.0] * 30 + [0.0] + [100.0] * 29
    df = pd.DataFrame({"CLOSE": closes})
    out, gap = trim_price_gap(df)
    assert gap is False
    assert len(out) == 60


def test_trim_price_gap_empty_and_short():
    out, gap = trim_price_gap(pd.DataFrame())
    assert gap is False and out.empty
    one = pd.DataFrame({"CLOSE": [100.0]})
    out, gap = trim_price_gap(one)
    assert gap is False and len(out) == 1


# ── Непрерывность RSI на границах 30/70 (регресс на исправление #4) ──────────

def _ind(**kwargs):
    base = _empty_indicators()
    base.update(kwargs)
    return base


def test_rsi_no_jump_at_30():
    """Разница скора при RSI=29.9 и RSI=30.1 менее 1 балла."""
    assert abs(score_technical(_ind(rsi=29.9)) - score_technical(_ind(rsi=30.1))) < 1.0


def test_rsi_no_jump_at_70():
    """Разница скора при RSI=69.9 и RSI=70.1 менее 1 балла."""
    assert abs(score_technical(_ind(rsi=69.9)) - score_technical(_ind(rsi=70.1))) < 1.0


def test_rsi_midpoint_between_extremes():
    s_low = score_technical(_ind(rsi=30.0))
    s_mid = score_technical(_ind(rsi=50.0))
    s_high = score_technical(_ind(rsi=70.0))
    assert s_high < s_mid < s_low


# ── Непрерывность 52w при RSI=50 (регресс на исправление #5) ─────────────────

def test_52w_no_jump_at_rsi50():
    """Разница скора при RSI=49.9 и RSI=50.1 с position=0.5 менее 0.5 балла."""
    s_below = score_technical(_ind(rsi=49.9, position_52w=0.5))
    s_above = score_technical(_ind(rsi=50.1, position_52w=0.5))
    assert abs(s_below - s_above) < 0.5
