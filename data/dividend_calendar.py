"""
Фолбэк-календарь дивидендных отсечек со smart-lab.ru.

MOEX ISS в /securities/{ticker}/dividends.json отдаёт по сути исторические
выплаты — будущие объявленные отсечки появляются с большим лагом или не
появляются вовсе (прогон 2026-07-17: «Предстоящие дивиденды: 0 из 20» при
известных отсечках на неделе). Smart-lab ведёт полный календарь объявленных
дивидендов. Модуль используется как ФОЛБЭК: в main данные ISS приоритетны.

Разбор — регэкспами по HTML-таблице (без новых зависимостей). Структура
страницы может меняться; при любой ошибке возвращаем {} и warning —
пайплайн деградирует до поведения «календарь пуст», не падает.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

from config import REQUEST_TIMEOUT, today_msk

logger = logging.getLogger(__name__)

SMARTLAB_URL = "https://smart-lab.ru/dividends/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_NUM_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def _cell_text(cell_html: str) -> str:
    return _TAG_RE.sub("", cell_html).replace("&nbsp;", " ").strip()


def _parse_smartlab_html(html: str, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Разбирает HTML календаря: для каждого тикера из списка — ближайшая
    будущая отсечка. Отсечкой считаем ПЕРВУЮ будущую дату строки (на
    smart-lab это «последний день покупки»/T-2 — именно после него гэп).
    Суммой — первую числовую ячейку без «%» (колонка «дивиденд, руб» идёт
    раньше цены и доходности).
    """
    today_str = today_msk().strftime("%Y-%m-%d")
    wanted = set(tickers)
    result: dict[str, dict[str, Any]] = {}

    for row_html in re.split(r"<tr[\s>]", html)[1:]:
        cells = [_cell_text(c) for c in re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.S
        )]
        if not cells:
            continue

        ticker = next((c for c in cells if c in wanted), None)
        if ticker is None or ticker in result:
            continue

        dates = sorted(
            f"{y}-{m}-{d}" for d, m, y in _DATE_RE.findall(row_html)
        )
        ex_date = next((dt for dt in dates if dt >= today_str), None)
        if ex_date is None:
            continue

        amount = None
        for c in cells:
            if "%" in c:
                continue
            c_norm = c.replace(" ", "").replace("\xa0", "")
            if _NUM_RE.match(c_norm):
                amount = float(c_norm.replace(",", "."))
                break
        if amount is None or amount <= 0:
            continue

        result[ticker] = {"ex_date": ex_date, "amount": amount}

    return result


def get_upcoming_dividends_smartlab(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Ближайшие объявленные отсечки со smart-lab для списка тикеров.
    Возвращает {"SBER": {"ex_date": "2026-07-18", "amount": 34.84}, ...};
    {} при любой ошибке (сеть, смена вёрстки) — с warning в лог.
    """
    try:
        resp = requests.get(SMARTLAB_URL, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = _parse_smartlab_html(resp.text, tickers)
        logger.info("Smart-lab календарь: отсечки для %d из %d тикеров", len(parsed), len(tickers))
        return parsed
    except Exception as exc:
        logger.warning("Smart-lab календарь недоступен: %s", exc)
        return {}
