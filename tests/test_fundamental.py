"""Тесты фундаментального анализа и валидации (analysis/fundamental.py)."""
from datetime import date, timedelta

from analysis.fundamental import (
    _validate_entry,
    get_sector_medians,
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
