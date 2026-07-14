"""
Кэш дневных OHLCV-баров MOEX в SQLite (таблица ohlcv в том же history.db).

get_history_cached() — замена moex_api.get_history для пайплайна и бэктеста:
  • первый запрос по тикеру качает полную историю и складывает в кэш;
  • последующие — дельта-выборку от последнего закэшированного бара
    (1 HTTP-запрос на несколько строк вместо 3-6 постраничных);
  • при недоступности MOEX отдаёт кэш (вчерашние бары лучше нейтрального
    фолбэка — см. падение прогона 2026-07-13 на таймауте ISS).

Вызывается из пула потоков — на каждую операцию своё соединение
(timeout=30 гасит блокировки записи между воркерами). Никогда не бросает
исключений наружу: при любой ошибке ведёт себя как обычный get_history.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pandas as pd

from config import STORE_FILE, today_msk

logger = logging.getLogger(__name__)

_COLUMNS = ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    ticker    TEXT NOT NULL,
    tradedate TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    REAL,
    PRIMARY KEY (ticker, tradedate)
);
CREATE TABLE IF NOT EXISTS ohlcv_meta (
    ticker          TEXT PRIMARY KEY,
    full_fetch_from TEXT   -- нижняя граница последней ПОЛНОЙ выборки (YYYY-MM-DD)
);
"""


def _required_from(days: int):
    """Нижняя граница окна для `days` торговых дней (та же формула, что в get_history)."""
    return today_msk() - timedelta(days=int(days * 1.6) + 60)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or STORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def _load(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Вся закэшированная история тикера, отсортированная по дате."""
    rows = conn.execute(
        "SELECT tradedate, open, high, low, close, volume FROM ohlcv "
        "WHERE ticker = ? ORDER BY tradedate",
        (ticker,),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["TRADEDATE"] = pd.to_datetime(df["TRADEDATE"])
    return df


def _upsert(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> None:
    """Дописывает/обновляет бары (INSERT OR REPLACE — перекрытие дат безопасно)."""
    rows = [
        (
            ticker,
            pd.Timestamp(r.TRADEDATE).strftime("%Y-%m-%d"),
            r.OPEN, r.HIGH, r.LOW, r.CLOSE, r.VOLUME,
        )
        for r in df.itertuples()
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv "
            "(ticker, tradedate, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def get_history_cached(
    ticker: str,
    days: int = 260,
    db_path: Path | None = None,
) -> pd.DataFrame:
    """
    История торгов через SQLite-кэш: тот же контракт, что moex_api.get_history
    (TRADEDATE datetime, OPEN..VOLUME, последние `days` строк, пустой DF при
    полном отсутствии данных).
    """
    from data.moex_api import get_history

    conn: sqlite3.Connection | None = None
    cached = pd.DataFrame(columns=_COLUMNS)
    try:
        conn = _connect(db_path)
        cached = _load(conn, ticker)
    except Exception as exc:
        logger.warning("Кэш истории %s недоступен (%s) — работаем без кэша", ticker, exc)
        if conn is not None:
            conn.close()
            conn = None

    def _remember_full_fetch() -> None:
        """Помнит глубину полной выборки — защита от ежедневного перекачивания
        молодых тикеров (X5), у которых баров меньше, чем просят."""
        if conn is not None:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ohlcv_meta (ticker, full_fetch_from) VALUES (?, ?)",
                    (ticker, _required_from(days).strftime("%Y-%m-%d")),
                )

    try:
        # 1. Свежие бары: дельта от последнего бара кэша или полная выборка
        if cached.empty:
            fresh = get_history(ticker, days=days)
            if fresh is not None and not fresh.empty:
                _remember_full_fetch()
        else:
            last = cached["TRADEDATE"].max().strftime("%Y-%m-%d")
            fresh = get_history(ticker, from_date=last)

        if fresh is not None and not fresh.empty:
            if conn is not None:
                _upsert(conn, ticker, fresh)
                cached = _load(conn, ticker)
            else:
                cached = fresh  # кэш недоступен — работаем на свежих данных
        elif cached.empty:
            logger.warning("История %s: ни кэша, ни данных MOEX", ticker)
            return pd.DataFrame()
        else:
            logger.warning(
                "MOEX недоступен для %s — отдаём кэш (%d баров, до %s)",
                ticker, len(cached), cached["TRADEDATE"].max().date(),
            )

        # 2. Бэкфил: баров меньше, чем нужно, И полная выборка такой глубины ещё
        # не делалась (иначе это молодой тикер — глубже истории просто нет,
        # и повторная полная выборка каждый прогон ничего не даст)
        if 0 < len(cached) < days and conn is not None:
            row = conn.execute(
                "SELECT full_fetch_from FROM ohlcv_meta WHERE ticker = ?", (ticker,)
            ).fetchone()
            deep_enough = row and row[0] and row[0] <= _required_from(days).strftime("%Y-%m-%d")
            if not deep_enough:
                full = get_history(ticker, days=days)
                if full is not None and not full.empty:
                    _remember_full_fetch()
                    _upsert(conn, ticker, full)
                    cached = _load(conn, ticker)

        return cached.tail(days).reset_index(drop=True)
    except Exception as exc:
        logger.error("Ошибка кэша истории %s: %s — фолбэк на прямую выборку", ticker, exc)
        try:
            return get_history(ticker, days=days)
        except Exception:
            return pd.DataFrame()
    finally:
        if conn is not None:
            conn.close()
