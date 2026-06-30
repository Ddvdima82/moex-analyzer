"""
Макроэкономические данные для контекста анализа.

Источники:
  • MOEX ISS  — IMOEX, RGBI, USD/RUB, CNY/RUB (тот же API что котировки)
  • cbr.ru    — ключевая ставка ЦБ РФ (SOAP, публичный)
  • Yahoo Finance — Brent crude (BZ=F)

Каждая функция возвращает float или None при недоступности.
fetch_macro() собирает всё параллельно.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

logger = logging.getLogger(__name__)
_TIMEOUT = 12


def _get(url: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers or {"User-Agent": "moex-analyzer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read()
    except Exception as exc:
        logger.debug("macro _get %s: %s", url, exc)
        return None


# ──────────────────────────────────────────────────────────────
# MOEX ISS
# ──────────────────────────────────────────────────────────────

def _moex_index(secid: str) -> float | None:
    """Текущее значение индекса (IMOEX, RGBI) из MOEX ISS."""
    url = (
        f"https://iss.moex.com/iss/engines/stock/markets/index"
        f"/securities/{secid}.json?iss.only=marketdata&iss.meta=off"
    )
    raw = _get(url)
    if raw is None:
        return None
    try:
        md = json.loads(raw).get("marketdata", {})
        cols = md.get("columns", [])
        rows = md.get("data", [])
        for col in ("CURRENTVALUE", "LAST", "VALUE"):
            if col in cols:
                idx = cols.index(col)
                for row in rows:
                    v = row[idx]
                    if v is not None:
                        return round(float(v), 2)
    except Exception as exc:
        logger.debug("moex_index %s: %s", secid, exc)
    return None


def _moex_currency(secid: str) -> float | None:
    """Курс валюты из MOEX ISS. LAST если торги открыты, иначе PREVWAPRICE из securities."""
    base = (
        f"https://iss.moex.com/iss/engines/currency/markets/selt"
        f"/boards/CETS/securities/{secid}.json"
    )
    # Пробуем live-цену
    raw = _get(base + "?iss.only=marketdata&iss.meta=off")
    if raw is not None:
        try:
            md = json.loads(raw).get("marketdata", {})
            cols, rows = md.get("columns", []), md.get("data", [])
            for col in ("LAST", "MARKETPRICE", "OPEN"):
                if col in cols:
                    idx = cols.index(col)
                    for row in rows:
                        v = row[idx]
                        if v is not None:
                            return round(float(v), 4)
        except Exception as exc:
            logger.debug("moex_currency live %s: %s", secid, exc)
    # Fallback: цена закрытия предыдущей сессии
    raw2 = _get(base + "?iss.only=securities&iss.meta=off")
    if raw2 is not None:
        try:
            sec = json.loads(raw2).get("securities", {})
            cols, rows = sec.get("columns", []), sec.get("data", [])
            for col in ("PREVWAPRICE", "PREVPRICE"):
                if col in cols:
                    idx = cols.index(col)
                    for row in rows:
                        v = row[idx]
                        if v is not None:
                            return round(float(v), 4)
        except Exception as exc:
            logger.debug("moex_currency prev %s: %s", secid, exc)
    return None


# ──────────────────────────────────────────────────────────────
# ЦБ РФ — ключевая ставка (SOAP)
# ──────────────────────────────────────────────────────────────

def _cbr_key_rate() -> float | None:
    """Ключевая ставка ЦБ РФ через публичный SOAP-сервис cbr.ru."""
    today = date.today()
    from_dt = (today - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00")
    to_dt = today.strftime("%Y-%m-%dT00:00:00")
    soap = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<KeyRate xmlns="http://web.cbr.ru/">'
        f"<fromDate>{from_dt}</fromDate>"
        f"<ToDate>{to_dt}</ToDate>"
        "</KeyRate>"
        "</soap:Body>"
        "</soap:Envelope>"
    )
    raw = _get(
        "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
        method="POST",
        data=soap.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://web.cbr.ru/KeyRate",
            "User-Agent": "moex-analyzer/1.0",
        },
    )
    if raw is None:
        return None
    try:
        root = ET.fromstring(raw)
        rates: list[float] = []
        for el in root.iter():
            if el.tag.endswith("}Rate") or el.tag == "Rate":
                if el.text:
                    rates.append(float(el.text.replace(",", ".")))
        return rates[-1] if rates else None
    except Exception as exc:
        logger.debug("cbr_key_rate parse: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────
# Brent (Yahoo Finance)
# ──────────────────────────────────────────────────────────────

def _brent_price() -> float | None:
    """Цена нефти Brent через Yahoo Finance (BZ=F фьючерс)."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=5d&interval=1d"
    raw = _get(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    if raw is None:
        return None
    try:
        closes = (
            json.loads(raw)["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        )
        closes = [c for c in closes if c is not None]
        return round(closes[-1], 2) if closes else None
    except Exception as exc:
        logger.debug("brent_price: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────
# Публичный API
# ──────────────────────────────────────────────────────────────

def fetch_macro() -> dict:
    """
    Параллельно собирает макропоказатели.
    Возвращает dict — каждое поле None если источник недоступен.
    """
    tasks = {
        "imoex":    (_moex_index,    "IMOEX"),
        "rgbi":     (_moex_index,    "RGBI"),
        "usd_rub":  (_moex_currency, "USD000UTSTOM"),
        "cny_rub":  (_moex_currency, "CNYRUB_TOM"),
        "cbr_rate": (_cbr_key_rate,  None),
        "brent":    (_brent_price,   None),
    }
    result: dict = {k: None for k in tasks}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for key, (fn, arg) in tasks.items():
            futs[ex.submit(fn, arg) if arg is not None else ex.submit(fn)] = key
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                result[key] = fut.result()
            except Exception as exc:
                logger.warning("macro %s: %s", key, exc)

    logger.info(
        "Макро: IMOEX=%s USD=%s CNY=%s RGBI=%s ставка=%s%% Brent=%s",
        result["imoex"], result["usd_rub"], result["cny_rub"],
        result["rgbi"], result["cbr_rate"], result["brent"],
    )
    return result
