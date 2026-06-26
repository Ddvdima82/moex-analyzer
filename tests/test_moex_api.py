"""Тесты разбора ответов MOEX ISS (data/moex_api.py) без сети."""
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
