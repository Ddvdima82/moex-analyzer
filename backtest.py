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
    from analysis.technical import compute_indicators, score_technical

    closes = df["CLOSE"].astype(float).reset_index(drop=True)
    n = len(df)
    long_rec: list[float] = []
    short_rec: list[float] = []
    flat = 0

    for i in range(warmup, n - horizon):
        window = df.iloc[: i + 1]
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


def evaluate_stored_runs(
    horizon_days: int = 28,
    db_path=None,
    history_provider: Callable[[str], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """
    Оценивает все прогоны из SQLite против форвардной доходности.
    Для каждой строки (дата, тикер, сигнал) берёт цену на дату прогона и через
    horizon_days календарных дней из истории MOEX и считает попадание.
    """
    from data.store import _connect

    if history_provider is None:
        from data.history_cache import get_history_cached
        history_provider = lambda t: get_history_cached(t, days=400)  # noqa: E731

    try:
        conn = _connect(db_path)
        conn.row_factory = __import__("sqlite3").Row
        rows = [dict(r) for r in conn.execute(
            "SELECT run_date, ticker, signal FROM runs ORDER BY run_date"
        ).fetchall()]
        conn.close()
    except Exception as exc:
        logger.error("Не удалось прочитать прогоны: %s", exc)
        return {"n": 0, "by_signal": {}, "hit_rate": 0.0, "mean_return": 0.0}

    records: list[tuple[str, float]] = []
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
        records.append((r["signal"], (p_out / p_in - 1.0) * 100.0))

    summary = _summarize(records)
    summary["horizon_days"] = horizon_days
    summary["runs_evaluated"] = len(records)
    return summary


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Бэктест сигналов MOEX-анализатора")
    parser.add_argument("--horizon", type=int, default=28, help="Горизонт в днях/барах")
    parser.add_argument("--ticker", type=str, help="Walk-forward технического столпа по тикеру")
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()

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
              f"{res['runs_evaluated']} наблюдений):")
        print(f"  Общий hit-rate: {res['hit_rate']}% | ср.доходность: {res['mean_return']}%")
        for sig in ("BUY", "SELL", "HOLD"):
            s = res["by_signal"][sig]
            print(f"  {sig:<5}: n={s['n']:>4} "
                  f"hit={s.get('hit_rate', '—')!s:>6} ср.дох={s['mean_return']:>6}%")


if __name__ == "__main__":
    main()
