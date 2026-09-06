"""Тесты разбора ответов MOEX ISS (data/moex_api.py) без сети."""
import pandas as pd
from datetime import timedelta

from config import today_msk
from data import moex_api


def _history_rows(n):
    cols = ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    data = []
    for i in range(n):
        d = f"2025-01-{(i % 28) + 1:02d}"
        data.append([d, 100 + i, 101 + i, 99 + i, 100 + i, 1000 + i])
    return cols, data


def test_get_history_paginates(monkeypatch):
    """Курсорная пагинация: 150 строк приходят двумя страницами (100 + 50)."""
    cols, all_rows = _history_rows(150)

    def fake_get(url, params=None):
        start = params.get("start", 0)
        page = all_rows[start:start + 100]      # ISS отдаёт максимум 100
        return {"history": {"columns": cols, "data": page}}

    monkeypatch.setattr(moex_api, "_get", fake_get)
    df = moex_api.get_history("SBER", days=260)
    assert len(df) == 150                        # обе страницы собраны
    assert list(df.columns) == ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    assert df["TRADEDATE"].is_monotonic_increasing


def test_get_history_tail_limit(monkeypatch):
    """tail(days) ограничивает число строк запрошенным окном."""
    cols, all_rows = _history_rows(150)

    def fake_get(url, params=None):
        start = params.get("start", 0)
        return {"history": {"columns": cols, "data": all_rows[start:start + 100]}}

    monkeypatch.setattr(moex_api, "_get", fake_get)
    df = moex_api.get_history("SBER", days=50)
    assert len(df) == 50


def test_get_history_empty(monkeypatch):
    monkeypatch.setattr(moex_api, "_get", lambda url, params=None: None)
    assert moex_api.get_history("SBER").empty


def test_get_current_quotes_parsing(monkeypatch):
    columns = ["SECID", "LAST", "OPEN", "HIGH", "LOW", "PREVPRICE", "VOLTODAY"]
    data = [
        ["SBER", 312.5, 314.0, 315.2, 311.8, 314.0, 45_000_000],
        ["GAZP", 130.0, 131.0, 132.0, 129.0, 132.0, 10_000_000],
    ]
    monkeypatch.setattr(
        moex_api, "_get",
        lambda url, params=None: {"marketdata": {"columns": columns, "data": data}},
    )
    q = moex_api.get_current_quotes(["SBER", "GAZP"])
    assert q["SBER"]["price"] == 312.5
    assert q["SBER"]["volume"] == 45_000_000
    # change_pct = (312.5/314 - 1) * 100 ≈ -0.48
    assert q["SBER"]["change_pct"] == -0.48
    assert set(q.keys()) == {"SBER", "GAZP"}


def test_get_current_quotes_no_data(monkeypatch):
    monkeypatch.setattr(moex_api, "_get", lambda url, params=None: None)
    assert moex_api.get_current_quotes(["SBER"]) == {}


def _dividends_response(rows):
    return {"dividends": {"columns": ["registryclosedate", "value"], "data": rows}}


def test_calc_div_yield_excludes_future_announced_payout(monkeypatch):
    """Объявленная будущая отсечка не должна попадать в trailing-расчёт —
    она учитывается отдельно (forward-добавка в main.py), иначе задвоение."""
    today = today_msk()
    within_year = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    future = (today + timedelta(days=60)).strftime("%Y-%m-%d")
    rows = [[future, 25.0], [within_year, 10.0]]
    monkeypatch.setattr(
        moex_api, "_get", lambda url, params=None: _dividends_response(rows)
    )
    # Только within_year (10.0) должен войти в trailing; future (25.0) — нет
    assert moex_api.calc_div_yield("SBER", 100.0) == 10.0


def test_calc_div_yield_excludes_payout_older_than_a_year(monkeypatch):
    today = today_msk()
    within_year = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    too_old = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    rows = [[within_year, 10.0], [too_old, 20.0]]
    monkeypatch.setattr(
        moex_api, "_get", lambda url, params=None: _dividends_response(rows)
    )
    assert moex_api.calc_div_yield("SBER", 100.0) == 10.0


# ── Свечи: источник баров выходного дня ──────────────────────────────────────

def _candles_payload(rows):
    return {"candles": {
        "columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
        "data": rows,
    }}


def test_get_candles_normalizes_to_history_format(monkeypatch):
    """Формат совпадает с get_history: TRADEDATE (дата бара) + OHLCV."""
    import data.moex_api as api

    monkeypatch.setattr(api, "_get", lambda *a, **kw: _candles_payload([
        [280.9, 278.48, 281.01, 278.06, 6.1e8, 2189750, "2026-09-06 00:00:00", "2026-09-06 12:11:31"],
    ]))
    df = api.get_candles("SBER", from_date="2026-09-05")

    assert list(df.columns) == ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    # begin приходит с временем — нужна ровно дата, иначе ключ не сойдётся с /history
    assert df["TRADEDATE"].iloc[0] == pd.Timestamp("2026-09-06")
    assert df["CLOSE"].iloc[0] == 278.48
    assert df["HIGH"].iloc[0] == 281.01


def test_get_candles_sorted_and_without_empty_closes(monkeypatch):
    import data.moex_api as api

    monkeypatch.setattr(api, "_get", lambda *a, **kw: _candles_payload([
        [1, 3.0, 1, 1, 1, 1, "2026-09-06 00:00:00", "2026-09-06 23:59:59"],
        [1, None, 1, 1, 1, 1, "2026-09-05 00:00:00", "2026-09-05 23:59:59"],
        [1, 2.0, 1, 1, 1, 1, "2026-09-04 00:00:00", "2026-09-04 23:59:59"],
    ]))
    df = api.get_candles("SBER", from_date="2026-09-04")

    assert len(df) == 2                                     # бар без close отброшен
    assert list(df["TRADEDATE"]) == sorted(df["TRADEDATE"])  # по возрастанию даты


def test_get_candles_network_failure_returns_empty(monkeypatch):
    import data.moex_api as api

    monkeypatch.setattr(api, "_get", lambda *a, **kw: None)
    assert api.get_candles("SBER", from_date="2026-09-05").empty


def test_get_candles_empty_payload(monkeypatch):
    import data.moex_api as api

    monkeypatch.setattr(api, "_get", lambda *a, **kw: _candles_payload([]))
    assert api.get_candles("SBER", from_date="2026-09-05").empty
