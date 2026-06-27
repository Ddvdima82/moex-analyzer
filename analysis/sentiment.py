"""
Сентимент-анализ новостей по компании. Провайдер выбирается в конфиге
SENTIMENT_PROVIDER:
  • gemini    — Google Gemini со встроенным поиском (дёшево, есть free tier);
  • anthropic — Claude с web_search (дороже);
  • none      — сентимент отключён (нейтральный, без трат).

При любой ошибке/отсутствии ключа возвращает нейтральный скор 50, не падает.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    SENTIMENT_PROVIDER,
)

logger = logging.getLogger(__name__)

# Шаблон промпта для сентимента
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

    # Берём сбалансированный {...} блок (жадный поиск может захватить лишнее)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("Нет JSON в ответе для %s: %s", ticker, text[:200])
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


def _gemini_search(prompt: str) -> str:
    """Один вызов Gemini со встроенным поиском Google. Возвращает текст ответа."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return resp.text or ""


def _analyze_gemini(ticker: str, company_name: str) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан — нейтральный сентимент для %s", ticker)
        return _neutral_result(ticker, "нет GEMINI-ключа")
    try:
        prompt = SENTIMENT_PROMPT.format(company_name=company_name, ticker=ticker)
        return _parse_sentiment_json(_gemini_search(prompt), ticker)
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
    """
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

    # Если нет новостей совсем — небольшой штраф за неизвестность
    pos = sentiment_data.get("positive_count", 0) or 0
    neg = sentiment_data.get("negative_count", 0) or 0
    if pos + neg == 0:
        base_score = max(base_score - 10, 0)

    return round(base_score, 1)
