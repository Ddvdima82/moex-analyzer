"""Тесты генератора дашборда (dashboard.py)."""
import dashboard
from data import store


def _result(ticker="SBER", score=80.0, signal="BUY"):
    return {
        "ticker": ticker, "company": "Сбербанк", "price": 300.0,
        "final_score": score, "signal": signal, "signal_emoji": "🟢",
        "target_price": 330.0, "upside_pct": 10.0,
        "scores": {"fundamental": 82, "technical": 75, "sentiment": 60},
        "indicators": {"rsi": 55.0, "macd_histogram": 1.2, "above_sma200": True},
        "fundamental": {"pe_ratio": 4.2, "div_yield_pct": 11.7, "roe_pct": 24.5, "sector": "banking"},
        "sentiment": {"overall": "positive", "key_event": "Сильный отчёт"},
    }


def test_latest_from_results_mapping():
    out = dashboard._latest_from_results([_result()])
    x = out[0]
    assert x["ticker"] == "SBER"
    assert x["f_score"] == 82 and x["t_score"] == 75 and x["s_score"] == 60
    assert x["rsi"] == 55.0 and x["above_sma200"] is True
    assert x["pe"] == 4.2 and x["sector"] == "banking"
    assert x["key_event"] == "Сильный отчёт"


def test_gather_single_run_no_backtest(tmp_path):
    db = tmp_path / "h.db"
    store.save_run([_result("SBER", 80.0, "BUY")], run_date="2026-06-26", db_path=db)
    data = dashboard.gather_dashboard_data([_result("SBER", 80.0, "BUY")], db_path=db)
    assert data["stats"]["total"] == 1
    assert data["stats"]["BUY"] == 1
    assert data["stats"]["runs"] == 1
    assert "SBER" in data["history"]
    assert len(data["timeline"]) == 1
    assert data["backtest"] is None              # один прогон → бэктест не считается


def test_gather_invokes_backtest_with_multiple_runs(tmp_path, monkeypatch):
    db = tmp_path / "h.db"
    store.save_run([_result("SBER", 80.0)], run_date="2026-06-12", db_path=db)
    store.save_run([_result("SBER", 70.0)], run_date="2026-06-19", db_path=db)

    fake = {"runs_evaluated": 5, "hit_rate": 60.0, "mean_return": 1.2,
            "horizon_days": 28, "by_signal": {"BUY": {"n": 5, "hit_rate": 60.0, "mean_return": 1.2}}}
    monkeypatch.setattr("backtest.evaluate_stored_runs", lambda **kw: fake)

    data = dashboard.gather_dashboard_data(None, db_path=db)
    assert data["backtest"] == fake
    assert data["stats"]["runs"] == 2


def test_build_dashboard_writes_html(tmp_path):
    db = tmp_path / "h.db"
    out = tmp_path / "index.html"
    store.save_run([_result("SBER"), _result("GAZP", 40.0, "HOLD")], run_date="2026-06-26", db_path=db)
    path = dashboard.build_dashboard([_result("SBER"), _result("GAZP", 40.0, "HOLD")],
                                     db_path=db, out_path=out)
    assert path == out and out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "SBER" in html
    assert "Chart" in html                       # подключён график


def test_render_escapes_script_injection(tmp_path):
    # key_event с </script> не должен разорвать встроенный <script>
    res = _result()
    res["sentiment"]["key_event"] = "взлом </script><script>alert(1)</script>"
    data = dashboard.gather_dashboard_data([res], db_path=tmp_path / "h.db")
    html = dashboard.render_html(data)
    # сырой закрывающий тег из данных экранирован
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html
