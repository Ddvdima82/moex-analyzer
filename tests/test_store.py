"""Тесты SQLite-хранилища прогонов (data/store.py)."""
from data import store


def _stock(ticker="SBER", score=80.0, signal="BUY"):
    return {
        "ticker": ticker, "company": "Сбербанк", "price": 300.0,
        "final_score": score, "signal": signal, "target_price": 330.0,
        "upside_pct": 10.0, "scores": {"fundamental": 80, "technical": 70, "sentiment": 60},
    }


def test_save_and_load_roundtrip(tmp_path):
    db = tmp_path / "h.db"
    n = store.save_run([_stock("SBER"), _stock("GAZP", 40.0, "HOLD")], run_date="2026-06-26", db_path=db)
    assert n == 2

    rows = store.load_run("2026-06-26", db_path=db)
    assert len(rows) == 2
    assert rows[0]["ticker"] == "SBER"          # отсортировано по убыванию балла
    assert rows[0]["final_score"] == 80.0
    # scores сериализованы в JSON
    assert '"fundamental"' in rows[0]["scores_json"]


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
