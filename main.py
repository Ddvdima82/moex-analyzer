"""
main.py — точка входа системы анализа акций Мосбиржи.

Запускает полный pipeline:
  1. Сбор котировок и исторических данных (MOEX ISS API)
  2. Технический анализ (RSI, MACD, SMA, Volume)
  3. Фундаментальный анализ (P/E, ROE, Долг, Дивиденды)
  4. Сентимент-анализ (Claude API + web_search)
  5. Финальный взвешенный скоринг → BUY / HOLD / SELL
  6. Генерация отчёта через Claude
  7. Отправка в Telegram

Запуск: python main.py
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# ──────────────────────────────────────────────────────────────
# Настройка логирования
# ──────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    """Настраивает логирование в файл и консоль."""
    from config import LOGS_DIR, today_msk

    log_file = LOGS_DIR / f"analysis_{today_msk().strftime('%Y%m%d')}.log"

    # На Windows консоль по умолчанию cp1251 → эмодзи/«→» в логах падают с
    # UnicodeEncodeError. Принудительно UTF-8 (на Linux/CI уже UTF-8 — no-op).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("main")


logger = _setup_logging()


# ──────────────────────────────────────────────────────────────
# Основной pipeline
# ──────────────────────────────────────────────────────────────

def _process_ticker(
    ticker: str,
    company_name: str,
    current_price: float,
    fundamentals: dict,
    sector_medians: dict,
) -> tuple[dict, dict]:
    """
    Обрабатывает один тикер (технич. + фундам. + сентимент → итог).
    Каждый из трёх столпов изолирован: ошибка → нейтральный скор, не падаем.
    Возвращает (результат, meta) — meta фиксирует, где сработал фолбэк,
    чтобы пайплайн собрал сводку и тихая деградация была видимой.
    Вызывается из пула потоков — читает только разделяемые данные, не пишет.
    """
    from data.moex_api import calc_div_yield, get_history
    from analysis.fundamental import score_fundamental
    from analysis.technical import _empty_indicators, compute_indicators, score_technical
    from analysis.sentiment import analyze_sentiment, score_sentiment
    from scoring.final_score import build_stock_result

    meta = {"tech_fallback": False, "fund_neutral": False, "sent_fallback": False}

    # 1. Технический анализ (история за 260 торговых дней)
    try:
        history_df = get_history(ticker, days=260)
        indicators = compute_indicators(history_df)
        tech_score = score_technical(indicators)
    except Exception as exc:
        logger.error("Ошибка техн. анализа %s: %s", ticker, exc)
        indicators = _empty_indicators()
        indicators["current_price"] = current_price
        tech_score = 50.0
        meta["tech_fallback"] = True

    # 2. Фундаментальный анализ
    try:
        fund_data = fundamentals.get(ticker, {}).copy()
        if fund_data:
            fund_data["div_yield_pct"] = calc_div_yield(ticker, current_price)
            fund_score = score_fundamental(fund_data, sector_medians)
        else:
            logger.warning("Нет фундаментальных данных для %s — нейтральный скор", ticker)
            fund_data = {}
            fund_score = 50.0
            meta["fund_neutral"] = True
    except Exception as exc:
        logger.error("Ошибка фундам. анализа %s: %s", ticker, exc)
        fund_data = {}
        fund_score = 50.0
        meta["fund_neutral"] = True

    # 3. Сентимент-анализ через Claude (web_search)
    try:
        sentiment_data = analyze_sentiment(ticker, company_name)
        sent_score = score_sentiment(sentiment_data)
        if sentiment_data.get("error"):
            meta["sent_fallback"] = True
    except Exception as exc:
        logger.error("Ошибка сентимента %s: %s", ticker, exc)
        sentiment_data = {"sentiment_score": 50, "overall_sentiment": "neutral",
                          "key_event": "Ошибка анализа"}
        sent_score = 50.0
        meta["sent_fallback"] = True

    result = build_stock_result(
        ticker=ticker,
        company_name=company_name,
        current_price=current_price,
        fundamental_score=fund_score,
        technical_score=tech_score,
        sentiment_score=sent_score,
        indicators=indicators,
        fundamental_data=fund_data,
        sentiment_data=sentiment_data,
    )
    logger.info(
        "%s: ИТОГ score=%.1f → %s | цель=%.2f (%.1f%%)",
        ticker, result["final_score"], result["signal"],
        result["target_price"], result["upside_pct"],
    )
    return result, meta


def run_pipeline() -> list[dict]:
    """
    Выполняет полный цикл анализа и возвращает список результатов
    по каждой акции, отсортированных по убыванию final_score.
    Тикеры обрабатываются параллельно (I/O-bound: история + Claude).
    """
    from config import COMPANY_NAMES, TICKER_MAX_WORKERS, TOP20_TICKERS
    from data.moex_api import get_current_quotes
    from analysis.fundamental import get_sector_medians, load_fundamentals

    # 1. Фундаментальные данные из JSON
    logger.info("=== ШАГ 1: Загрузка фундаментальных данных ===")
    fundamentals = load_fundamentals()
    sector_medians = get_sector_medians(fundamentals)
    logger.info("Загружено %d компаний, %d секторов", len(fundamentals), len(sector_medians))

    # 2. Текущие котировки (один батч-запрос)
    logger.info("=== ШАГ 2: Получение котировок MOEX ===")
    quotes = get_current_quotes(TOP20_TICKERS)
    logger.info("Котировки получены для %d из %d тикеров", len(quotes), len(TOP20_TICKERS))

    # 3. Список к обработке (только с валидной ценой)
    worklist = []
    skipped_no_price = 0
    for ticker in TOP20_TICKERS:
        price = quotes.get(ticker, {}).get("price", 0.0)
        if price <= 0:
            logger.warning("Нет цены для %s — пропускаем", ticker)
            skipped_no_price += 1
            continue
        worklist.append((ticker, COMPANY_NAMES.get(ticker, ticker), price))

    # 4. Параллельная обработка
    logger.info("=== ШАГ 3: Анализ %d тикеров (%d воркеров) ===", len(worklist), TICKER_MAX_WORKERS)
    results: list[dict] = []
    summary = {"tech_fallback": 0, "fund_neutral": 0, "sent_fallback": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=TICKER_MAX_WORKERS) as ex:
        futures = {
            ex.submit(_process_ticker, t, name, price, fundamentals, sector_medians): t
            for t, name, price in worklist
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                result, meta = fut.result()
            except Exception as exc:
                logger.error("Тикер %s упал в потоке: %s", ticker, exc, exc_info=True)
                summary["errors"] += 1
                continue
            results.append(result)
            for key in ("tech_fallback", "fund_neutral", "sent_fallback"):
                if meta[key]:
                    summary[key] += 1

    results.sort(key=lambda x: -x["final_score"])

    # 5. Сводка прогона — делает тихую деградацию видимой
    logger.info(
        "=== Pipeline завершён: обработано %d | пропущено(нет цены) %d | "
        "ошибок %d | фолбэк техн=%d фундам=%d сентимент=%d ===",
        len(results), skipped_no_price, summary["errors"],
        summary["tech_fallback"], summary["fund_neutral"], summary["sent_fallback"],
    )
    return results


# ──────────────────────────────────────────────────────────────
# Сохранение результатов
# ──────────────────────────────────────────────────────────────

def save_results(results: list[dict]) -> None:
    """Сохраняет JSON и MD-отчёт в reports/ и строки прогона в SQLite."""
    from config import REPORTS_DIR, today_msk
    from report.claude_report import format_full_table
    from data.store import save_run
    from dashboard import build_dashboard

    today = today_msk().strftime("%Y%m%d")

    # SQLite (история прогонов для бэктеста); ошибки внутри не валят пайплайн
    save_run(results)

    # HTML-дашборд (docs/index.html → GitHub Pages); тоже не валит пайплайн
    build_dashboard(results)

    # JSON с сырыми данными
    json_path = REPORTS_DIR / f"analysis_{today}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    logger.info("Сохранён JSON: %s", json_path)

    # Текстовый отчёт (таблица)
    md_path = REPORTS_DIR / f"analysis_{today}.md"
    table = format_full_table(results)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(table)
    logger.info("Сохранён MD: %s", md_path)


# ──────────────────────────────────────────────────────────────
# Главная функция
# ──────────────────────────────────────────────────────────────

def main() -> None:
    from config import today_msk, validate_config

    logger.info("========== ЗАПУСК АНАЛИЗА МОСБИРЖИ ==========")
    logger.info("Дата: %s (МСК)", today_msk().strftime("%d.%m.%Y"))

    # Валидация конфига: фатальная ошибка (битые веса/пороги) останавливает старт
    try:
        for warn in validate_config():
            logger.warning("Конфиг: %s", warn)
    except ValueError as exc:
        logger.critical("Некорректная конфигурация: %s", exc)
        sys.exit(1)

    from report.telegram_bot import check_connection, send_error, send_report
    from report.claude_report import format_full_table, generate_report

    # Проверяем Telegram
    tg_ok = check_connection()
    if not tg_ok:
        logger.warning("Telegram недоступен или не настроен — отчёт не будет отправлен")

    try:
        # Запускаем pipeline
        results = run_pipeline()

        if not results:
            msg = "Pipeline завершён, но нет данных ни по одной акции!"
            logger.error(msg)
            if tg_ok:
                send_error(msg)
            sys.exit(1)

        # Сохраняем файлы
        save_results(results)

        # Генерируем текстовый отчёт через Claude
        logger.info("=== ШАГ 7: Генерация отчёта через Claude ===")
        report_text = generate_report(results)

        # Таблица всех акций (добавляется отдельно)
        table_text = format_full_table(results)

        # Отправляем в Telegram
        if tg_ok:
            logger.info("=== ШАГ 8: Отправка в Telegram ===")
            send_report(report_text)
            send_report(table_text)
            logger.info("Отчёт успешно отправлен в Telegram")
        else:
            # Выводим в консоль если Telegram не настроен
            logger.info("=== ОТЧЁТ (консоль) ===\n%s\n%s", report_text, table_text)

        logger.info("========== АНАЛИЗ ЗАВЕРШЁН УСПЕШНО ==========")

    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        sys.exit(0)
    except Exception as exc:
        error_msg = f"Критическая ошибка pipeline:\n{traceback.format_exc()}"
        logger.critical(error_msg)
        if tg_ok:
            send_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
