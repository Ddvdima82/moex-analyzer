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

# Якоря в href заголовков колонок — устойчивы к смене вёрстки/локализации текста
# (сам видимый текст заголовков там же в другой кодировке и может съезжать).
_HEADER_MARKERS = {
    "order_by_ticker": "ticker",
    "order_by_dividend": "amount",
    # «Дата отсечки реестра» — фактическая дата отсечки (после неё гэп).
    # НЕ «Закр. РД»/T+2 (последний день покупки) и НЕ «Дата выплаты» —
    # у одной бумаги в строке до 3 дат, и после того как более ранние из них
    # (T+2, отсечка) уходят в прошлое, наивный «первая будущая дата в строке»
    # ошибочно подбирает дату ВЫПЛАТЫ и выдаёт её за ещё не прошедшую отсечку
    # (баг, живший в проде: 2026-07-17..2026-07-30, отчёт показывал SBER/VTBR
    # с «отсечкой» 30.07 — датой выплаты, хотя реальная отсечка была 20.07).
    "order_by_cut_off_date": "cutoff_date",
}


def _cell_text(cell_html: str) -> str:
    return _TAG_RE.sub("", cell_html).replace("&nbsp;", " ").strip()


def _header_column_map(html: str) -> dict[str, int]:
    """Индексы колонок по устойчивым href-якорям в <thead>. {} если не нашли."""
    thead = re.search(r"<thead[^>]*>(.*?)</thead>", html, flags=re.S)
    if not thead:
        return {}
    ths = re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), flags=re.S)
    col_map: dict[str, int] = {}
    for i, th in enumerate(ths):
        for marker, key in _HEADER_MARKERS.items():
            if marker in th:
                col_map[key] = i
    return col_map


def _parse_smartlab_html(html: str, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Разбирает HTML календаря по КОЛОНКАМ (не по «любой будущей дате в строке»):
    ticker/amount/cutoff_date — позиции ищутся через _header_column_map. Если
    ожидаемые колонки не найдены (сменилась вёрстка) — {} с warning, а не
    угаданный (и потенциально неверный) результат.
    """
    col_map = _header_column_map(html)
    if not {"ticker", "amount", "cutoff_date"}.issubset(col_map):
        logger.warning(
            "Smart-lab: не найдены ожидаемые колонки таблицы (сменилась вёрстка?) — %s",
            col_map,
        )
        return {}

    today_str = today_msk().strftime("%Y-%m-%d")
    wanted = set(tickers)
    result: dict[str, dict[str, Any]] = {}
    max_idx = max(col_map.values())

    # Страница разбита на несколько <tbody> (по секциям календаря) — берём все,
    # иначе re.search() по первому блоку молча теряет строки из остальных.
    tbodies = re.findall(r"<tbody[^>]*>(.*?)</tbody>", html, flags=re.S)
    body_html = "".join(tbodies) if tbodies else html

    for row_html in re.split(r"<tr[\s>]", body_html)[1:]:
        cells = [_cell_text(c) for c in re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.S
        )]
        if len(cells) <= max_idx:
            continue

        ticker = cells[col_map["ticker"]]
        if ticker not in wanted or ticker in result:
            continue

        cutoff_raw = cells[col_map["cutoff_date"]]
        m = _DATE_RE.search(cutoff_raw)
        if not m:
            continue
        d, mo, y = m.groups()
        cutoff_date = f"{y}-{mo}-{d}"
        if cutoff_date < today_str:
            continue  # отсечка уже прошла — не upcoming

        amount_raw = cells[col_map["amount"]].replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            amount = float(amount_raw)
        except ValueError:
            continue
        if amount <= 0:
            continue

        result[ticker] = {"ex_date": cutoff_date, "amount": amount}

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
