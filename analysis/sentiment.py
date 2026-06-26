"""
Сентимент-анализ через Claude API с инструментом web_search.
Ищет новости за последние 7 дней и оценивает тональность.

Если Claude API недоступен или нет ключа — возвращает нейтральный скор 50
и сообщение об ошибке вместо краша.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT,
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


def analyze_sentiment(ticker: str, company_name: str) -> dict[str, Any]:
    """
    Анализирует новостной сентимент по компании через Claude API.
    При любой ошибке возвращает нейтральный результат (не падает).
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан — возвращаем нейтральный сентимент для %s", ticker)
        return _neutral_result(ticker, "нет API-ключа")

    try:
        import anthropic

        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=CLAUDE_TIMEOUT,
            max_retries=CLAUDE_MAX_RETRIES,
        )

        prompt = SENTIMENT_PROMPT.format(company_name=company_name, ticker=ticker)

        # web_search — серверный инструмент (GA): обычный messages.create, без beta.
        # Тип 20250305 поддерживается sonnet-4-5/4-6. Если вызов падает (нет
        # доступа к инструменту, старый SDK) — фолбэк на запрос без поиска.
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as search_exc:
            logger.warning(
                "web_search для %s недоступен (%s), анализ без реального поиска",
                ticker, search_exc,
            )
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Ты финансовый аналитик. Дай нейтральную оценку сентимента "
                            f"для компании {company_name} ({ticker}) на текущей неделе. "
                            "Верни ТОЛЬКО JSON: "
                            '{"news":[],"positive_count":0,"negative_count":0,"neutral_count":0,'
                            '"overall_sentiment":"neutral","key_event":"Нет данных поиска",'
                            '"sentiment_score":50}'
                        ),
                    }
                ],
            )

        # Извлекаем текстовый ответ из блоков. При web_search в content есть
        # промежуточные блоки (server_tool_use, web_search_tool_result) — JSON
        # обычно в ПОСЛЕДНЕМ text-блоке, поэтому берём его, а не первый.
        text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", ""):
                text = block.text

        if not text:
            return _neutral_result(ticker, "пустой ответ Claude")

        # Парсим JSON (ищем первый {...} блок)
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            logger.warning("Claude не вернул JSON для %s: %s", ticker, text[:200])
            return _neutral_result(ticker, "JSON не найден в ответе")

        data = json.loads(json_match.group())

        # Проверка обязательных полей
        required = {"sentiment_score", "overall_sentiment", "key_event"}
        if not required.issubset(data.keys()):
            logger.warning("Неполный JSON для %s", ticker)
            return _neutral_result(ticker, "неполный ответ")

        logger.info(
            "Сентимент %s: %s (score=%s)",
            ticker,
            data.get("overall_sentiment"),
            data.get("sentiment_score"),
        )
        return data

    except Exception as exc:
        logger.error("Ошибка сентимент-анализа %s: %s", ticker, exc, exc_info=True)
        return _neutral_result(ticker, str(exc)[:100])


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
