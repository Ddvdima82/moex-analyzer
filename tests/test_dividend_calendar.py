"""Тесты фолбэк-календаря отсечек (data/dividend_calendar.py)."""
from datetime import timedelta

from config import today_msk
from data.dividend_calendar import _parse_smartlab_html, get_upcoming_dividends_smartlab


def _page(rows: str) -> str:
    return f"<html><body><table>{rows}</table></body></html>"


def _future(days: int) -> str:
    return (today_msk() + timedelta(days=days)).strftime("%d.%m.%Y")


def _past(days: int) -> str:
    return (today_msk() - timedelta(days=days)).strftime("%d.%m.%Y")


def test_parse_future_cutoff():
    html = _page(
        "<tr><td><a href='/q/SBER/dividend/'>Сбербанк</a></td><td>SBER</td>"
        f"<td>34,84</td><td>6,9%</td><td>295,5</td><td>{_future(2)}</td><td>{_future(4)}</td></tr>"
    )
    out = _parse_smartlab_html(html, ["SBER", "LKOH"])
    assert "SBER" in out
    assert out["SBER"]["amount"] == 34.84
    # первая будущая дата строки = последний день покупки
    assert out["SBER"]["ex_date"] == (today_msk() + timedelta(days=2)).strftime("%Y-%m-%d")


def test_parse_past_cutoff_skipped():
    html = _page(
        "<tr><td>Лукойл</td><td>LKOH</td><td>500</td><td>7%</td>"
        f"<td>{_past(10)}</td><td>{_past(8)}</td></tr>"
    )
    assert _parse_smartlab_html(html, ["LKOH"]) == {}


def test_parse_unknown_ticker_ignored():
    html = _page(
        f"<tr><td>Чужая</td><td>ZZZZ</td><td>10</td><td>{_future(3)}</td></tr>"
    )
    assert _parse_smartlab_html(html, ["SBER"]) == {}


def test_parse_percent_cell_not_taken_as_amount():
    html = _page(
        f"<tr><td>МТС</td><td>MTSS</td><td>6,9%</td><td>35,0</td><td>{_future(5)}</td></tr>"
    )
    out = _parse_smartlab_html(html, ["MTSS"])
    assert out["MTSS"]["amount"] == 35.0


def test_parse_garbage_html_empty():
    assert _parse_smartlab_html("<html>ничего</html>", ["SBER"]) == {}
    assert _parse_smartlab_html("", ["SBER"]) == {}


def test_network_failure_returns_empty(monkeypatch):
    import data.dividend_calendar as dc

    def _boom(*a, **kw):
        raise OSError("нет сети")

    monkeypatch.setattr(dc.requests, "get", _boom)
    assert get_upcoming_dividends_smartlab(["SBER"]) == {}
