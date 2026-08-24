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
    assert res["n_independent_dates"] == 0
    assert res["reliable"] is False


# ── _cluster_adjusted_stats: не путать строки с независимыми наблюдениями ────

def test_cluster_adjusted_stats_counts_dates_not_rows():
    # 3 даты × 20 "тикеров" = 60 строк, но n_independent_dates должно быть 3
    resolved = [
        {"run_date": d, "fwd_return": 1.0}
        for d in ("2026-01-01", "2026-01-02", "2026-01-03")
        for _ in range(20)
    ]
    stats = backtest._cluster_adjusted_stats(resolved)
    assert stats["n_independent_dates"] == 3
    assert stats["reliable"] is False  # < RELIABLE_MIN_INDEPENDENT_DATES


def test_cluster_adjusted_stats_reliable_above_threshold():
    resolved = [
        {"run_date": f"2026-01-{d:02d}", "fwd_return": float(d)}
        for d in range(1, 20)
        for _ in range(5)
    ]
    stats = backtest._cluster_adjusted_stats(resolved)
    assert stats["n_independent_dates"] == 19
    assert stats["reliable"] is True
    assert stats["t_stat_by_date"] is not None


# ── walk_forward_weight_validation: honest OOS с embargo ─────────────────────

def test_walk_forward_weight_validation_insufficient_dates(tmp_path):
    from data import store
    store.save_run(
        [{"ticker": "SBER", "final_score": 80.0, "signal": "BUY",
          "scores": {"fundamental": 50, "technical": 50, "sentiment": 50}}],
        run_date="2026-01-10", db_path=tmp_path / "h.db",
    )
    hist = pd.DataFrame({
        "TRADEDATE": pd.to_datetime(["2026-01-10", "2026-02-10"]),
        "CLOSE": [100.0, 110.0],
    })
    res = backtest.walk_forward_weight_validation(
        horizon_days=28, db_path=tmp_path / "h.db", history_provider=lambda t: hist,
    )
    assert res["insufficient"] is True


def test_walk_forward_weight_validation_full_run(tmp_path):
    from data import store
    import random
    random.seed(11)

    db = tmp_path / "h.db"
    # 20 торговых дней подряд, technical идеально предсказывает доходность,
    # остальные столпы — шум. Достаточно для min_train_dates=10/min_test_dates=5
    # даже после embargo, т.к. цены разнесены далеко за горизонт.
    dates = pd.date_range("2026-01-01", periods=20, freq="D")
    hist_rows = []
    for i, d in enumerate(dates):
        # цена входа всегда 100, цена через 28д растёт с i → forward_return
        # монотонно растёт с i, как и technical-скор ниже (иначе их корреляция
        # зависит от знака (100+i+10)/(100+i)-1, который УБЫВАЕТ по i — ловушка)
        hist_rows.append({"TRADEDATE": d, "CLOSE": 100.0})
        hist_rows.append({"TRADEDATE": d + pd.Timedelta(days=28), "CLOSE": 100.0 + i * 2})
    hist = pd.DataFrame(hist_rows).drop_duplicates("TRADEDATE").sort_values("TRADEDATE")

    for i, d in enumerate(dates):
        tech = 30 + i * 3  # растёт со временем — коррелирует с ценой, тоже растущей
        store.save_run(
            [{"ticker": "SBER", "final_score": tech, "signal": "BUY",
              "scores": {"fundamental": random.uniform(0, 100), "technical": tech,
                         "sentiment": random.uniform(0, 100)}}],
            run_date=d.strftime("%Y-%m-%d"), db_path=db,
        )

    res = backtest.walk_forward_weight_validation(
        horizon_days=28, embargo_days=0, min_train_dates=10, min_test_dates=5,
        db_path=db, history_provider=lambda t: hist,
    )
    assert res["insufficient"] is False
    assert res["fitted_weights"]["technical"] > res["fitted_weights"]["fundamental"]
    assert abs(sum(res["fitted_weights"].values()) - 1.0) < 1e-6
    assert "test_fitted" in res and "test_default" in res
