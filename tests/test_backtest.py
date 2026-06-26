"""Тесты бэктест-харнесса (backtest.py)."""
import pandas as pd

import backtest


def test_forward_returns():
    closes = pd.Series([100.0, 110.0, 121.0, 133.1])
    fwd = backtest.forward_returns(closes, horizon=1)
    assert round(fwd.iloc[0], 1) == 10.0          # 100→110
    assert round(fwd.iloc[1], 1) == 10.0          # 110→121
    assert pd.isna(fwd.iloc[-1])                  # нет будущего бара


def test_classify_hit():
    assert backtest.classify_hit("BUY", 5.0) is True
    assert backtest.classify_hit("BUY", -5.0) is False
    assert backtest.classify_hit("SELL", -5.0) is True
    assert backtest.classify_hit("SELL", 5.0) is False
    assert backtest.classify_hit("HOLD", 5.0) is None


def test_summarize_hit_rate():
    records = [("BUY", 5.0), ("BUY", -2.0), ("SELL", -3.0), ("HOLD", 1.0)]
    s = backtest._summarize(records)
    assert s["n"] == 4
    # BUY: 1 из 2 верных, SELL: 1 из 1 → общий hit-rate по BUY+SELL = 2/3
    assert s["hit_rate"] == 66.7
    assert s["by_signal"]["BUY"]["hit_rate"] == 50.0
    assert s["by_signal"]["SELL"]["hit_rate"] == 100.0
    assert "hit_rate" not in s["by_signal"]["HOLD"]   # HOLD исключён


def test_walk_forward_technical_partition_invariant():
    # Колебательный ряд → скор варьируется, бары распределяются по корзинам.
    # Проверяем инвариант: long + short + flat == число протестированных баров.
    import math
    closes = [100 + 10 * math.sin(i / 7.0) + i * 0.1 for i in range(320)]
    df = pd.DataFrame({"CLOSE": closes, "VOLUME": [1000 + (i % 50) for i in range(320)]})
    res = backtest.walk_forward_technical(df, horizon=10, warmup=200)
    assert res["bars_tested"] > 0
    assert res["long"]["n"] + res["short"]["n"] + res["flat"] == res["bars_tested"]
    assert isinstance(res["edge"], float)
    # Корзины с наблюдениями имеют валидный hit-rate в [0, 100]
    for b in ("long", "short"):
        if res[b]["n"]:
            assert 0.0 <= res[b]["hit_rate"] <= 100.0


def test_evaluate_stored_runs_with_mock_history(tmp_path):
    from data import store

    db = tmp_path / "h.db"
    # Прогон: BUY по SBER на дату 2026-01-10
    store.save_run(
        [{"ticker": "SBER", "company": "Сбербанк", "price": 100.0,
          "final_score": 80.0, "signal": "BUY", "target_price": 110.0,
          "upside_pct": 10.0, "scores": {}}],
        run_date="2026-01-10", db_path=db,
    )

    # История: цена выросла со 100 до 115 за месяц
    hist = pd.DataFrame({
        "TRADEDATE": pd.to_datetime(["2026-01-10", "2026-01-20", "2026-02-10"]),
        "CLOSE": [100.0, 105.0, 115.0],
    })

    res = backtest.evaluate_stored_runs(
        horizon_days=28, db_path=db, history_provider=lambda t: hist,
    )
    assert res["runs_evaluated"] == 1
    # BUY на росте → попадание, hit-rate 100%
    assert res["by_signal"]["BUY"]["hit_rate"] == 100.0
    assert res["mean_return"] > 0


def test_evaluate_stored_runs_empty(tmp_path):
    res = backtest.evaluate_stored_runs(db_path=tmp_path / "empty.db",
                                        history_provider=lambda t: pd.DataFrame())
    assert res["runs_evaluated"] == 0
