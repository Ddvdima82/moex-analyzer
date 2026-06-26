"""
Модуль работы с MOEX ISS API.
Документация: https://iss.moex.com/iss/reference/

Все функции имеют retry-логику (3 попытки × 5 сек).
При ошибке возвращают None / пустую структуру и логируют — не падают.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

from config import (
    MOEX_BASE_URL,
    MOEX_BOARD,
    REQUEST_TIMEOUT,
    RETRY_COUNT,
    RETRY_DELAY,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Вспомогательная функция: HTTP-запрос с retry
# ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> dict | None:
    """GET-запрос к MOEX ISS с повторными попытками."""
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("Попытка %d/%d — ошибка запроса %s: %s", attempt, RETRY_COUNT, url, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    logger.error("Все %d попытки исчерпаны для %s", RETRY_COUNT, url)
    return None


# ──────────────────────────────────────────────────────────────
# Текущие котировки
# ──────────────────────────────────────────────────────────────

def get_current_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Получает текущие рыночные данные по списку тикеров.

    Возвращает:
        {
          "SBER": {
            "price": 312.5,
            "change_pct": -0.48,
            "volume": 45_000_000,
            "open": 314.0,
            "high": 315.2,
            "low": 311.8,
          },
          ...
        }
    """
    url = f"{MOEX_BASE_URL}/engines/stock/markets/shares/boards/{MOEX_BOARD}/securities.json"
    params = {
        "securities": ",".join(tickers),
        "iss.meta": "off",
    }
    data = _get(url, params)
    result: dict[str, dict[str, Any]] = {}

    if not data:
        return result

    try:
        # Раздел marketdata содержит текущие цены
        md_section = data.get("marketdata", {})
        columns: list[str] = md_section.get("columns", [])
        rows: list[list] = md_section.get("data", [])

        # Нужные поля и их индексы
        col_idx = {col: i for i, col in enumerate(columns)}

        for row in rows:
            ticker = row[col_idx.get("SECID", 0)]
            if ticker not in tickers:
                continue

            def _val(col: str) -> Any:
                idx = col_idx.get(col)
                return row[idx] if idx is not None else None

            last = _val("LAST") or _val("MARKETPRICE") or _val("WAPRICE")
            if last is None:
                continue

            open_ = _val("OPEN") or last
            high = _val("HIGH") or last
            low = _val("LOW") or last
            prev_price = _val("PREVPRICE") or last
            change_pct = ((last - prev_price) / prev_price * 100) if prev_price else 0.0

            result[ticker] = {
                "price": float(last),
                "change_pct": round(float(change_pct), 2),
                "volume": int(_val("VOLTODAY") or 0),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
            }

        logger.info("Получены котировки для %d из %d тикеров", len(result), len(tickers))
    except Exception as exc:
        logger.error("Ошибка разбора котировок: %s", exc, exc_info=True)

    return result


# ──────────────────────────────────────────────────────────────
# Исторические данные
# ──────────────────────────────────────────────────────────────

def get_history(ticker: str, days: int = 260) -> pd.DataFrame:
    """
    Загружает историю торгов за последние `days` торговых дней.
    Возвращает DataFrame с колонками: TRADEDATE, OPEN, HIGH, LOW, CLOSE, VOLUME.
    При ошибке возвращает пустой DataFrame.

    ВАЖНО: эндпоинт ISS отдаёт максимум 100 строк за один запрос — нужна
    постраничная выборка через курсор `start`. Берём запас по календарным
    дням (выходные/праздники), чтобы хватило `days` ТОРГОВЫХ дней для SMA200.
    """
    # ~365 календарных дней ≈ 252 торговых; берём с запасом под нужное число дней
    calendar_days = int(days * 1.6) + 60
    from_date = (date.today() - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    url = (
        f"{MOEX_BASE_URL}/history/engines/stock/markets/shares"
        f"/boards/{MOEX_BOARD}/securities/{ticker}.json"
    )

    columns: list[str] = []
    rows: list[list] = []
    page_size = 100          # жёсткий лимит страницы ISS
    max_pages = 30           # защита от бесконечного цикла (~3000 строк)
    start = 0

    for _ in range(max_pages):
        params = {
            "from": from_date,
            "start": start,
            "iss.meta": "off",
            "iss.only": "history",
        }
        data = _get(url, params)
        if not data:
            break

        hist = data.get("history", {})
        if not columns:
            columns = hist.get("columns", [])
        page_rows: list[list] = hist.get("data", [])
        if not page_rows:
            break

        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break        # последняя страница
        start += len(page_rows)

    if not rows:
        logger.warning("Нет исторических данных для %s", ticker)
        return pd.DataFrame()

    try:
        df = pd.DataFrame(rows, columns=columns)

        # Оставляем нужные колонки
        needed = ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
        df = df[[c for c in needed if c in df.columns]].copy()
        df["TRADEDATE"] = pd.to_datetime(df["TRADEDATE"])
        df = df.sort_values("TRADEDATE").reset_index(drop=True)

        # Оставляем только последние `days` строк с данными
        df = df.dropna(subset=["CLOSE"])
        df = df.tail(days).reset_index(drop=True)

        logger.info("История %s: %d строк", ticker, len(df))
        return df

    except Exception as exc:
        logger.error("Ошибка разбора истории %s: %s", ticker, exc, exc_info=True)
        return pd.DataFrame()


# ──────────────────────────────────────────────────────────────
# Дивиденды
# ──────────────────────────────────────────────────────────────

def get_dividends(ticker: str) -> list[dict[str, Any]]:
    """
    Возвращает список последних 5 дивидендных выплат:
    [{"value": 33.45, "registryclosedate": "2024-07-18", ...}, ...]
    """
    url = f"{MOEX_BASE_URL}/securities/{ticker}/dividends.json"
    data = _get(url)

    if not data:
        return []

    try:
        divs = data.get("dividends", {})
        columns: list[str] = divs.get("columns", [])
        rows: list[list] = divs.get("data", [])

        if not rows:
            return []

        result = [dict(zip(columns, row)) for row in rows]
        # Сортируем по дате реестра (новые первыми) и берём 5 штук
        result.sort(key=lambda x: x.get("registryclosedate", ""), reverse=True)
        return result[:5]

    except Exception as exc:
        logger.error("Ошибка разбора дивидендов %s: %s", ticker, exc, exc_info=True)
        return []


def calc_div_yield(ticker: str, current_price: float) -> float:
    """
    Рассчитывает дивидендную доходность за последние 12 месяцев в %.
    Возвращает 0.0 если данных нет.
    """
    if current_price <= 0:
        return 0.0

    divs = get_dividends(ticker)
    if not divs:
        return 0.0

    # Берём выплаты за последний год
    one_year_ago = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    annual_div = sum(
        float(d.get("value") or 0)
        for d in divs
        if (d.get("registryclosedate") or "") >= one_year_ago
    )

    return round(annual_div / current_price * 100, 2)


# ──────────────────────────────────────────────────────────────
# Состав индекса IMOEX
# ──────────────────────────────────────────────────────────────

def get_index_composition() -> list[dict[str, Any]]:
    """
    Возвращает список бумаг и их весов в индексе IMOEX:
    [{"ticker": "SBER", "weight": 15.2}, ...]
    """
    url = f"{MOEX_BASE_URL}/statistics/engines/stock/markets/index/analytics/IMOEX.json"
    data = _get(url)

    if not data:
        return []

    try:
        analytics = data.get("analytics", {})
        columns: list[str] = analytics.get("columns", [])
        rows: list[list] = analytics.get("data", [])

        result = []
        for row in rows:
            item = dict(zip(columns, row))
            ticker = item.get("secid") or item.get("SECID")
            weight = item.get("weight") or item.get("WEIGHT")
            if ticker and weight is not None:
                result.append({"ticker": str(ticker), "weight": float(weight)})

        return result

    except Exception as exc:
        logger.error("Ошибка разбора состава IMOEX: %s", exc, exc_info=True)
        return []
