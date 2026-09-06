"""Тесты SQLite-кэша истории OHLCV (data/history_cache.py). Сеть замокана."""
import pandas as pd
import pytest

import data.history_cache as hc
import data.moex_api as moex_api


def _df(dates: list[str], base: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        "TRADEDATE": pd.to_datetime(dates),
        "OPEN": [base] * len(dates),
        "HIGH": [base + 1] * len(dates),
        "LOW": [base - 1] * len(dates),
        "CLOSE": [base + i for i in range(len(dates))],
        "VOLUME": [1000.0] * len(dates),
    })


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cache.db"


@pytest.fixture(autouse=True)
def _no_candles(monkeypatch):
    """
    Свечной хвост по умолчанию пуст: тесты обязаны быть network-free, а
    get_candles иначе ушёл бы в реальный ISS. Тесты про выходные сессии
    переопределяют этот мок явно.
    """
    monkeypatch.setattr(moex_api, "get_candles", lambda *a, **kw: pd.DataFrame())


def test_first_call_full_fetch_and_store(db, monkeypatch):
    calls = []

    def fake_history(ticker, days=260, from_date=None):
        calls.append({"days": days, "from_date": from_date})
        return _df(["2026-07-10", "2026-07-13", "2026-07-14"])

    monkeypatch.setattr(moex_api, "get_history", fake_history)
    df = hc.get_history_cached("SBER", days=260, db_path=db)

    assert len(df) == 3
    assert list(df.columns) == hc._COLUMNS
    assert calls[0]["from_date"] is None          # первая выборка — полная

    # Кэш заполнен
    conn = hc._connect(db)
    assert len(hc._load(conn, "SBER")) == 3
    conn.close()


def test_second_call_delta_fetch(db, monkeypatch):
    calls = []

    def fake_history(ticker, days=260, from_date=None):
        calls.append(from_date)
        if from_date is None:
            return _df(["2026-07-10", "2026-07-13"])
        return _df(["2026-07-13", "2026-07-14"])   # перекрытие последнего бара

    monkeypatch.setattr(moex_api, "get_history", fake_history)
    hc.get_history_cached("SBER", days=260, db_path=db)
    df = hc.get_history_cached("SBER", days=260, db_path=db)

    assert calls == [None, "2026-07-13"]           # дельта от последнего бара
    assert len(df) == 3                            # перекрытие не дублируется
    assert df["TRADEDATE"].is_monotonic_increasing


def test_network_failure_serves_cache(db, monkeypatch):
    state = {"fail": False}

    def fake_history(ticker, days=260, from_date=None):
        if state["fail"]:
            return pd.DataFrame()                  # MOEX недоступен
        return _df(["2026-07-13", "2026-07-14"])

    monkeypatch.setattr(moex_api, "get_history", fake_history)
    hc.get_history_cached("SBER", days=260, db_path=db)

    state["fail"] = True
    df = hc.get_history_cached("SBER", days=260, db_path=db)
    assert len(df) == 2                            # отдан кэш, не пустой фолбэк


def test_no_cache_no_network_empty(db, monkeypatch):
    monkeypatch.setattr(moex_api, "get_history",
                        lambda ticker, days=260, from_date=None: pd.DataFrame())
    df = hc.get_history_cached("SBER", days=260, db_path=db)
    assert df.empty


def test_tail_days_respected(db, monkeypatch):
    dates = [f"2026-06-{d:02d}" for d in range(1, 29)]
    monkeypatch.setattr(moex_api, "get_history",
                        lambda ticker, days=260, from_date=None: _df(dates))
    df = hc.get_history_cached("SBER", days=10, db_path=db)
    assert len(df) == 10
    assert df["TRADEDATE"].iloc[-1] == pd.Timestamp("2026-06-28")


def test_backfill_when_cache_window_too_short(db, monkeypatch):
    """Кэш засеян под 5 баров, запросили 20 — окно короткое, нужен бэкфил."""
    from datetime import timedelta
    from config import today_msk

    recent = [(today_msk() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4, 0, -1)]
    older = [(today_msk() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30, 4, -1)]
    calls = []

    def fake_history(ticker, days=260, from_date=None):
        calls.append((days, from_date))
        if from_date is not None:
            return _df(recent[-1:])
        return _df(recent) if days <= 5 else _df(older + recent)

    monkeypatch.setattr(moex_api, "get_history", fake_history)
    hc.get_history_cached("SBER", days=5, db_path=db)      # засев: 4 бара
    df = hc.get_history_cached("SBER", days=20, db_path=db)

    assert len(df) == 20
    assert (20, None) in calls                              # был полный бэкфил


def test_young_ticker_not_refetched_daily(db, monkeypatch):
    """У молодого тикера (X5) баров меньше, чем просят, — полная выборка
    делается один раз, а не каждый прогон заново."""
    full_calls = []

    def fake_history(ticker, days=260, from_date=None):
        if from_date is None:
            full_calls.append(days)
            return _df(["2026-07-10", "2026-07-13", "2026-07-14"])  # вся история
        return _df(["2026-07-14"])

    monkeypatch.setattr(moex_api, "get_history", fake_history)
    hc.get_history_cached("X5", days=260, db_path=db)
    hc.get_history_cached("X5", days=260, db_path=db)
    hc.get_history_cached("X5", days=260, db_path=db)

    assert full_calls == [260]                     # одна полная выборка, дальше дельты


def test_separate_tickers_isolated(db, monkeypatch):
    monkeypatch.setattr(moex_api, "get_history",
                        lambda ticker, days=260, from_date=None:
                        _df(["2026-07-14"], base=100.0 if ticker == "SBER" else 200.0))
    sber = hc.get_history_cached("SBER", db_path=db)
    gazp = hc.get_history_cached("GAZP", db_path=db)
    assert sber["OPEN"].iloc[0] == 100.0
    assert gazp["OPEN"].iloc[0] == 200.0


# ── Хвост из свечей: торги выходного дня ─────────────────────────────────────

def _bars(dates, close=100.0):
    import pandas as pd
    return pd.DataFrame({
        "TRADEDATE": pd.to_datetime(dates),
        "OPEN": [close] * len(dates), "HIGH": [close + 1] * len(dates),
        "LOW": [close - 1] * len(dates), "CLOSE": [close] * len(dates),
        "VOLUME": [1000] * len(dates),
    })


def test_weekend_candles_extend_history(tmp_path, monkeypatch):
    """
    /history не отдаёт сессии выходного дня, /candles отдаёт — иначе прогон
    в субботу-воскресенье считал бы индикаторы по пятничным барам.
    """
    import data.moex_api as api
    import pandas as pd

    monkeypatch.setattr(api, "get_history", lambda *a, **kw: _bars(["2026-09-03", "2026-09-04"]))
    monkeypatch.setattr(
        api, "get_candles",
        lambda t, from_date, **kw: _bars(["2026-09-04", "2026-09-05", "2026-09-06"]),
    )

    df = hc.get_history_cached("SBER", days=260, db_path=tmp_path / "h.db")
    dates = [str(d.date()) for d in df["TRADEDATE"]]
    assert dates[-3:] == ["2026-09-04", "2026-09-05", "2026-09-06"]


def test_candles_do_not_duplicate_existing_bars(tmp_path, monkeypatch):
    """Свечи за уже известные даты не создают дублей — апсерт по (тикер, дата)."""
    import data.moex_api as api

    monkeypatch.setattr(api, "get_history", lambda *a, **kw: _bars(["2026-09-03", "2026-09-04"]))
    monkeypatch.setattr(api, "get_candles", lambda t, from_date, **kw: _bars(["2026-09-03", "2026-09-04"]))

    df = hc.get_history_cached("SBER", days=260, db_path=tmp_path / "h.db")
    assert len(df) == len(set(str(d.date()) for d in df["TRADEDATE"]))


def test_candles_failure_does_not_break_history(tmp_path, monkeypatch):
    """Падение свечей оставляет обычную историю рабочей."""
    import data.moex_api as api

    def _boom(*a, **kw):
        raise OSError("candles недоступны")

    monkeypatch.setattr(api, "get_history", lambda *a, **kw: _bars(["2026-09-03", "2026-09-04"]))
    monkeypatch.setattr(api, "get_candles", _boom)

    df = hc.get_history_cached("SBER", days=260, db_path=tmp_path / "h.db")
    assert len(df) == 2


def test_official_bar_overwrites_candle_bar(tmp_path, monkeypatch):
    """
    Когда MOEX публикует официальный бар за дату, ранее закрытую свечой,
    он должен перезаписать свечное значение, а не остаться рядом.
    """
    import data.moex_api as api

    db = tmp_path / "h.db"
    # Первый прогон: официальной истории нет, дату закрывает свеча
    monkeypatch.setattr(api, "get_history", lambda *a, **kw: _bars(["2026-09-04"], close=280.0))
    monkeypatch.setattr(api, "get_candles", lambda t, from_date, **kw: _bars(["2026-09-05"], close=281.0))
    hc.get_history_cached("SBER", days=260, db_path=db)

    # Второй прогон: MOEX опубликовал официальный бар за ту же дату
    monkeypatch.setattr(
        api, "get_history",
        lambda *a, **kw: _bars(["2026-09-04", "2026-09-05"], close=999.0),
    )
    monkeypatch.setattr(api, "get_candles", lambda t, from_date, **kw: __import__("pandas").DataFrame())
    df = hc.get_history_cached("SBER", days=260, db_path=db)

    row = df[df["TRADEDATE"] == __import__("pandas").Timestamp("2026-09-05")]
    assert len(row) == 1
    assert float(row["CLOSE"].iloc[0]) == 999.0
