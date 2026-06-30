"""
Ежедневная проверка изменений сигналов.
Запускается после main.py: сравнивает последние два прогона из SQLite.
При изменениях — отправляет Telegram-уведомление.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("delta_check")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from data.store import get_last_two_run_dates, load_run
from report.delta_report import build_delta_message
from report.telegram_bot import check_connection, send_report


def main() -> None:
    dates = get_last_two_run_dates()
    if len(dates) < 2:
        logger.info("Недостаточно прогонов для сравнения (найдено: %d)", len(dates))
        sys.exit(0)

    curr_date, prev_date = dates[0], dates[1]
    logger.info("Сравниваем: %s vs %s", curr_date, prev_date)

    curr_rows = load_run(curr_date)
    prev_rows = load_run(prev_date)

    msg = build_delta_message(prev_rows, curr_rows, prev_date, curr_date)
    if not msg:
        logger.info("Изменений сигналов нет: %s → %s", prev_date, curr_date)
        sys.exit(0)

    logger.info("Найдены изменения — отправляем в Telegram")
    print(msg)

    if check_connection():
        send_report(msg)
        logger.info("Отправлено в Telegram")
    else:
        logger.warning("Telegram недоступен — только лог")


if __name__ == "__main__":
    main()
