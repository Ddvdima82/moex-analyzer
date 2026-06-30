# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.11 MOEX stock-analysis pipeline. The main entry point is `main.py`, with configuration in `config.py`. Data access and persistence live in `data/` (`moex_api.py`, `store.py`, `fundamentals.json`). Analysis pillars live in `analysis/` for technical, fundamental, and sentiment scoring. Final scoring is in `scoring/`, report generation and Telegram delivery are in `report/`, and CLI backtesting is in `backtest.py`. Tests are in `tests/`. Runtime outputs are written to `reports/`, `logs/`, `docs/`, and SQLite databases; treat these as generated artifacts.

## Build, Test, and Development Commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the full weekly pipeline:

```bash
python main.py
```

Run tests:

```bash
pytest
pytest tests/test_scoring.py
```

Run analysis utilities:

```bash
python backtest.py --ticker SBER
python backtest.py --horizon 28
python dashboard.py
```

There is no separate build step or committed virtual environment.

## Coding Style & Naming Conventions

Use standard Python style with 4-space indentation, `snake_case` functions and variables, and uppercase constants in `config.py`. Keep comments and log messages in Russian, matching the existing codebase. Prefer pure functions for pipeline stages and keep module boundaries intact: data fetching in `data/`, scoring in `analysis/` and `scoring/`, output formatting in `report/`.

## Testing Guidelines

The project uses `pytest`. Test files follow `tests/test_*.py`, and tests should remain network-free by monkeypatching MOEX, Claude, and Telegram calls. Add focused tests when changing scoring formulas, config validation, persistence, report formatting, or fallback behavior. Run `pytest` before submitting changes.

## Commit & Pull Request Guidelines

Git history was not available in this checkout, so use clear, imperative commit messages such as `Fix neutral fallback for sentiment errors` or `Add dashboard trend test`. Pull requests should describe the user-visible change, list tests run, mention configuration or data-file updates, and include screenshots only when `docs/index.html` or dashboard output changes.

## Security & Configuration Tips

Secrets must come from environment variables or `.env`: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`. Do not commit `.env`, logs, reports, generated dashboards, or SQLite databases. `config.py` is the source of truth for tickers, weights, thresholds, retry settings, and model selection; validate weight sums and threshold ordering when editing it.

## Agent-Specific Instructions

Preserve failure isolation: one ticker or pillar failure should log a warning and fall back to neutral score `50`, not abort the pipeline. The dict produced by `build_stock_result()` is consumed by reports and dashboards, so update downstream code and tests if its keys change.
