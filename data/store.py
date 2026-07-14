"""
Персистентность результатов прогона в SQLite.

Одна строка = (дата прогона, тикер). Хранит итоговый балл, сигнал, цену и
цель — минимум, нужный для последующего бэктеста (сравнение сигналов с
форвардной доходностью). Полные сырые данные остаются в reports/*.json.

Повторный прогон за ту же дату перезаписывает строки этой даты (идемпотентно).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from config import STORE_FILE, today_msk

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_date      TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    company       TEXT,
    price         REAL,
    final_score   REAL,
    signal        TEXT,
    target_price  REAL,
    upside_pct    REAL,
    scores_json   TEXT,
    indicators_json TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (run_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_runs_ticker ON runs (ticker);
CREATE INDEX IF NOT EXISTS idx_runs_date   ON runs (run_date);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or STORE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    # Миграция БД, созданных до появления indicators_json (CREATE IF NOT EXISTS
    # не добавляет колонки в существующую таблицу)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "indicators_json" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN indicators_json TEXT")
        conn.commit()
    return conn


def save_run(
    results: list[dict[str, Any]],
    run_date: str | None = None,
    db_path: Path | None = None,
) -> int:
    """
    Сохраняет результаты прогона. Возвращает число записанных строк.
    Строки за `run_date` предварительно удаляются (идемпотентность за день).
    """
    run_date = run_date or today_msk().strftime("%Y-%m-%d")
    created = today_msk().isoformat()

    rows = [
        (
            run_date,
            r["ticker"],
            r.get("company"),
            r.get("price"),
            r.get("final_score"),
            r.get("signal"),
            r.get("target_price"),
            r.get("upside_pct"),
            json.dumps(r.get("scores", {}), ensure_ascii=False),
            json.dumps(r.get("indicators", {}), ensure_ascii=False),
            created,
        )
        for r in results
    ]

    conn = None
    try:
        conn = _connect(db_path)
        with conn:
            conn.execute("DELETE FROM runs WHERE run_date = ?", (run_date,))
            conn.executemany(
                "INSERT INTO runs (run_date, ticker, company, price, final_score, "
                "signal, target_price, upside_pct, scores_json, indicators_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        logger.info("Сохранено в БД: %d строк за %s", len(rows), run_date)
        return len(rows)
    except Exception as exc:
        logger.error("Ошибка записи в SQLite: %s", exc, exc_info=True)
        return 0
    finally:
        if conn is not None:
            conn.close()


def has_run_earlier_this_week(
    today: "date | None" = None,
    db_path: Path | None = None,
) -> bool:
    """
    True если в SQLite уже есть прогон на текущей ISO-неделе с датой РАНЬШЕ
    сегодняшней. Управляет отправкой полного отчёта: он уходит в первый
    успешный прогон недели (обычно понедельник; упал понедельник → вторник),
    а не строго по дню недели. При ошибке чтения БД возвращает True —
    консервативно молчим, а не спамим отчётом каждый день.
    """
    from datetime import datetime as _dt

    today = today or today_msk()
    iso_week = today.isocalendar()[:2]
    try:
        conn = _connect(db_path)
        dates = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT run_date FROM runs WHERE run_date < ?",
                (today.strftime("%Y-%m-%d"),),
            )
        ]
        conn.close()
    except Exception as exc:
        logger.error("Ошибка чтения дат прогонов недели: %s", exc)
        return True

    for d in dates:
        try:
            if _dt.strptime(d, "%Y-%m-%d").date().isocalendar()[:2] == iso_week:
                return True
        except ValueError:
            continue
    return False


def get_prev_signals(
    before_date: str | None = None,
    db_path: Path | None = None,
) -> dict[str, str]:
    """
    Сигналы последнего прогона СТРОГО раньше before_date (по умолчанию — сегодня,
    МСК). Для гистерезиса сигналов: повторный прогон за тот же день не должен
    опираться сам на себя. {} если истории нет — get_signal перейдёт на чистые пороги.
    """
    before_date = before_date or today_msk().strftime("%Y-%m-%d")
    try:
        conn = _connect(db_path)
        row = conn.execute(
            "SELECT MAX(run_date) FROM runs WHERE run_date < ?", (before_date,)
        ).fetchone()
        prev_date = row[0] if row else None
        if not prev_date:
            conn.close()
            return {}
        out = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT ticker, signal FROM runs WHERE run_date = ?", (prev_date,)
            )
            if r[1]
        }
        conn.close()
        return out
    except Exception as exc:
        logger.error("Ошибка чтения прошлых сигналов: %s", exc)
        return {}


def get_last_two_run_dates(db_path: Path | None = None) -> list[str]:
    """Возвращает последние 2 даты прогонов (новые первыми), [] если нет данных."""
    try:
        conn = _connect(db_path)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT 2"
        ).fetchall()]
        conn.close()
        return dates
    except Exception as exc:
        logger.error("Ошибка чтения дат прогонов: %s", exc)
        return []


def load_run(run_date: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Читает строки прогона за дату (для проверки/бэктеста)."""
    try:
        conn = _connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM runs WHERE run_date = ? ORDER BY final_score DESC", (run_date,))
        out = [dict(row) for row in cur.fetchall()]
        conn.close()
        return out
    except Exception as exc:
        logger.error("Ошибка чтения из SQLite: %s", exc, exc_info=True)
        return []
