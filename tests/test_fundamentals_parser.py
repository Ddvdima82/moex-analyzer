"""Тесты автопарсера фундаментала (data/fundamentals_parser.py)."""
import json
from datetime import timedelta

from config import today_msk
from data.fundamentals_parser import (
    _extract_row,
    _num,
    _parse_tables,
    fetch_fundamentals_smartlab,
    load_auto_fundamentals,
    update_fundamentals_auto,
)

# Разметка smart-lab: заголовки без <thead>, колонки опознаются по order_by-якорям.
# Две таблицы с РАЗНЫМ набором колонок: нефинансовые (debt_ebitda, нет roe)
# и банки (roe, нет debt_ebitda).
_NONFIN_HEAD = (
    "<tr>"
    "<th>#</th>"
    "<th><a href='/q/shares_fundamental/order_by_short_name/desc/'>Имя</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_sec_id/desc/'>Тикер</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_market_cap/desc/'>Кап</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_revenue/desc/'>Выручка</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_net_income/desc/'>Прибыль</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_p_e/desc/'>P/E</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_p_bv/desc/'>P/BV</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_debt_ebitda/desc/'>Долг/EBITDA</a></th>"
    "</tr>"
)
_BANK_HEAD = (
    "<tr>"
    "<th>#</th>"
    "<th><a href='/q/shares_fundamental/order_by_short_name/desc/'>Имя</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_sec_id/desc/'>Тикер</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_market_cap/desc/'>Кап</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_net_income/desc/'>Прибыль</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_p_e/desc/'>P/E</a></th>"
    "<th><a href='/q/shares_fundamental/order_by_roe/desc/'>RoE</a></th>"
    "</tr>"
)


def _nonfin_row(ticker, cap, revenue, net_income, pe, pbv, debt):
    return (
        f"<tr><td>1</td><td><a>Компания</a></td><td>{ticker}</td><td>{cap}</td>"
        f"<td>{revenue}</td><td>{net_income}</td><td>{pe}</td><td>{pbv}</td><td>{debt}</td></tr>"
    )


def _bank_row(ticker, cap, net_income, pe, roe):
    return (
        f"<tr><td>1</td><td><a>Банк</a></td><td>{ticker}</td><td>{cap}</td>"
        f"<td>{net_income}</td><td>{pe}</td><td>{roe}</td></tr>"
    )


def _page(*blocks):
    return "<html><body><table>" + "".join(blocks) + "</table></body></html>"


# ── Разбор чисел ────────────────────────────────────────────────────────────

def test_num_parsing():
    assert _num("3 498") == 3498.0        # неразрывный/обычный пробел как разделитель
    assert _num("24%") == 24.0
    assert _num("-0.3") == -0.3
    assert _num("1,5") == 1.5             # запятая как десятичный разделитель
    assert _num("") is None
    assert _num("-") is None
    assert _num("н/д") is None


# ── Структура таблиц ────────────────────────────────────────────────────────

def test_parse_tables_finds_two_layouts():
    html = _page(_NONFIN_HEAD, _nonfin_row("LKOH", "3 498", "3 768", "92.5", "37.8", "1.1", "-0.3"),
                 _BANK_HEAD, _bank_row("SBER", "6 340", "1 707", "3.7", "22.7%"))
    tables = _parse_tables(html)
    assert len(tables) == 2
    assert "debt_ebitda" in tables[0][0] and "roe" not in tables[0][0]
    assert "roe" in tables[1][0] and "debt_ebitda" not in tables[1][0]


def test_rows_do_not_leak_between_tables():
    """Строки нефинансовой таблицы не должны попадать в банковскую и наоборот."""
    html = _page(_NONFIN_HEAD, _nonfin_row("LKOH", "3 498", "3 768", "92.5", "37.8", "1.1", "-0.3"),
                 _BANK_HEAD, _bank_row("SBER", "6 340", "1 707", "3.7", "22.7%"))
    tables = _parse_tables(html)
    first_tickers = [r[2] for r in tables[0][1] if len(r) > 2]
    assert "LKOH" in first_tickers and "SBER" not in first_tickers


# ── Извлечение показателей ──────────────────────────────────────────────────

def test_extract_nonfinancial_row():
    cols, rows = _parse_tables(_page(
        _NONFIN_HEAD, _nonfin_row("GMKN", "1 992", "1 147", "164.1", "12.1", "2.3", "1.4")
    ))[0]
    out = _extract_row(cols, rows[0])
    assert out["pe_ratio"] == 12.1
    assert out["debt_ebitda"] == 1.4
    assert out["market_cap_bln_rub"] == 1992.0
    assert out["net_margin_pct"] == 14.3          # 164.1 / 1147 * 100
    assert out["roe_pct"] == 19.0                 # (P/BV)/(P/E) = 2.3/12.1


def test_extract_bank_row_uses_native_roe():
    cols, rows = _parse_tables(_page(_BANK_HEAD, _bank_row("SBER", "6 340", "1 707", "3.7", "22.7%")))[0]
    out = _extract_row(cols, rows[0])
    assert out["pe_ratio"] == 3.7
    assert out["roe_pct"] == 22.7                 # готовая колонка, не расчёт
    assert "debt_ebitda" not in out               # у банков колонки нет


def test_roe_not_derived_when_equity_negative():
    """
    Отрицательный P/BV (отрицательный капитал, как у X5/MTSS) формально даёт
    отрицательное частное, но это дыра в балансе, а не рентабельность —
    такое «ROE» утащило бы вниз медиану сектора. Поле не заполняем.
    """
    cols, rows = _parse_tables(_page(
        _NONFIN_HEAD, _nonfin_row("X5", "494", "4 000", "80", "5.2", "-0.5", "0.8")
    ))[0]
    assert "roe_pct" not in _extract_row(cols, rows[0])


def test_roe_not_derived_when_loss_making():
    """Убыток (P/E<0) — тождество тоже неприменимо."""
    cols, rows = _parse_tables(_page(
        _NONFIN_HEAD, _nonfin_row("MGNT", "170", "3 000", "-27", "-5.0", "1.2", "2.9")
    ))[0]
    assert "roe_pct" not in _extract_row(cols, rows[0])


def test_missing_cells_yield_partial_result():
    """Пустые ячейки не ломают строку — заполняем то, что есть."""
    cols, rows = _parse_tables(_page(
        _NONFIN_HEAD, _nonfin_row("SNGS", "878", "", "", "", "", "")
    ))[0]
    out = _extract_row(cols, rows[0])
    assert out == {"market_cap_bln_rub": 878.0}


def test_insane_values_rejected():
    """Значения вне диапазона правдоподобия (сдвиг колонок) отбрасываются."""
    cols, rows = _parse_tables(_page(
        _NONFIN_HEAD, _nonfin_row("XXXX", "100", "1 000", "50", "999999", "1.0", "9999")
    ))[0]
    out = _extract_row(cols, rows[0])
    assert "pe_ratio" not in out and "debt_ebitda" not in out


# ── Сетевой слой ────────────────────────────────────────────────────────────

def test_fetch_filters_to_requested_tickers(monkeypatch):
    import data.fundamentals_parser as fp

    html = _page(_NONFIN_HEAD,
                 _nonfin_row("GMKN", "1 992", "1 147", "164.1", "12.1", "2.3", "1.4"),
                 _nonfin_row("ZZZZ", "1", "1", "1", "1", "1", "1"))

    class _Resp:
        text = html
        def raise_for_status(self): pass

    monkeypatch.setattr(fp.requests, "get", lambda *a, **kw: _Resp())
    out = fetch_fundamentals_smartlab(["GMKN"])
    assert set(out) == {"GMKN"}


def test_fetch_network_failure_returns_empty(monkeypatch):
    import data.fundamentals_parser as fp

    def _boom(*a, **kw):
        raise OSError("нет сети")

    monkeypatch.setattr(fp.requests, "get", _boom)
    assert fetch_fundamentals_smartlab(["SBER"]) == {}


def test_fetch_garbage_markup_returns_empty(monkeypatch):
    import data.fundamentals_parser as fp

    class _Resp:
        text = "<html>совсем другая страница</html>"
        def raise_for_status(self): pass

    monkeypatch.setattr(fp.requests, "get", lambda *a, **kw: _Resp())
    assert fetch_fundamentals_smartlab(["SBER"]) == {}


# ── Файл авто-данных: троттлинг, возраст, слияние ───────────────────────────

def test_update_throttled_when_fresh(tmp_path, monkeypatch):
    import data.fundamentals_parser as fp

    f = tmp_path / "auto.json"
    f.write_text(json.dumps({"last_updated": today_msk().isoformat(), "tickers": {}}), encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)

    called = {"n": 0}
    def _spy(tickers):
        called["n"] += 1
        return {}
    monkeypatch.setattr(fp, "fetch_fundamentals_smartlab", _spy)

    assert update_fundamentals_auto(["SBER"]) is None
    assert called["n"] == 0        # до сети даже не дошли


def test_update_runs_when_stale(tmp_path, monkeypatch):
    import data.fundamentals_parser as fp

    f = tmp_path / "auto.json"
    old = (today_msk() - timedelta(days=30)).isoformat()
    f.write_text(json.dumps({"last_updated": old, "tickers": {}}), encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)
    monkeypatch.setattr(fp, "fetch_fundamentals_smartlab", lambda t: {"SBER": {"pe_ratio": 3.7}})

    result = update_fundamentals_auto(["SBER"])
    assert result is not None
    assert json.loads(f.read_text(encoding="utf-8"))["tickers"]["SBER"]["pe_ratio"] == 3.7


def test_update_keeps_old_file_when_source_fails(tmp_path, monkeypatch):
    """Источник упал — прежний файл остаётся нетронутым, а не затирается пустым."""
    import data.fundamentals_parser as fp

    f = tmp_path / "auto.json"
    old = (today_msk() - timedelta(days=30)).isoformat()
    f.write_text(json.dumps({"last_updated": old, "tickers": {"SBER": {"pe_ratio": 3.7}}}), encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)
    monkeypatch.setattr(fp, "fetch_fundamentals_smartlab", lambda t: {})

    assert update_fundamentals_auto(["SBER"]) is None
    assert json.loads(f.read_text(encoding="utf-8"))["tickers"]["SBER"]["pe_ratio"] == 3.7


def test_load_auto_stamps_last_updated_per_ticker(tmp_path, monkeypatch):
    """
    Дата парсинга лежит на уровне файла, но слиянию она нужна в каждой записи:
    иначе свежие авто-данные унаследуют старый last_updated ручного файла
    и попадут под is_fund_stale.
    """
    import data.fundamentals_parser as fp

    f = tmp_path / "auto.json"
    stamp = today_msk().isoformat()
    f.write_text(json.dumps({"last_updated": stamp, "tickers": {"SBER": {"pe_ratio": 3.7}}}), encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)

    loaded = load_auto_fundamentals()
    assert loaded["SBER"]["last_updated"] == stamp


def test_load_auto_ignores_ancient_file(tmp_path, monkeypatch):
    import data.fundamentals_parser as fp
    from config import FUNDAMENTALS_AUTO_MAX_AGE_DAYS

    f = tmp_path / "auto.json"
    ancient = (today_msk() - timedelta(days=FUNDAMENTALS_AUTO_MAX_AGE_DAYS * 4 + 1)).isoformat()
    f.write_text(json.dumps({"last_updated": ancient, "tickers": {"SBER": {"pe_ratio": 3.7}}}), encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)

    assert load_auto_fundamentals() == {}


def test_load_auto_corrupted_file(tmp_path, monkeypatch):
    import data.fundamentals_parser as fp

    f = tmp_path / "auto.json"
    f.write_text("{битый json", encoding="utf-8")
    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", f)
    assert load_auto_fundamentals() == {}


def test_load_auto_missing_file(tmp_path, monkeypatch):
    import data.fundamentals_parser as fp

    monkeypatch.setattr(fp, "FUNDAMENTALS_AUTO_FILE", tmp_path / "нет.json")
    assert load_auto_fundamentals() == {}
