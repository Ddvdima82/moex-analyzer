"""Тесты фолбэк-календаря отсечек (data/dividend_calendar.py)."""
from datetime import timedelta

from config import today_msk
from data.dividend_calendar import (
    _header_column_map,
    _parse_smartlab_html,
    get_upcoming_dividends_smartlab,
)

# Реальная разметка smart-lab: колонки (по индексу) — имя, тикер, период,
# дивиденд, доходность, %, "Закр. РД"/T+2 (последний день покупки),
# "Дата отсечки реестра" (реальная отсечка — она нам и нужна), "Дата выплаты", цена.
_THEAD = """
<thead class="sticky_thead">
<tr>
<th><a href="/dividends/index/order_by_short_name/desc/">Имя</a></th>
<th><a href="/dividends/index/order_by_ticker/desc/">Тикер</a></th>
<th>Период</th>
<th><a href="/dividends/index/order_by_dividend/desc/">Дивиденд</a></th>
<th><a href="/dividends/index/order_by_yield/desc/">Доходность</a></th>
<th>%</th>
<th><a href="/dividends/index/order_by_t2_date/desc/">Закр. РД</a></th>
<th><a href="/dividends/index/order_by_cut_off_date/desc/">Дата отсечки реестра</a></th>
<th><a href="/dividends/index/order_by_payment_date/desc/">Дата выплаты</a></th>
</tr>
</thead>
"""


def _row(ticker: str, amount: str, t2: str, cutoff: str, payment: str) -> str:
    return (
        f"<tr class='dividend_approved'><td><a>Компания</a></td><td>{ticker}</td>"
        f"<td>2026</td><td><strong>{amount}</strong></td><td>7,0%</td><td></td>"
        f"<td>{t2}</td><td>{cutoff}</td><td>{payment}</td></tr>"
    )


def _page(*tbody_rows: str) -> str:
    """Каждый аргумент — содержимое отдельного <tbody> (страница реально их дробит)."""
    bodies = "".join(f"<tbody>{rows}</tbody>" for rows in tbody_rows)
    return f"<html><body><table>{_THEAD}{bodies}</table></body></html>"


def _future(days: int) -> str:
    return (today_msk() + timedelta(days=days)).strftime("%d.%m.%Y")


def _past(days: int) -> str:
    return (today_msk() - timedelta(days=days)).strftime("%d.%m.%Y")


def test_header_column_map_finds_indices():
    col_map = _header_column_map(_page(""))
    assert col_map == {"ticker": 1, "amount": 3, "cutoff_date": 7}


def test_parse_future_cutoff():
    html = _page(_row("SBER", "34,84", _future(1), _future(3), _future(31)))
    out = _parse_smartlab_html(html, ["SBER", "LKOH"])
    assert out["SBER"]["amount"] == 34.84
    assert out["SBER"]["ex_date"] == (today_msk() + timedelta(days=3)).strftime("%Y-%m-%d")


def test_parse_past_cutoff_skipped_even_with_future_payment_date():
    """
    Регресс на баг 2026-07-17..07-30: реальная отсечка (cutoff) уже прошла,
    но дата ВЫПЛАТЫ (payment) ещё в будущем. Раньше наивный парсер («первая
    будущая дата в строке») хватал payment date и выдавал её за отсечку.
    """
    html = _page(_row("SBER", "37,64", _past(10), _past(3), _future(20)))
    assert _parse_smartlab_html(html, ["SBER"]) == {}


def test_parse_reads_from_all_tbody_sections():
    """
    Регресс: страница реально разбита на несколько <tbody> (по секциям
    календаря) — нужная строка может быть НЕ в первом блоке.
    """
    html = _page(
        _row("GAZP", "10,0", _past(30), _past(20), _past(10)),   # 1-й tbody, не то
        _row("SBER", "34,84", _future(1), _future(3), _future(31)),  # 2-й tbody
    )
    out = _parse_smartlab_html(html, ["SBER"])
    assert "SBER" in out


def test_parse_unknown_ticker_ignored():
    html = _page(_row("ZZZZ", "10,0", _future(1), _future(3), _future(31)))
    assert _parse_smartlab_html(html, ["SBER"]) == {}


def test_parse_missing_header_columns_returns_empty():
    """Сменилась вёрстка (нет ожидаемых order_by-якорей) — {}, не угаданный результат."""
    html = "<html><table><thead><tr><th>Что-то другое</th></tr></thead><tbody></tbody></table></html>"
    assert _parse_smartlab_html(html, ["SBER"]) == {}


def test_parse_garbage_html_empty():
    assert _parse_smartlab_html("<html>ничего</html>", ["SBER"]) == {}
    assert _parse_smartlab_html("", ["SBER"]) == {}


def test_parse_zero_or_missing_amount_skipped():
    html = _page(_row("SBER", "0", _future(1), _future(3), _future(31)))
    assert _parse_smartlab_html(html, ["SBER"]) == {}


def test_network_failure_returns_empty(monkeypatch):
    import data.dividend_calendar as dc

    def _boom(*a, **kw):
        raise OSError("нет сети")

    monkeypatch.setattr(dc.requests, "get", _boom)
    assert get_upcoming_dividends_smartlab(["SBER"]) == {}
