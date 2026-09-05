"""Тесты фундаментального анализа и валидации (analysis/fundamental.py)."""
from datetime import date, timedelta

from config import today_msk

from analysis.fundamental import (
    _validate_entry,
    fund_age_days,
    get_sector_medians,
    is_fund_stale,
    score_fundamental,
)


def _good_entry(**over):
    base = {
        "pe_ratio": 5.0,
        "debt_ebitda": 0.5,
        "roe_pct": 20.0,
        "net_margin_pct": 25.0,
        "revenue_growth_yoy_pct": 12.0,
        "sector": "banking",
        "last_updated": date.today().strftime("%Y-%m-%d"),
    }
    base.update(over)
    return base


def test_validate_entry_ok():
    assert _validate_entry("SBER", _good_entry()) is True


def test_validate_entry_missing_numeric():
    e = _good_entry()
    del e["pe_ratio"]
    assert _validate_entry("X", e) is False


def test_validate_entry_non_dict():
    assert _validate_entry("X", "not-a-dict") is False
    assert _validate_entry("X", None) is False


def test_validate_entry_stale_still_valid(caplog):
    # Устаревшие данные валидны для скоринга, но логируют предупреждение
    old = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    assert _validate_entry("X", _good_entry(last_updated=old)) is True


# ── Жёсткое устаревание: исключение столпа из финального скора ──────────────

def test_is_fund_stale_fresh_data():
    assert is_fund_stale(_good_entry()) is False


def test_is_fund_stale_old_data():
    old = (date.today() - timedelta(days=300)).strftime("%Y-%m-%d")
    assert is_fund_stale(_good_entry(last_updated=old)) is True


def test_is_fund_stale_missing_date_conservative():
    """Нет last_updated → свежесть недоказуема → считается устаревшим."""
    e = _good_entry()
    del e["last_updated"]
    assert is_fund_stale(e) is True


def test_is_fund_stale_broken_date_conservative():
    assert is_fund_stale(_good_entry(last_updated="июнь 2026")) is True


def test_fund_age_days():
    e = _good_entry(last_updated=(date.today() - timedelta(days=10)).strftime("%Y-%m-%d"))
    age = fund_age_days(e)
    # МСК-дата может отличаться от локальной на ±1 день у полуночи
    assert age is not None and 9 <= age <= 11
    assert fund_age_days({}) is None


def test_sector_medians():
    funds = {
        "A": {"sector": "banking", "pe_ratio": 4.0, "roe_pct": 20.0},
        "B": {"sector": "banking", "pe_ratio": 6.0, "roe_pct": 24.0},
    }
    med = get_sector_medians(funds)
    assert med["banking"]["pe"] == 5.0
    assert med["banking"]["roe"] == 22.0


def test_score_fundamental_bounds():
    medians = {"banking": {"pe": 5.0, "roe": 20.0}}
    score = score_fundamental(_good_entry(div_yield_pct=8.0), medians)
    assert 0.0 <= score <= 100.0
    # Плохие метрики дают балл не выше хороших
    bad = _good_entry(pe_ratio=50.0, debt_ebitda=5.0, roe_pct=1.0,
                      net_margin_pct=1.0, revenue_growth_yoy_pct=-10.0, div_yield_pct=0.0)
    good = _good_entry(div_yield_pct=12.0)
    assert score_fundamental(bad, medians) <= score_fundamental(good, medians)


# ── P/E отрицательный и нулевой (регресс на исправление #1) ──────────────────

def test_pe_negative_gives_zero_contribution():
    """Убыточная компания (P/E < 0) не получает бонус за PE."""
    medians = {"banking": {"pe": 8.0, "roe": 15.0}}
    score_neg = score_fundamental(_good_entry(pe_ratio=-5.0, div_yield_pct=0.0), medians)
    score_pos = score_fundamental(_good_entry(pe_ratio=4.0, div_yield_pct=0.0), medians)
    assert score_neg < score_pos


def test_pe_none_gives_zero_contribution():
    """Нет данных P/E → вклад PE = 0, не использует fallback 8.0."""
    medians = {"banking": {"pe": 8.0, "roe": 15.0}}
    entry_none = _good_entry(div_yield_pct=0.0)
    entry_none["pe_ratio"] = None
    entry_good = _good_entry(pe_ratio=4.0, div_yield_pct=0.0)
    assert score_fundamental(entry_none, medians) < score_fundamental(entry_good, medians)


def test_pe_zero_gives_zero_contribution():
    medians = {"banking": {"pe": 8.0, "roe": 15.0}}
    entry = _good_entry(pe_ratio=0.0, div_yield_pct=0.0)
    entry_good = _good_entry(pe_ratio=4.0, div_yield_pct=0.0)
    assert score_fundamental(entry, medians) < score_fundamental(entry_good, medians)


def test_negative_roe_clamped_to_zero():
    """Отрицательный ROE не даёт отрицательного вклада — просто 0."""
    medians = {"banking": {"pe": 8.0, "roe": 15.0}}
    s = score_fundamental(_good_entry(roe_pct=-30.0, div_yield_pct=0.0), medians)
    assert s >= 0.0


# ── Слияние авто-фундаментала с ручным файлом ────────────────────────────────

def test_merge_auto_overrides_only_present_fields(monkeypatch):
    """Авто-слой перекрывает свои поля, ручные (сектор, рост выручки) остаются."""
    import analysis.fundamental as fund

    manual = {"SBER": {"pe_ratio": 4.2, "roe_pct": 24.5, "debt_ebitda": 0.0,
                       "net_margin_pct": 41.0, "revenue_growth_yoy_pct": 12.3,
                       "sector": "banking", "last_updated": "2026-06-01"}}
    monkeypatch.setattr(
        "data.fundamentals_parser.load_auto_fundamentals",
        lambda: {"SBER": {"pe_ratio": 3.7, "roe_pct": 22.7, "last_updated": "2026-09-05"}},
    )
    merged = fund._merge_auto_fundamentals(manual)["SBER"]

    assert merged["pe_ratio"] == 3.7                    # перекрыто свежим
    assert merged["roe_pct"] == 22.7                    # перекрыто свежим
    assert merged["revenue_growth_yoy_pct"] == 12.3     # у парсера нет — ручное
    assert merged["sector"] == "banking"                # у парсера нет — ручное
    assert merged["last_updated"] == "2026-09-05"       # свежесть подтянулась


def test_merge_auto_ticker_without_fresh_data_untouched(monkeypatch):
    import analysis.fundamental as fund

    manual = {"SNGS": {"pe_ratio": 2.8, "sector": "oil_gas", "last_updated": "2026-06-01"}}
    monkeypatch.setattr(
        "data.fundamentals_parser.load_auto_fundamentals",
        lambda: {"SBER": {"pe_ratio": 3.7}},
    )
    assert fund._merge_auto_fundamentals(manual)["SNGS"]["pe_ratio"] == 2.8


def test_merge_auto_empty_layer_returns_manual(monkeypatch):
    import analysis.fundamental as fund

    manual = {"SBER": {"pe_ratio": 4.2, "sector": "banking"}}
    monkeypatch.setattr("data.fundamentals_parser.load_auto_fundamentals", lambda: {})
    assert fund._merge_auto_fundamentals(manual) == manual


def test_merge_auto_parser_failure_falls_back_to_manual(monkeypatch):
    """Падение авто-слоя не должно ронять загрузку фундаментала."""
    import analysis.fundamental as fund

    def _boom():
        raise RuntimeError("парсер сломался")

    manual = {"SBER": {"pe_ratio": 4.2, "sector": "banking"}}
    monkeypatch.setattr("data.fundamentals_parser.load_auto_fundamentals", _boom)
    assert fund._merge_auto_fundamentals(manual) == manual


def test_merge_auto_refreshes_staleness(monkeypatch):
    """Свежие авто-данные снимают признак устаревания со старого ручного файла."""
    import analysis.fundamental as fund

    manual = {"SBER": {"pe_ratio": 4.2, "sector": "banking", "last_updated": "2024-01-01"}}
    assert fund.is_fund_stale(manual["SBER"]) is True

    monkeypatch.setattr(
        "data.fundamentals_parser.load_auto_fundamentals",
        lambda: {"SBER": {"pe_ratio": 3.7, "last_updated": today_msk().isoformat()}},
    )
    merged = fund._merge_auto_fundamentals(manual)["SBER"]
    assert fund.is_fund_stale(merged) is False
