"""Тесты оркестрации run_pipeline (параллелизм, сводка, пропуск без цены)."""
import analysis.fundamental as fundamental_mod
import analysis.sentiment as sentiment_mod
import data.moex_api as moex_api
import main


def test_process_ticker_sentiment_fallback_flag(monkeypatch):
    """meta.sent_fallback выставляется, когда сентимент вернул error."""
    import analysis.sentiment as sent

    monkeypatch.setattr(moex_api, "get_history", lambda t, days=260: __import__("pandas").DataFrame())
    monkeypatch.setattr(moex_api, "calc_div_yield", lambda t, p: 5.0)
    monkeypatch.setattr(sent, "analyze_sentiment", lambda t, n: {"sentiment_score": 50, "error": "нет ключа"})

    result, meta = main._process_ticker("SBER", "Сбербанк", 300.0, {}, {})
    assert meta["sent_fallback"] is True
    assert meta["fund_neutral"] is True          # пустой fundamentals
    assert result["ticker"] == "SBER"
    assert 0.0 <= result["final_score"] <= 100.0


def test_run_pipeline_orchestration(monkeypatch):
    # Котировки: цена есть только у SBER и GAZP, у остальных тикеров нет → пропуск
    monkeypatch.setattr(
        moex_api, "get_current_quotes",
        lambda tickers: {"SBER": {"price": 300.0}, "GAZP": {"price": 130.0}},
    )
    monkeypatch.setattr(fundamental_mod, "load_fundamentals", lambda: {})
    monkeypatch.setattr(fundamental_mod, "get_sector_medians", lambda f: {})
    monkeypatch.setattr(moex_api, "get_upcoming_dividends", lambda tickers: {})
    monkeypatch.setattr(sentiment_mod, "batch_analyze_sentiment", lambda pairs: None)

    def fake_process(ticker, name, price, funds, medians, cbr_rate=None, upcoming_div=None):
        result = {
            "ticker": ticker, "company": name, "price": price,
            "final_score": 90.0 if ticker == "SBER" else 40.0,
            "signal": "BUY", "target_price": price, "upside_pct": 0.0,
        }
        meta = {"tech_fallback": False, "fund_neutral": True, "sent_fallback": False}
        return result, meta

    monkeypatch.setattr(main, "_process_ticker", fake_process)
    import data.macro as macro_mod
    monkeypatch.setattr(macro_mod, "fetch_macro", lambda: {})

    results, macro = main.run_pipeline()
    assert [r["ticker"] for r in results] == ["SBER", "GAZP"]   # отсортировано по баллу
    assert len(results) == 2


def test_run_pipeline_handles_worker_exception(monkeypatch):
    monkeypatch.setattr(
        moex_api, "get_current_quotes",
        lambda tickers: {"SBER": {"price": 300.0}, "GAZP": {"price": 130.0}},
    )
    monkeypatch.setattr(fundamental_mod, "load_fundamentals", lambda: {})
    monkeypatch.setattr(fundamental_mod, "get_sector_medians", lambda f: {})
    monkeypatch.setattr(moex_api, "get_upcoming_dividends", lambda tickers: {})
    monkeypatch.setattr(sentiment_mod, "batch_analyze_sentiment", lambda pairs: None)

    def fake_process(ticker, name, price, funds, medians, cbr_rate=None, upcoming_div=None):
        if ticker == "GAZP":
            raise RuntimeError("сбой потока")
        result = {"ticker": ticker, "company": name, "price": price,
                  "final_score": 90.0, "signal": "BUY", "target_price": price, "upside_pct": 0.0}
        return result, {"tech_fallback": False, "fund_neutral": False, "sent_fallback": False}

    monkeypatch.setattr(main, "_process_ticker", fake_process)
    import data.macro as macro_mod
    monkeypatch.setattr(macro_mod, "fetch_macro", lambda: {})

    # Падение одного тикера не валит весь прогон
    results, macro = main.run_pipeline()
    assert [r["ticker"] for r in results] == ["SBER"]
