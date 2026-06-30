"""
Технический анализ акций.
Реализован на чистом pandas/numpy без внешних TA-библиотек.
"""
from __future__ import annotations

import logging

import math

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Индикаторы
# ──────────────────────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = 14) -> float:
    """
    RSI (Relative Strength Index) от 0 до 100.
    < 30 — перепродан, > 70 — перекуплен.
    """
    if len(close) < period + 1:
        return 50.0  # нейтральное значение при нехватке данных

    delta = close.diff().dropna()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # Начальные средние (простые за `period` баров)
    avg_gain = gain.iloc[:period].mean()
    avg_loss = loss.iloc[:period].mean()

    # Сглаживание Wilder'а
    for i in range(period, len(gain)):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float]:
    """
    MACD (Moving Average Convergence Divergence).
    Возвращает {'macd': float, 'signal': float, 'histogram': float}.
    """
    if len(close) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
    }


def compute_sma(close: pd.Series, period: int) -> float | None:
    """Простая скользящая средняя за `period` дней."""
    if len(close) < period:
        return None
    return round(float(close.rolling(period).mean().iloc[-1]), 4)


def compute_volume_trend(volume: pd.Series) -> float:
    """
    Изменение объёма: (средний за 10 дней / средний за 30 дней − 1) × 100%.
    Положительное → рост объёма.
    """
    if len(volume) < 30:
        return 0.0

    avg_10 = float(volume.iloc[-10:].mean())
    avg_30 = float(volume.iloc[-30:].mean())

    if avg_30 == 0:
        return 0.0

    return round((avg_10 / avg_30 - 1) * 100, 2)


def compute_52w_position(close: pd.Series) -> float:
    """
    Позиция текущей цены в 52-недельном диапазоне.
    0.0 = на минимуме, 1.0 = на максимуме.
    """
    if len(close) < 2:
        return 0.5

    window = close.iloc[-252:] if len(close) >= 252 else close
    low_52 = float(window.min())
    high_52 = float(window.max())

    if high_52 == low_52:
        return 0.5

    current = float(close.iloc[-1])
    return round((current - low_52) / (high_52 - low_52), 4)


def compute_volatility(close: pd.Series, period: int = 20) -> float:
    """Аннуализированная историческая волатильность в %."""
    if len(close) < period + 1:
        return 0.0

    log_returns = np.log(close / close.shift(1)).dropna()
    daily_vol = float(log_returns.iloc[-period:].std())
    annualized = daily_vol * np.sqrt(252) * 100

    return round(annualized, 2)


# ──────────────────────────────────────────────────────────────
# Расчёт всех индикаторов для одной акции
# ──────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict[str, float | bool]:
    """
    Принимает DataFrame с колонками CLOSE, VOLUME.
    Возвращает словарь всех технических индикаторов.
    """
    if df.empty or "CLOSE" not in df.columns:
        logger.warning("Пустой DataFrame в compute_indicators")
        return _empty_indicators()

    close: pd.Series = df["CLOSE"].astype(float)
    volume: pd.Series = df["VOLUME"].astype(float) if "VOLUME" in df.columns else pd.Series(dtype=float)

    current_price = float(close.iloc[-1])
    sma20 = compute_sma(close, 20)
    sma50 = compute_sma(close, 50)
    sma200 = compute_sma(close, 200)

    return {
        "rsi": compute_rsi(close),
        "macd_histogram": compute_macd(close)["histogram"],
        "above_sma20": bool(sma20 is not None and current_price > sma20),
        "above_sma50": bool(sma50 is not None and current_price > sma50),
        "above_sma200": bool(sma200 is not None and current_price > sma200),
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "volume_trend_pct": compute_volume_trend(volume) if not volume.empty else 0.0,
        "position_52w": compute_52w_position(close),
        "volatility_pct": compute_volatility(close),
        "current_price": current_price,
    }


def _empty_indicators() -> dict[str, float | bool]:
    return {
        "rsi": 50.0,
        "macd_histogram": 0.0,
        "above_sma20": False,
        "above_sma50": False,
        "above_sma200": False,
        "sma20": None,
        "sma50": None,
        "sma200": None,
        "volume_trend_pct": 0.0,
        "position_52w": 0.5,
        "volatility_pct": 0.0,
        "current_price": 0.0,
    }


# ──────────────────────────────────────────────────────────────
# Скоринг 0–100
# ──────────────────────────────────────────────────────────────

def score_technical(indicators: dict) -> float:
    """
    Взвешенный технический скор от 0 до 100.
    Веса: RSI 25 | SMA-тренд 25 | MACD 20 | Объём 15 | 52w-позиция 15
    """
    score = 0.0

    # 1. RSI (вес 25) — непрерывная линейная функция без разрывов на границах
    # RSI ≤ 30 (перепродан) → 1.0; RSI ≥ 70 (перекуплен) → 0.0; между — линейно
    rsi = indicators.get("rsi", 50.0)
    if rsi <= 30:
        rsi_score = 1.0
    elif rsi >= 70:
        rsi_score = 0.0
    else:
        rsi_score = (70.0 - rsi) / 40.0
    score += 25 * rsi_score

    # 2. SMA-тренд (вес 25)
    sma_score = 0.0
    if indicators.get("above_sma200"):
        sma_score += 0.5
    if indicators.get("above_sma50"):
        sma_score += 0.3
    if indicators.get("above_sma20"):
        sma_score += 0.2
    score += 25 * min(sma_score, 1.0)

    # 3. MACD гистограмма (вес 20) — градуальная нормализация через atan.
    # Нулевой MACD → 0.5; положительный → к 1.0; отрицательный → к 0.0.
    macd_hist = indicators.get("macd_histogram", 0.0)
    macd_score = math.atan(macd_hist / 0.5) / math.pi + 0.5
    score += 20 * macd_score

    # 4. Объём (вес 15)
    vol_trend = indicators.get("volume_trend_pct", 0.0)
    vol_norm = (min(max(vol_trend / 30, -1), 1) + 1) / 2  # нормализация 0-1
    score += 15 * vol_norm

    # 5. Позиция в 52w (вес 15) — плавное смешение контрарного и моментум-режима
    # При низком RSI ценим близость к лоу (отскок), при высоком — к хаю (моментум).
    # rsi_weight плавно переходит от 0 (RSI=0) к 1 (RSI=100) → нет разрыва на RSI=50.
    position = indicators.get("position_52w", 0.5)
    rsi_weight = rsi / 100.0
    contrarian = 1.0 - position        # близость к лоу хороша при слабом RSI
    momentum = position * 0.5          # близость к хаю хороша при сильном RSI (сдержанно)
    range_score = contrarian * (1.0 - rsi_weight) + momentum * rsi_weight
    score += 15 * min(max(range_score, 0.0), 1.0)

    return round(score, 1)
