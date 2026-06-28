"""
Сентимент-анализ новостей по компании. Провайдер выбирается в конфиге
SENTIMENT_PROVIDER:
  • gemini    — Gemini анализирует заголовки из RSS одним батч-вызовом (1 запрос на все
                тикеры вместо N); используй batch_analyze_sentiment() перед пайплайном;
  • anthropic — Claude с web_search (дороже);
  • none      — сентимент отключён (нейтральный, без трат).

При любой ошибке/отсутствии ключа возвращает нейтральный скор 50, не падает.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT,
    GEMINI_API_KEY,
    GEMINI_CONCURRENCY,
    GEMINI_MAX_RETRIES,
    GEMINI_MODEL,
    GEMINI_RETRY_DELAY,
    SENTIMENT_PROVIDER,
)

logger = logging.getLogger(__name__)

# Ограничитель параллелизма (для Anthropic-провайдера; Gemini без grounding терпим)
_GEMINI_SEM = threading.Semaphore(max(1, GEMINI_CONCURRENCY))

# Кэш результатов батч-анализа: {ticker: sentiment_dict}
# Заполняется batch_analyze_sentiment() до старта параллельного пайплайна.
# analyze_sentiment() проверяет кэш первым — API не вызывается повторно.
_SENTIMENT_CACHE: dict[str, dict] = {}

# Промпт для Gemini: классифицируем готовые заголовки, grounding НЕ нужен
_GEMINI_PROMPT = """Ты финансовый аналитик российского рынка акций.

Ниже — заголовки новостей за последние 7 дней о компании {company_name} (тикер {ticker}):

{headlines}

Оцени каждую новость как positive / neutral / negative.
Учитывай: финансовые отчёты, дивиденды, корпоративные события, санкции, регуляторику, операционные новости.

Верни ТОЛЬКО валидный JSON без markdown-блоков:
{{
  "news": [
    {{"headline": "...", "sentiment": "positive", "impact": "high"}},
    {{"headline": "...", "sentiment": "neutral", "impact": "low"}}
  ],
  "positive_count": 3,
  "negative_count": 1,
  "neutral_count": 2,
  "overall_sentiment": "positive",
  "key_event": "Краткое описание главного события недели (1 предложение)",
  "sentiment_score": 65
}}

sentiment_score: от 0 (очень негативно) до 100 (очень позитивно), 50 = нейтрально."""

# Промпт для Anthropic (с web_search)
SENTIMENT_PROMPT = """Ты финансовый аналитик российского рынка акций.

Используй инструмент web_search и найди последние 5-7 новостей о компании \
{company_name} (тикер {ticker}) на российском фондовом рынке за последние 7 дней.

Оцени каждую новость как positive / neutral / negative.
Учитывай: финансовые отчёты, дивиденды, корпоративные события, санкции, регуляторику, \
операционные новости.

Верни ТОЛЬКО валидный JSON без markdown-блоков и лишнего текста:
{{
  "news": [
    {{"headline": "...", "sentiment": "positive", "impact": "high"}},
    {{"headline": "...", "sentiment": "neutral", "impact": "low"}}
  ],
  "positive_count": 3,
  "negative_count": 1,
  "neutral_count": 2,
  "overall_sentiment": "positive",
  "key_event": "Краткое описание главного события недели (1 предложение)",
  "sentiment_score": 65
}}

sentiment_score: от 0 (очень негативно) до 100 (очень позитивно), 50 = нейтрально."""


# Батч-промпт: один вызов Gemini для ВСЕХ тикеров → 20 вызовов → 1
_GEMINI_BATCH_PROMPT = """Ты финансовый аналитик российского рынка акций.

Ниже — заголовки новостей за последние 7 дней по нескольким компаниям.

{sections}

Для каждой компании оцени сентимент. Учитывай: финансовые отчёты, дивиденды, корпоративные события, санкции, регуляторику, операционные новости.

Верни ТОЛЬКО валидный JSON без markdown-блоков, где ключи — тикеры:
{{
  "TICKER1": {{
    "news": [{{"headline": "...", "sentiment": "positive", "impact": "high"}}],
    "positive_count": 2,
    "negative_count": 0,
    "neutral_count": 1,
    "overall_sentiment": "positive",
    "key_event": "Главное событие (1 предложение)",
    "sentiment_score": 65
  }},
  "TICKER2": {{...}}
}}

sentiment_score: от 0 (очень негативно) до 100 (очень позитивно), 50 = нейтрально."""


def _build_batch_sections(ticker_headlines: dict[str, tuple[str, list]]) -> str:
    """Формирует текстовые секции для батч-промпта."""
    parts = []
    for ticker, (company_name, headlines) in ticker_headlines.items():
        body = "\n".join(f"- {h.title}" for h in headlines) if headlines else "нет новостей"
        parts.append(f"## {ticker} ({company_name})\n{body}")
    return "\n\n".join(parts)


def _parse_batch_response(text: str, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Парсит батч-ответ модели → {ticker: sentiment_dict}."""
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        logger.warning("Невалидный батч-JSON от Gemini: %s", text[:300])
        return {}
    required = {"sentiment_score", "overall_sentiment", "key_event"}
    results: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        item = data.get(ticker)
        if isinstance(item, dict) and required.issubset(item.keys()):
            results[ticker] = item
    return results


def batch_analyze_sentiment(worklist: list[tuple[str, str]]) -> None:
    """
    Предзагружает сентимент для всех тикеров ОДНИМ вызовом Gemini.
    Вызывать до запуска параллельного пайплайна; analyze_sentiment() проверит кэш.
    При любой ошибке молча пропускает — пайплайн деградирует на нейтральный скор.
    """
    if SENTIMENT_PROVIDER != "gemini" or not GEMINI_API_KEY:
        return

    from data.news import fetch_headlines

    # Параллельно собираем заголовки (I/O-bound: HTTP к RSS-лентам)
    ticker_headlines: dict[str, tuple[str, list]] = {}

    def _fetch(ticker: str, company_name: str) -> tuple[str, str, list]:
        return ticker, company_name, fetch_headlines(ticker, company_name)

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_fetch, t, n): t for t, n in worklist}
        for fut in as_completed(futs):
            try:
                ticker, company_name, headlines = fut.result()
                ticker_headlines[ticker] = (company_name, headlines)
            except Exception as exc:
                logger.warning("Ошибка получения новостей: %s", exc)

    # Тикеры без новостей — сразу нейтральный, не тратим токены
    tickers_with_news: list[str] = []
    for ticker, (_, headlines) in ticker_headlines.items():
        if not headlines:
            _SENTIMENT_CACHE[ticker] = _neutral_result(ticker, "новостей не найдено")
            logger.info("Батч: нет новостей %s — нейтральный", ticker)
        else:
            tickers_with_news.append(ticker)

    if not tickers_with_news:
        logger.info("Батч-сентимент: новостей нет ни по одному тикеру")
        return

    logger.info("Батч-сентимент: %d тикеров с новостями → 1 вызов Gemini", len(tickers_with_news))
    sections = _build_batch_sections({t: ticker_headlines[t] for t in tickers_with_news})
    prompt = _GEMINI_BATCH_PROMPT.format(sections=sections)

    try:
        raw = _gemini_classify(prompt)
        results = _parse_batch_response(raw, tickers_with_news)
        for ticker in tickers_with_news:
            if ticker in results:
                _SENTIMENT_CACHE[ticker] = results[ticker]
                logger.info("Батч %s: %s (score=%s)", ticker,
                            results[ticker].get("overall_sentiment"),
                            results[ticker].get("sentiment_score"))
            else:
                logger.warning("Батч: нет результата для %s — нейтральный", ticker)
                _SENTIMENT_CACHE[ticker] = _neutral_result(ticker, "нет в батч-ответе")
    except Exception as exc:
        logger.error("Ошибка батч-Gemini: %s", exc, exc_info=True)
        for ticker in tickers_with_news:
            if ticker not in _SENTIMENT_CACHE:
                _SENTIMENT_CACHE[ticker] = _neutral_result(ticker, str(exc)[:100])


def _neutral_result(ticker: str, reason: str = "") -> dict[str, Any]:
    """Нейтральный результат при ошибке."""
    return {
        "news": [],
        "positive_count": 0,
        "negative_count": 0,
        "neutral_count": 0,
        "overall_sentiment": "neutral",
        "key_event": f"Нет данных ({reason})" if reason else "Нет данных",
        "sentiment_score": 50,
        "error": reason,
    }


def _parse_sentiment_json(text: str, ticker: str) -> dict[str, Any]:
    """Извлекает и валидирует JSON-объект сентимента из ответа модели."""
    if not text:
        return _neutral_result(ticker, "пустой ответ модели")

    start = text.find("{")
    if start == -1:
        logger.warning("Нет JSON в ответе для %s: %s", ticker, text[:200])
        return _neutral_result(ticker, "JSON не найден в ответе")

    # Балансируем скобки чтобы не захватить мусор после JSON
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        logger.warning("Незакрытый JSON для %s: %s", ticker, text[:200])
        return _neutral_result(ticker, "JSON не найден в ответе")

    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        logger.warning("Невалидный JSON для %s: %s", ticker, text[start:start + 200])
        return _neutral_result(ticker, "невалидный JSON")

    required = {"sentiment_score", "overall_sentiment", "key_event"}
    if not required.issubset(data.keys()):
        logger.warning("Неполный JSON для %s", ticker)
        return _neutral_result(ticker, "неполный ответ")

    logger.info("Сентимент %s: %s (score=%s)", ticker,
                data.get("overall_sentiment"), data.get("sentiment_score"))
    return data


def _gemini_classify(prompt: str) -> str:
    """
    Вызов Gemini БЕЗ grounding — классифицирует готовый текст.
    Без grounding free-tier лимит ~1500 RPM (vs ~15 для grounded).
    Семафор оставляем для защиты от пиков; backoff на 429 сохраняем.
    """
    from google import genai
    from google.genai import errors

    client = genai.Client(api_key=GEMINI_API_KEY)

    with _GEMINI_SEM:
        last_exc: Exception | None = None
        for attempt in range(GEMINI_MAX_RETRIES):
            try:
                resp = client.models.generate_content(
                    model=GEMINI_MODEL, contents=prompt,
                )
                return resp.text or ""
            except errors.ClientError as exc:
                if getattr(exc, "code", None) != 429:
                    raise
                last_exc = exc
                if attempt < GEMINI_MAX_RETRIES - 1:
                    delay = GEMINI_RETRY_DELAY * (2 ** attempt)
                    logger.warning("Gemini 429 — повтор через %.0fс (попытка %d/%d)",
                                   delay, attempt + 1, GEMINI_MAX_RETRIES)
                    time.sleep(delay)
        raise last_exc  # type: ignore[misc]


def _analyze_gemini(ticker: str, company_name: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан — нейтральный сентимент для %s", ticker)
        return _neutral_result(ticker, "нет GEMINI-ключа")

    from data.news import fetch_headlines
    headlines = fetch_headlines(ticker, company_name)

    if not headlines:
        logger.info("Нет новостей для %s — нейтральный сентимент", ticker)
        return _neutral_result(ticker, "новостей не найдено")

    headlines_text = "\n".join(f"- {h.title}" for h in headlines)
    try:
        prompt = _GEMINI_PROMPT.format(
            company_name=company_name, ticker=ticker, headlines=headlines_text,
        )
        return _parse_sentiment_json(_gemini_classify(prompt), ticker)
    except Exception as exc:
        logger.error("Ошибка Gemini-сентимента %s: %s", ticker, exc, exc_info=True)
        return _neutral_result(ticker, str(exc)[:100])


def _analyze_anthropic(ticker: str, company_name: str) -> dict[str, Any]:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан — нейтральный сентимент для %s", ticker)
        return _neutral_result(ticker, "нет API-ключа")
    try:
        import anthropic

        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=CLAUDE_TIMEOUT,
            max_retries=CLAUDE_MAX_RETRIES,
        )
        prompt = SENTIMENT_PROMPT.format(company_name=company_name, ticker=ticker)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
        )
        # JSON обычно в ПОСЛЕДНЕМ text-блоке (до него — server_tool_use и т.п.)
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", ""):
                text = block.text
        return _parse_sentiment_json(text, ticker)
    except Exception as exc:
        logger.error("Ошибка Claude-сентимента %s: %s", ticker, exc, exc_info=True)
        return _neutral_result(ticker, str(exc)[:100])


def analyze_sentiment(ticker: str, company_name: str) -> dict[str, Any]:
    """
    Анализирует новостной сентимент по компании выбранным провайдером
    (config.SENTIMENT_PROVIDER). При любой ошибке — нейтральный результат.
    Если batch_analyze_sentiment() уже заполнил кэш — API не вызывается.
    """
    if ticker in _SENTIMENT_CACHE:
        return _SENTIMENT_CACHE[ticker]

    if SENTIMENT_PROVIDER == "gemini":
        return _analyze_gemini(ticker, company_name)
    if SENTIMENT_PROVIDER == "anthropic":
        return _analyze_anthropic(ticker, company_name)
    if SENTIMENT_PROVIDER == "none":
        return _neutral_result(ticker, "сентимент отключён")
    logger.warning("Неизвестный SENTIMENT_PROVIDER=%s — нейтральный сентимент", SENTIMENT_PROVIDER)
    return _neutral_result(ticker, f"неизвестный провайдер {SENTIMENT_PROVIDER}")


# ──────────────────────────────────────────────────────────────
# Скоринг 0–100
# ──────────────────────────────────────────────────────────────

def score_sentiment(sentiment_data: dict[str, Any]) -> float:
    """
    Скор сентимента от 0 до 100.
    Базовый score из Claude. Штраф -10 если нет новостей.
    """
    base_score = float(sentiment_data.get("sentiment_score", 50))

    # Штраф только если новостей нет совсем (нейтральные — это тоже данные)
    if not sentiment_data.get("news"):
        base_score = max(base_score - 10, 0)

    return round(base_score, 1)
