"""
Автообновление фундаментальных показателей со smart-lab.ru.

data/fundamentals.json ведётся руками и устаревает: между квартальными
отчётами P/E и ROE в нём расходятся с реальностью (на 2026-09-05 файл был
от 01.06, а у LKOH ручной P/E 5.1 против 37.8 у источника). Фундаментальный
столп при этом весит больше всех после самокалибровки, так что цена ошибки
высокая.

Источник — сводная таблица https://smart-lab.ru/q/shares_fundamental/. Она
разбита на ДВЕ таблицы с разным набором колонок: нефинансовые компании
(есть debt_ebitda, ebitda_margin, но НЕТ roe) и банки (есть roe, roa, NIM,
но нет debt_ebitda — у банков он неприменим). Колонки ищутся по устойчивым
якорям order_by_* в заголовках, как в data/dividend_calendar.py, а не по
позиции.

ROE для нефинансовых компаний в таблице отсутствует, но выводится точно:
ROE = E/BV = (P/BV) / (P/E) — цена сокращается, остаётся отношение прибыли
к капиталу. Для банков берётся готовая колонка roe.

Результат пишется в data/fundamentals_auto.json — генерируемый артефакт
класса data/calibration.json (гитигнорится, кэшируется в CI). Ручной
fundamentals.json остаётся фолбэком: слияние идёт ПОЛЕВОЕ, поэтому поля,
которых нет у источника (сектор, revenue_growth_yoy_pct), продолжают
браться из него. При любой ошибке возвращаем {} и пишем warning —
пайплайн деградирует до чисто ручных данных, не падает.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import requests

from config import (
    FUNDAMENTALS_AUTO_FILE,
    FUNDAMENTALS_AUTO_MAX_AGE_DAYS,
    REQUEST_TIMEOUT,
    today_msk,
)

logger = logging.getLogger(__name__)

SMARTLAB_FUNDAMENTALS_URL = "https://smart-lab.ru/q/shares_fundamental/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"order_by_([a-z_0-9]+)")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)

# Границы правдоподобия: за ними значение считаем мусором (сменилась вёрстка,
# сдвинулись колонки) и оставляем прежнее из ручного файла, а не портим скор.
_SANE_RANGES: dict[str, tuple[float, float]] = {
    "pe_ratio": (0.0, 1000.0),
    "roe_pct": (-200.0, 200.0),
    "debt_ebitda": (-20.0, 50.0),
    "net_margin_pct": (-500.0, 100.0),
    "market_cap_bln_rub": (0.0, 100_000.0),
}


def _cell_text(cell_html: str) -> str:
    return _TAG_RE.sub("", cell_html).replace("&nbsp;", " ").replace("\xa0", " ").strip()


def _num(raw: str) -> float | None:
    """«3 498» → 3498.0, «24%» → 24.0, «-0.3» → -0.3, «» / «-» / мусор → None."""
    if not raw:
        return None
    s = raw.replace("%", "").replace(" ", "").replace(",", ".")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_tables(html: str) -> list[tuple[dict[str, int], list[list[str]]]]:
    """
    [(колонка→индекс, строки-ячейки)] для каждой таблицы с order_by-заголовками.
    Строками таблицы считаем всё до следующего заголовочного <tr>.
    """
    header_positions: list[tuple[dict[str, int], int]] = []
    for m in _ROW_RE.finditer(html):
        block = m.group(1)
        if "order_by" not in block:
            continue
        cols: dict[str, int] = {}
        for idx, th in enumerate(_TH_RE.findall(block)):
            anchor = _ANCHOR_RE.search(th)
            if anchor:
                cols[anchor.group(1)] = idx
        if cols:
            header_positions.append((cols, m.end()))

    tables: list[tuple[dict[str, int], list[list[str]]]] = []
    for i, (cols, start) in enumerate(header_positions):
        end = header_positions[i + 1][1] if i + 1 < len(header_positions) else len(html)
        rows = [
            [_cell_text(c) for c in _TD_RE.findall(m.group(1))]
            for m in _ROW_RE.finditer(html[start:end])
        ]
        tables.append((cols, rows))
    return tables


def _extract_row(cols: dict[str, int], cells: list[str]) -> dict[str, float]:
    """Достаёт интересующие показатели из одной строки таблицы."""

    def val(key: str) -> float | None:
        idx = cols.get(key)
        return _num(cells[idx]) if idx is not None and idx < len(cells) else None

    out: dict[str, float] = {}

    pe = val("p_e")
    if pe is not None:
        out["pe_ratio"] = pe

    # ROE: у банков готовая колонка; у нефинансовых выводим как (P/BV)/(P/E) —
    # цена сокращается, остаётся прибыль/капитал. Тождество осмысленно только
    # при ПОЛОЖИТЕЛЬНЫХ P/E и P/BV: при убытке (P/E<0) или отрицательном
    # капитале (P/BV<0, как у X5 и MTSS) частное формально считается, но
    # означает уже не рентабельность, а дыру в балансе — такое «ROE» утащило
    # бы вниз медиану сектора. Тогда поле не заполняем, останется ручное.
    roe = val("roe")
    if roe is None:
        pbv = val("p_bv") if "p_bv" in cols else val("p_b")
        if pbv is not None and pbv > 0 and pe is not None and pe > 0:
            roe = pbv / pe * 100.0
    if roe is not None:
        out["roe_pct"] = round(roe, 1)

    debt = val("debt_ebitda")
    if debt is not None:
        out["debt_ebitda"] = debt

    revenue, net_income = val("revenue"), val("net_income")
    if revenue and net_income is not None and revenue > 0:
        out["net_margin_pct"] = round(net_income / revenue * 100.0, 1)

    cap = val("market_cap")
    if cap is not None:
        out["market_cap_bln_rub"] = cap

    return {
        k: v for k, v in out.items()
        if k not in _SANE_RANGES or _SANE_RANGES[k][0] <= v <= _SANE_RANGES[k][1]
    }


def fetch_fundamentals_smartlab(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """
    Свежие мультипликаторы по списку тикеров со smart-lab.
    {"SBER": {"pe_ratio": 3.7, "roe_pct": 24.5, ...}, ...}; {} при любой ошибке.
    """
    try:
        resp = requests.get(SMARTLAB_FUNDAMENTALS_URL, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("Smart-lab фундаментал недоступен: %s", exc)
        return {}

    try:
        wanted = set(tickers)
        result: dict[str, dict[str, Any]] = {}
        for cols, rows in _parse_tables(html):
            if "sec_id" not in cols:
                continue
            for cells in rows:
                idx = cols["sec_id"]
                if idx >= len(cells):
                    continue
                ticker = cells[idx]
                if ticker not in wanted or ticker in result:
                    continue
                fields = _extract_row(cols, cells)
                if fields:
                    result[ticker] = fields
        logger.info(
            "Smart-lab фундаментал: показатели для %d из %d тикеров", len(result), len(tickers)
        )
        return result
    except Exception as exc:
        logger.warning("Ошибка разбора фундаментала smart-lab: %s", exc)
        return {}


def _load_auto_file() -> dict[str, Any] | None:
    try:
        if FUNDAMENTALS_AUTO_FILE.exists():
            return json.loads(FUNDAMENTALS_AUTO_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("fundamentals_auto.json повреждён, игнорируем: %s", exc)
    return None


def update_fundamentals_auto(tickers: list[str], force: bool = False) -> dict[str, Any] | None:
    """
    Обновляет data/fundamentals_auto.json, если прошло достаточно времени.
    Отчётность выходит квартально, поэтому чаще раза в
    FUNDAMENTALS_AUTO_MAX_AGE_DAYS дней ходить к источнику незачем.
    Возвращает записанные данные либо None (пропуск/ошибка).
    """
    existing = _load_auto_file()
    if existing and not force:
        try:
            age = (today_msk() - date.fromisoformat(str(existing.get("last_updated", ""))[:10])).days
            if age < FUNDAMENTALS_AUTO_MAX_AGE_DAYS:
                logger.info(
                    "Фундаментал: авто-данным %d дн. (< %d) — обновление пропущено",
                    age, FUNDAMENTALS_AUTO_MAX_AGE_DAYS,
                )
                return None
        except (ValueError, TypeError):
            pass  # битая дата не должна блокировать обновление

    parsed = fetch_fundamentals_smartlab(tickers)
    if not parsed:
        return None

    payload = {"last_updated": today_msk().isoformat(), "tickers": parsed}
    try:
        FUNDAMENTALS_AUTO_FILE.parent.mkdir(parents=True, exist_ok=True)
        FUNDAMENTALS_AUTO_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Фундаментал обновлён автоматически: %d тикеров", len(parsed))
    except Exception as exc:
        logger.error("Не удалось сохранить fundamentals_auto.json: %s", exc)
        return None
    return payload


def load_auto_fundamentals() -> dict[str, dict[str, Any]]:
    """
    Авто-показатели для слияния с ручным файлом. {} если файла нет, он битый
    или устарел настолько, что доверять ему уже нельзя.
    """
    data = _load_auto_file()
    if not data:
        return {}
    raw_date = str(data.get("last_updated", ""))[:10]
    try:
        age = (today_msk() - date.fromisoformat(raw_date)).days
    except (ValueError, TypeError):
        return {}
    # Двойной запас по возрасту: обновление раз в N дней, а негодным считаем
    # только вчетверо более старое — иначе неделя недоступности источника
    # обнулила бы весь авто-слой разом.
    if age > FUNDAMENTALS_AUTO_MAX_AGE_DAYS * 4:
        logger.warning("Авто-фундаментал устарел (%d дн.) — игнорируем", age)
        return {}
    tickers = data.get("tickers")
    if not isinstance(tickers, dict):
        return {}
    # Дата парсинга хранится один раз на файл, но потребителю (слияние в
    # analysis.fundamental) она нужна в каждой записи: без неё свежие
    # авто-данные унаследовали бы старый last_updated ручного файла и
    # попали бы под is_fund_stale.
    return {
        ticker: {**fields, "last_updated": raw_date}
        for ticker, fields in tickers.items()
        if isinstance(fields, dict) and fields
    }


if __name__ == "__main__":
    from config import TOP20_TICKERS

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = update_fundamentals_auto(TOP20_TICKERS, force=True)
    if result is None:
        print("Обновление не выполнено.")
    else:
        for t, fields in result["tickers"].items():
            print(f"{t}: {fields}")
