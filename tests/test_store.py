"""Тесты SQLite-хранилища прогонов (data/store.py)."""
from data import store


def _stock(ticker="SBER", score=80.0, signal="BUY"):
    return {
        "ticker": ticker, "company": "Сбербанк", "price": 300.0,
        "final_score": score, "signal": signal, "target_price": 330.0,
        "upside_pct": 10.0, "scores": {"fundamental": 80, "technical": 70, "sentiment": 60},
        "indicators": {"rsi": 55.0, "above_sma200": True, "volume_trend_pct": 12.0},
    }


def test_save_and_load_roundtrip(tmp_path):
    db = tmp_path / "h.db"
    n = store.save_run([_stock("SBER"), _stock("GAZP", 40.0, "HOLD")], run_date="2026-06-26", db_path=db)
    assert n == 2

    rows = store.load_run("2026-06-26", db_path=db)
    assert len(rows) == 2
    assert rows[0]["ticker"] == "SBER"          # отсортировано по убыванию балла
    assert rows[0]["final_score"] == 80.0
    # scores и indicators сериализованы в JSON
    assert '"fundamental"' in rows[0]["scores_json"]
    assert '"rsi"' in rows[0]["indicators_json"]


def test_migration_adds_indicators_column(tmp_path):
    """БД, созданная до появления indicators_json, мигрирует без ошибок."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE runs (
            run_date TEXT NOT NULL, ticker TEXT NOT NULL, company TEXT,
            price REAL, final_score REAL, signal TEXT, target_price REAL,
            upside_pct REAL, scores_json TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY (run_date, ticker)
        );
        INSERT INTO runs VALUES ('2026-06-19','SBER','Сбербанк',300,80,'BUY',330,10,'{}','2026-06-19');
    """)
    conn.commit()
    conn.close()

    store.save_run([_stock("SBER")], run_date="2026-06-26", db_path=db)
    old = store.load_run("2026-06-19", db_path=db)
    new = store.load_run("2026-06-26", db_path=db)
    assert old[0]["indicators_json"] is None      # старая строка — колонка пуста
    assert '"rsi"' in new[0]["indicators_json"]   # новая — заполнена


def test_save_run_idempotent_per_date(tmp_path):
    db = tmp_path / "h.db"
    store.save_run([_stock("SBER", 80.0)], run_date="2026-06-26", db_path=db)
    # Повторный прогон за ту же дату перезаписывает, а не дублирует
    store.save_run([_stock("SBER", 55.0), _stock("LKOH", 60.0)], run_date="2026-06-26", db_path=db)

    rows = store.load_run("2026-06-26", db_path=db)
    assert len(rows) == 2
    sber = next(r for r in rows if r["ticker"] == "SBER")
    assert sber["final_score"] == 55.0          # значение обновилось


def test_separate_dates_kept(tmp_path):
    db = tmp_path / "h.db"
    store.save_run([_stock("SBER")], run_date="2026-06-19", db_path=db)
    store.save_run([_stock("SBER")], run_date="2026-06-26", db_path=db)
    assert len(store.load_run("2026-06-19", db_path=db)) == 1
    assert len(store.load_run("2026-06-26", db_path=db)) == 1


def test_load_missing_date_empty(tmp_path):
    db = tmp_path / "h.db"
    assert store.load_run("1999-01-01", db_path=db) == []
