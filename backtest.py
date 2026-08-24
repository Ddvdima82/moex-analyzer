"""
Бэктест-харнесс: главный вопрос — есть ли у сигналов предсказательная сила.

Два режима:

1. walk_forward_technical(df, ...) — оффлайн прогон технического столпа по
   историческим окнам одной бумаги. Не требует ни сети, ни сентимента (его
   нельзя восстановить задним числом). Меряет «эджа»: средняя форвардная
   доходность лонг-корзины (высокий техн.скор) минус шорт-корзины.

2. evaluate_stored_runs(...) — оценивает реальные прошлые прогоны из SQLite
   против реализованной форвардной доходности (нужна история MOEX). Работает,
   когда в БД накопятся прогоны.

Запуск: python backtest.py [--horizon N] [--ticker SBER]
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Базовые метрики
# ──────────────────────────────────────────────────────────────

def forward_returns(closes: pd.Series, horizon: int) -> pd.Series:
    """Форвардная доходность в % через `horizon` баров: close[t+h]/close[t]-1."""
    closes = closes.astype(float).reset_index(drop=True)
    return (closes.shift(-horizon) / closes - 1.0) * 100.0


def classify_hit(signal: str, fwd_return: float) -> bool | None:
    """
    Попадание сигнала. BUY верен при росте, SELL — при падении.
    HOLD исключается из hit-rate (возвращаем None).
    """
    if signal == "BUY":
        return fwd_return > 0
    if signal == "SELL":
        return fwd_return < 0
    return None


def _summarize(records: list[tuple[str, float]]) -> dict[str, Any]:
    """records = [(signal, fwd_return), ...] → агрегированные метрики."""
    df = pd.DataFrame(records, columns=["signal", "fwd"]) if records else \
        pd.DataFrame(columns=["signal", "fwd"])
    out: dict[str, Any] = {"n": len(df), "by_signal": {}}

    hits, total = 0, 0
    for sig in ("BUY", "SELL", "HOLD"):
        sub = df[df["signal"] == sig]
        entry = {"n": int(len(sub)), "mean_return": round(float(sub["fwd"].mean()), 2) if len(sub) else 0.0}
        if sig in ("BUY", "SELL") and len(sub):
            sub_hits = sum(bool(classify_hit(sig, r)) for r in sub["fwd"])
            entry["hit_rate"] = round(sub_hits / len(sub) * 100, 1)
            hits += sub_hits
            total += len(sub)
        out["by_signal"][sig] = entry

    out["hit_rate"] = round(hits / total * 100, 1) if total else 0.0
    out["mean_return"] = round(float(df["fwd"].mean()), 2) if len(df) else 0.0
    return out


# ──────────────────────────────────────────────────────────────
# Режим 1: walk-forward технического столпа (оффлайн)
# ──────────────────────────────────────────────────────────────

def walk_forward_technical(
    df: pd.DataFrame,
    horizon: int = 20,
    warmup: int = 200,
    long_th: float = 55.0,
    short_th: float = 45.0,
) -> dict[str, Any]:
    """
    Прогоняет технический скор по истории одной бумаги.
    На каждом баре i (после warmup) считает индикаторы по df[:i+1], скор и
    форвардную доходность через horizon баров. Классифицирует по порогам:
    скор >= long_th → лонг, <= short_th → шорт.

    Возвращает метрики и `edge` = mean_return(long) − mean_return(short):
    положительный edge → у технического скора есть предсказательная сила.
    """
    from analysis.technical import compute_indicators, score_technical, trim_price_gap

    closes = df["CLOSE"].astype(float).reset_index(drop=True)
    n = len(df)
    long_rec: list[float] = []
    short_rec: list[float] = []
    flat = 0

    for i in range(warmup, n - horizon):
        # тот же препроцессинг, что в проде (main._process_ticker): окно после
        # ценового разрыва — иначе edge меряется на данных, которых прод не видит
        window, _ = trim_price_gap(df.iloc[: i + 1])
        score = score_technical(compute_indicators(window))
        fwd = (closes.iloc[i + horizon] / closes.iloc[i] - 1.0) * 100.0
        if score >= long_th:
            long_rec.append(fwd)
        elif score <= short_th:
            short_rec.append(fwd)
        else:
            flat += 1

    def _bucket(rec: list[float], positive_good: bool) -> dict[str, Any]:
        if not rec:
            return {"n": 0, "mean_return": 0.0, "hit_rate": 0.0}
        s = pd.Series(rec)
        hits = (s > 0).sum() if positive_good else (s < 0).sum()
        return {
            "n": len(rec),
            "mean_return": round(float(s.mean()), 2),
            "hit_rate": round(hits / len(rec) * 100, 1),
        }

    long_b = _bucket(long_rec, positive_good=True)
    short_b = _bucket(short_rec, positive_good=False)
    edge = round(long_b["mean_return"] - short_b["mean_return"], 2)

    return {
        "horizon": horizon,
        "bars_tested": max(0, n - warmup - horizon),
        "long": long_b,
        "short": short_b,
        "flat": flat,
        "edge": edge,        # >0 → лонги обгоняют шорты, сигнал работает
    }


# ──────────────────────────────────────────────────────────────
# Режим 2: оценка реальных прошлых прогонов из SQLite
# ──────────────────────────────────────────────────────────────

def _close_on_or_after(df: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """Первая цена закрытия на дату day или позже."""
    sub = df[df["TRADEDATE"] >= day]
    if sub.empty:
        return None
    return float(sub.iloc[0]["CLOSE"])


def _load_resolved_runs(
    horizon_days: int = 28,
    db_path=None,
    history_provider: Callable[[str], pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """
    Прогоны из SQLite с реализованной форвардной доходностью и per-pillar
    скорами (для evaluate_stored_runs и calibration.py — общая точка чтения,
    чтобы не дублировать сопоставление дат/цен из истории MOEX).
    Возвращает [{"ticker", "run_date", "signal", "scores": {...}, "fwd_return"}].
    """
    import json
    from data.store import _connect

    if history_provider is None:
        from data.history_cache import get_history_cached
        history_provider = lambda t: get_history_cached(t, days=400)  # noqa: E731

    try:
        conn = _connect(db_path)
        conn.row_factory = __import__("sqlite3").Row
        rows = [dict(r) for r in conn.execute(
            "SELECT run_date, ticker, signal, scores_json FROM runs ORDER BY run_date"
        ).fetchall()]
        conn.close()
    except Exception as exc:
        logger.error("Не удалось прочитать прогоны: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    hist_cache: dict[str, pd.DataFrame] = {}

    for r in rows:
        ticker = r["ticker"]
        if ticker not in hist_cache:
            hist_cache[ticker] = history_provider(ticker)
        df = hist_cache[ticker]
        if df is None or df.empty or "TRADEDATE" not in df.columns:
            continue

        entry_day = pd.to_datetime(r["run_date"])
        exit_day = entry_day + pd.Timedelta(days=horizon_days)
        p_in = _close_on_or_after(df, entry_day)
        p_out = _close_on_or_after(df, exit_day)
        if not p_in or not p_out:
            continue

        try:
            scores = json.loads(r["scores_json"]) if r.get("scores_json") else {}
        except (ValueError, TypeError):
            scores = {}

        out.append({
            "ticker": ticker,
            "run_date": r["run_date"],
            "signal": r["signal"],
            "scores": scores,
            "fwd_return": (p_out / p_in - 1.0) * 100.0,
        })

    return out


RELIABLE_MIN_INDEPENDENT_DATES = 15


def _cluster_adjusted_stats(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Строки резолвленных прогонов группированы по дате: N тикеров одного дня
    делят общий рыночный режим этого дня, это не N независимых наблюдений
    (псевдо-репликация) — see calibration.MIN_OBSERVATION_DATES для того же
    диагноза на стороне калибровки весов. n_independent_dates — реальный
    размер выборки; mean_return_by_date/t_stat считаются по СРЕДНИМ ПО ДНЯМ
    (не по сырым строкам), это грубый, но честный cluster-robust расчёт.
    reliable=False ниже RELIABLE_MIN_INDEPENDENT_DATES — не прячем метрику,
    просто явно помечаем, что доверять ей рано.
    """
    by_date: dict[str, list[float]] = {}
    for r in resolved:
        by_date.setdefault(r["run_date"], []).append(r["fwd_return"])

    n_independent = len(by_date)
    per_date_means = pd.Series([sum(v) / len(v) for v in by_date.values()])
    mean = float(per_date_means.mean()) if n_independent else 0.0
    se = float(per_date_means.std(ddof=1) / (n_independent ** 0.5)) if n_independent > 1 else float("nan")
    t_stat = mean / se if se == se and se > 0 else float("nan")

    return {
        "n_independent_dates": n_independent,
        "mean_return_by_date": round(mean, 3),
        "se_by_date": round(se, 3) if se == se else None,
        "t_stat_by_date": round(t_stat, 2) if t_stat == t_stat else None,
        "reliable": n_independent >= RELIABLE_MIN_INDEPENDENT_DATES,
    }


def evaluate_stored_runs(
    horizon_days: int = 28,
    db_path=None,
    history_provider: Callable[[str], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """
    Оценивает все прогоны из SQLite против форвардной доходности.
    Для каждой строки (дата, тикер, сигнал) берёт цену на дату прогона и через
    horizon_days календарных дней из истории MOEX и считает попадание.
    Помимо наивных per-row метрик добавляет cluster-adjusted (по датам) —
    см. _cluster_adjusted_stats: с малым числом торговых дней per-row hit-rate
    выглядит уверенно на 100+ строк, будучи по факту 3-5 независимыми днями.
    """
    resolved = _load_resolved_runs(horizon_days, db_path, history_provider)
    records = [(r["signal"], r["fwd_return"]) for r in resolved]
    summary = _summarize(records)
    summary["horizon_days"] = horizon_days
    summary["runs_evaluated"] = len(records)
    summary.update(_cluster_adjusted_stats(resolved))
    return summary


# ──────────────────────────────────────────────────────────────
# Режим 3: honest walk-forward проверка самокалибровки весов (OOS)
# ──────────────────────────────────────────────────────────────

def walk_forward_weight_validation(
    horizon_days: int = 28,
    embargo_days: int | None = None,
    min_train_dates: int = 10,
    min_test_dates: int = 5,
    db_path=None,
    history_provider: Callable[[str], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """
    Отвечает на вопрос, который calibration.py сам себе не задаёт: помогли ли
    бы откалиброванные веса ЗАДНИМ ЧИСЛОМ, если фитить их только на прошлом и
    проверять на будущем — а не на всей накопленной истории разом (что и
    calibration._target_weights, и наивный evaluate_stored_runs делают
    in-sample). Режет накопленные прогоны на train/test ПО ДАТАМ (не строкам —
    см. _cluster_adjusted_stats), с embargo между ними: без него хвост
    train-периода и голова test-периода имеют перекрывающиеся forward-return
    окна (оба смотрят в одни и те же будущие цены) — классическая утечка,
    Блюпринт Этап 18/21.

    Фитит веса корреляцией на train (та же формула, что calibration.
    _target_weights, продублирована — импортировать calibration.py сюда нельзя,
    он сам импортирует _load_resolved_runs ОТСЮДА, взаимный импорт зациклится),
    сравнивает гипотетический сигнал калиброванных весов против ЗАФИКСИРОВАННЫХ
    дефолтных (config._DEFAULT_WEIGHTS — не текущего calibration.json, чтобы
    сравнение не зависело от того, что уже когда-то насчитано) на одном и том
    же test-сете.

    {"insufficient": True, ...} вместо результата — предпочитаем явно сказать
    "данных мало", чем выдать OOS-число на 2 независимых днях.
    """
    from config import SIGNAL_THRESHOLDS, _DEFAULT_WEIGHTS

    embargo_days = horizon_days if embargo_days is None else embargo_days
    pillars = tuple(_DEFAULT_WEIGHTS)

    resolved = _load_resolved_runs(horizon_days, db_path, history_provider)
    rows = [r for r in resolved if r.get("scores") and all(p in r["scores"] for p in pillars)]

    dates = sorted({r["run_date"] for r in rows})
    if len(dates) < min_train_dates + min_test_dates:
        return {
            "insufficient": True,
            "reason": "too few distinct trading days with resolved forward returns",
            "n_dates": len(dates), "needed": min_train_dates + min_test_dates,
        }

    split_idx = len(dates) - min_test_dates
    train_dates = set(dates[:split_idx])
    train_last = pd.Timestamp(dates[split_idx - 1])
    test_dates = {d for d in dates[split_idx:] if pd.Timestamp(d) >= train_last + pd.Timedelta(days=embargo_days)}

    if len(train_dates) < min_train_dates or len(test_dates) < min_test_dates:
        return {
            "insufficient": True,
            "reason": "too few independent days left after embargo purge",
            "n_train_dates": len(train_dates), "n_test_dates": len(test_dates),
            "needed_train": min_train_dates, "needed_test": min_test_dates, "embargo_days": embargo_days,
        }

    train_rows = [r for r in rows if r["run_date"] in train_dates]
    test_rows = [r for r in rows if r["run_date"] in test_dates]

    df_train = pd.DataFrame([{**r["scores"], "fwd_return": r["fwd_return"]} for r in train_rows])
    corr = {p: df_train[p].corr(df_train["fwd_return"]) for p in pillars}
    if any(pd.isna(v) for v in corr.values()):
        return {"insufficient": True, "reason": "degenerate train correlation (NaN)"}
    clamped = {p: max(0.0, v) for p, v in corr.items()}
    total = sum(clamped.values())
    fitted_weights = {p: clamped[p] / total for p in pillars} if total > 0 else dict(_DEFAULT_WEIGHTS)

    def _hypothetical_signal(scores: dict[str, float], weights: dict[str, float]) -> str:
        score = sum(scores[p] * weights[p] for p in weights)
        if score >= SIGNAL_THRESHOLDS["BUY"]:
            return "BUY"
        if score <= SIGNAL_THRESHOLDS["SELL"]:
            return "SELL"
        return "HOLD"

    fitted_records = [(_hypothetical_signal(r["scores"], fitted_weights), r["fwd_return"]) for r in test_rows]
    default_records = [(_hypothetical_signal(r["scores"], _DEFAULT_WEIGHTS), r["fwd_return"]) for r in test_rows]

    return {
        "insufficient": False,
        "n_train_dates": len(train_dates), "n_test_dates": len(test_dates), "embargo_days": embargo_days,
        "fitted_weights": {k: round(v, 3) for k, v in fitted_weights.items()},
        "test_fitted": _summarize(fitted_records),
        "test_default": _summarize(default_records),
    }


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Бэктест сигналов MOEX-анализатора")
    parser.add_argument("--horizon", type=int, default=28, help="Горизонт в днях/барах")
    parser.add_argument("--ticker", type=str, help="Walk-forward технического столпа по тикеру")
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--walk-forward-weights", action="store_true",
                         help="OOS-проверка калибровки весов (train/test по датам, с embargo)")
    args = parser.parse_args()

    if args.walk_forward_weights:
        res = walk_forward_weight_validation(horizon_days=args.horizon)
        if res.get("insufficient"):
            print(f"\nWalk-forward проверка калибровки: недостаточно данных ({res['reason']}).")
            print(f"  {res}")
            return
        print(f"\nWalk-forward проверка калибровки весов "
              f"(train={res['n_train_dates']} дн., test={res['n_test_dates']} дн., "
              f"embargo={res['embargo_days']} дн.):")
        print(f"  Веса, обученные на train: {res['fitted_weights']}")
        for label, key in (("калиброванные", "test_fitted"), ("дефолтные", "test_default")):
            s = res[key]
            print(f"  {label:<13}: n={s['n']:>4} hit-rate={s['hit_rate']:>5}% "
                  f"ср.доходность={s['mean_return']:>6}%")
        return

    if args.ticker:
        from data.history_cache import get_history_cached
        df = get_history_cached(args.ticker, days=600)
        if df.empty:
            print(f"Нет истории для {args.ticker}")
            return
        res = walk_forward_technical(df, horizon=args.horizon, warmup=args.warmup)
        print(f"\nWalk-forward технический скор — {args.ticker} (горизонт {args.horizon} баров):")
        print(f"  лонг : n={res['long']['n']:>4} hit={res['long']['hit_rate']:>5}% "
              f"ср.дох={res['long']['mean_return']:>6}%")
        print(f"  шорт : n={res['short']['n']:>4} hit={res['short']['hit_rate']:>5}% "
              f"ср.дох={res['short']['mean_return']:>6}%")
        print(f"  EDGE (лонг−шорт): {res['edge']}%  (>0 → сигнал работает)")
    else:
        res = evaluate_stored_runs(horizon_days=args.horizon)
        if res["runs_evaluated"] == 0:
            print("В БД нет прогонов с достаточной форвардной историей. "
                  "Накопите несколько еженедельных прогонов и повторите.")
            return
        print(f"\nОценка прогонов из БД (горизонт {args.horizon} дн., "
              f"{res['runs_evaluated']} наблюдений, {res['n_independent_dates']} независимых дней):")
        print(f"  Общий hit-rate: {res['hit_rate']}% | ср.доходность: {res['mean_return']}%")
        for sig in ("BUY", "SELL", "HOLD"):
            s = res["by_signal"][sig]
            print(f"  {sig:<5}: n={s['n']:>4} "
                  f"hit={s.get('hit_rate', '—')!s:>6} ср.дох={s['mean_return']:>6}%")
        print(f"  По дням: ср.доходность={res['mean_return_by_date']}% "
              f"se={res['se_by_date']} t-stat={res['t_stat_by_date']}")
        if not res["reliable"]:
            print(f"  [!] Только {res['n_independent_dates']} независимых торговых дней "
                  f"(нужно >= {RELIABLE_MIN_INDEPENDENT_DATES}) — статистике выше доверять рано, "
                  f"это шум малой выборки, а не подтверждённый эдж.")


if __name__ == "__main__":
    main()
