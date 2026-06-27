# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Weekly stock-analysis pipeline for the top-20 Moscow Exchange (MOEX) tickers. Pulls quotes/history from the MOEX ISS API, scores each stock on three pillars (fundamental, technical, sentiment), emits BUY/HOLD/SELL signals with a 4-week target price, generates a Russian-language report via the Claude API, and pushes it to Telegram. Runs unattended every Monday via GitHub Actions.

Code comments and log messages are in Russian — keep that convention when editing.

## Commands

```bash
pip install -r requirements.txt   # deps: requests, pandas, numpy, anthropic (+ pytest for tests)
python main.py                    # run full pipeline (entry point)
pytest                            # run unit tests (tests/)
pytest tests/test_scoring.py::test_target_price   # run a single test
python backtest.py --ticker SBER  # walk-forward backtest of the technical pillar
python backtest.py --horizon 28   # evaluate stored runs vs realized forward returns
python dashboard.py               # rebuild docs/index.html from stored runs
```

Tests live in [tests/](tests/) and are network-free (MOEX/Claude calls are monkeypatched). No linter or build step. Python 3.11 (matches CI). No virtualenv committed.

To run a single stage in isolation, import its module functions in a REPL — every stage is a pure function taking plain dicts/DataFrames (see signatures in `analysis/`, `scoring/`). The pipeline degrades gracefully: missing API keys or failed network calls log a warning and fall back to a neutral score (50) rather than crashing.

## Configuration

All secrets come from env vars (`.env` locally, GitHub Secrets in CI): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Without them the pipeline still runs — sentiment/report fall back, and the report prints to console instead of Telegram.

[config.py](config.py) is the single source of truth for tunables: the `TOP20_TICKERS` list and `COMPANY_NAMES` map (update quarterly), pillar `WEIGHTS` (must sum to 1.0), `SIGNAL_THRESHOLDS`, MOEX board/retry settings, and `CLAUDE_MODEL`. It auto-creates `reports/` and `logs/` on import.

## Architecture

Pipeline orchestrated by [main.py](main.py) `run_pipeline()`: fetches fundamentals + a batch quotes call, then fans out per-ticker work across a `ThreadPoolExecutor` (`TICKER_MAX_WORKERS`, I/O-bound: history + Claude). Each ticker is handled by `_process_ticker`, which runs the three pillars and returns `(result, meta)`; `meta` feeds an end-of-run summary line (skipped/errors/fallbacks) so silent degradation is visible. Per ticker:

1. **Data** ([data/moex_api.py](data/moex_api.py)) — MOEX ISS API client. All HTTP goes through `_get()` (3 retries × 5s, returns `None` on failure, never raises). Functions: `get_current_quotes` (parses the `marketdata` section), `get_history` (returns a `pd.DataFrame`), `get_dividends`/`calc_div_yield`, `get_index_composition`.
2. **Technical** ([analysis/technical.py](analysis/technical.py)) — RSI/MACD/SMA/volume/volatility computed in pure pandas+numpy (no TA libs). `compute_indicators(df)` → dict; `score_technical(dict)` → 0–100. Insufficient data returns neutral defaults via `_empty_indicators()`.
3. **Fundamental** ([analysis/fundamental.py](analysis/fundamental.py)) — reads static [data/fundamentals.json](data/fundamentals.json) (P/E, debt/EBITDA, ROE, margins, sector; manually maintained, `last_updated` field). `score_fundamental` grades each metric **relative to sector medians** computed by `get_sector_medians`. Dividend yield is injected at runtime from the MOEX API, not the JSON.
4. **Sentiment** ([analysis/sentiment.py](analysis/sentiment.py)) — Claude API with the `web_search` beta tool to find last-7-days news. Falls back to a no-search Claude call, then to neutral-50 on any error. Parses the first `{...}` JSON block out of the response.
5. **Scoring** ([scoring/final_score.py](scoring/final_score.py)) — `build_stock_result()` assembles the final per-stock dict: weighted `compute_final_score`, `get_signal` (BUY≥70 / SELL≤30 / HOLD), and `get_target_price` (linear: score 0→−15%, 50→0%, 100→+15%).
6. **Report** ([report/claude_report.py](report/claude_report.py)) — `generate_report` writes a Telegram-HTML analyst summary via Claude, with a programmatic `_fallback_report`. `format_full_table` renders the all-stocks table separately.
7. **Delivery** ([report/telegram_bot.py](report/telegram_bot.py)) — splits messages at the 4096-char Telegram limit; `parse_mode=HTML` (only `<b>/<i>/<code>`, no markdown, no `<br>`).

`main.py` `save_results()` writes `reports/analysis_YYYYMMDD.{json,md}`, persists per-ticker run rows to SQLite via [data/store.py](data/store.py) (`save_run`, idempotent per date — for backtest), builds a self-contained HTML dashboard to `docs/index.html` via [dashboard.py](dashboard.py) (`build_dashboard` — latest run + SQLite trend history + backtest, embedded JSON + Chart.js, no server), and logs go to `logs/analysis_YYYYMMDD.log`. CI ([.github/workflows/weekly_analysis.yml](.github/workflows/weekly_analysis.yml)) caches `data/history.db` across runs (so trends/backtest accrue) and publishes `docs/` to GitHub Pages. `docs/` and `*.db` are gitignored (build artifacts).

**Startup validation.** `main()` calls `config.validate_config()` first — a `ValueError` on broken `WEIGHTS` (must be non-negative and sum to 1.0) or `SIGNAL_THRESHOLDS` (SELL < BUY) aborts the run before producing wrong signals; missing API keys are non-fatal warnings. Run dates use `config.today_msk()` (UTC+3), since CI runs in UTC.

**Backtest** ([backtest.py](backtest.py), standalone CLI). Two modes: `walk_forward_technical(df, ...)` replays the technical pillar over historical price windows offline (no network, no sentiment — which can't be reconstructed historically) and reports a long-vs-short `edge`; `evaluate_stored_runs(...)` scores past runs from SQLite against realized forward returns once history accrues. This is the harness that answers whether the signals have predictive value — calibrate `WEIGHTS`/`SIGNAL_THRESHOLDS` from its output, not by guesswork.

### Conventions that matter

- **Every stage is failure-isolated.** Each pillar in `_process_ticker` is wrapped in try/except; on error it logs, sets a `meta` fallback flag, and substitutes a neutral score (50) so one bad ticker never aborts the run. A whole-ticker crash is caught in the `run_pipeline` future loop. Preserve this — don't let exceptions propagate out of a stage.
- The dict shape produced by `build_stock_result` is the contract consumed by both report functions. Changing keys there means updating [report/claude_report.py](report/claude_report.py).
- Scores are always clamped to [0, 100]; signals/targets derive from the final score, not the individual pillars.

## CI

[.github/workflows/weekly_analysis.yml](.github/workflows/weekly_analysis.yml): cron `0 6 * * 1` (09:00 MSK Monday) + manual `workflow_dispatch`. 15-min timeout. Uploads `logs/` on failure, `reports/` on success.
