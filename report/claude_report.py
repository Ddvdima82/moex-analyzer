"""
Генерация аналитического отчёта через Claude API.
Fallback: при недоступности Claude формируем отчёт программно.
"""
from __future__ import annotations

import json
import logging
from html import escape
from typing import Any

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MAX_RETRIES,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CLAUDE_TIMEOUT,
    today_msk,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Промпт
# ──────────────────────────────────────────────────────────────

REPORT_PROMPT = """Ты опытный аналитик инвестиционного фонда.

Вот результаты еженедельного анализа {count} акций Мосбиржи на {date}:

{scored_stocks_json}

Сформируй читаемый аналитический отчёт для Telegram:

1. Одна строка о состоянии рынка (РАСТУЩИЙ / НЕЙТРАЛЬНЫЙ / ПАДАЮЩИЙ) и IMOEX
2. Краткий комментарий о рынке в целом (2-3 предложения)  
3. Топ-3 на покупку с обоснованием (по одному абзацу)
4. Топ-3 на продажу / избегать с обоснованием
5. НЕ включай полную таблицу — она будет добавлена отдельно

Стиль: профессиональный, конкретный, без воды и воды.
Язык: русский.
Формат: Telegram HTML (только теги <b>, <i>, <code>, без <br>, без markdown).
Максимум 2500 символов."""


def generate_report(scored_stocks: list[dict[str, Any]]) -> str:
    """
    Генерирует текстовую часть отчёта через Claude.
    При ошибке — возвращает программно сформированный fallback.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан — используем программный отчёт")
        return _fallback_report(scored_stocks)

    try:
        import anthropic

        # Подготавливаем упрощённый JSON для промпта (только ключевые поля)
        summary = [
            {
                "ticker": s["ticker"],
                "company": s["company"],
                "price": s["price"],
                "signal": s["signal"],
                "final_score": s["final_score"],
                "target_price": s["target_price"],
                "upside_pct": s["upside_pct"],
                "pe_ratio": s["fundamental"].get("pe_ratio"),
                "div_yield_pct": s["fundamental"].get("div_yield_pct"),
                "roe_pct": s["fundamental"].get("roe_pct"),
                "rsi": s["indicators"].get("rsi"),
                "above_sma200": s["indicators"].get("above_sma200"),
                "key_event": s["sentiment"].get("key_event"),
            }
            for s in scored_stocks
        ]

        prompt = REPORT_PROMPT.format(
            count=len(scored_stocks),
            date=today_msk().strftime("%d.%m.%Y"),
            scored_stocks_json=json.dumps(summary, ensure_ascii=False, indent=2),
        )

        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=CLAUDE_TIMEOUT,
            max_retries=CLAUDE_MAX_RETRIES,
        )
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
                break

        if text:
            logger.info("Отчёт сгенерирован Claude (%d символов)", len(text))
            return text
        else:
            logger.warning("Claude вернул пустой ответ для отчёта")
            return _fallback_report(scored_stocks)

    except Exception as exc:
        logger.error("Ошибка генерации отчёта через Claude: %s", exc, exc_info=True)
        return _fallback_report(scored_stocks)


# ──────────────────────────────────────────────────────────────
# Программный fallback
# ──────────────────────────────────────────────────────────────

def _fallback_report(scored_stocks: list[dict[str, Any]]) -> str:
    """Формирует базовый отчёт без Claude."""
    today = today_msk().strftime("%d.%m.%Y")

    buy_stocks = [s for s in scored_stocks if s["signal"] == "BUY"]
    sell_stocks = [s for s in scored_stocks if s["signal"] == "SELL"]

    lines = [f"📊 <b>Анализ Мосбиржи — {today}</b>\n"]
    lines.append("📈 <i>Автоматический отчёт (режим без AI-комментария)</i>\n")

    if buy_stocks:
        lines.append("\n🟢 <b>ТОП ПОКУПОК</b>")
        for s in sorted(buy_stocks, key=lambda x: -x["final_score"])[:3]:
            upside = f"+{s['upside_pct']}%" if s["upside_pct"] >= 0 else f"{s['upside_pct']}%"
            lines.append(
                f"\n<b>{escape(str(s['ticker']))} ({escape(str(s['company']))})</b> — {s['price']:,.0f} ₽\n"
                f"Score: {s['final_score']}/100 | Цель: {s['target_price']:,.0f} ₽ ({upside})"
            )

    if sell_stocks:
        lines.append("\n\n🔴 <b>ИЗБЕГАТЬ</b>")
        for s in sorted(sell_stocks, key=lambda x: x["final_score"])[:3]:
            upside = f"+{s['upside_pct']}%" if s["upside_pct"] >= 0 else f"{s['upside_pct']}%"
            lines.append(
                f"\n<b>{escape(str(s['ticker']))} ({escape(str(s['company']))})</b> — {s['price']:,.0f} ₽\n"
                f"Score: {s['final_score']}/100 | Риск: {s['target_price']:,.0f} ₽ ({upside})"
            )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Форматирование полной таблицы всех акций
# ──────────────────────────────────────────────────────────────

def format_full_table(scored_stocks: list[dict[str, Any]]) -> str:
    """Форматирует таблицу всех акций для Telegram."""
    sorted_stocks = sorted(scored_stocks, key=lambda x: -x["final_score"])

    header = "━━━━━━━━━━━━━━━━━━━━\n📋 <b>ВСЕ АКЦИИ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    table_lines = ["<code>Тикер  Score  Сигнал"]
    table_lines.append("─────────────────────")

    for s in sorted_stocks:
        emoji = s.get("signal_emoji", "⚪")
        ticker = escape(str(s["ticker"]))
        line = f"{ticker:<6} {s['final_score']:<6.1f} {emoji} {s['signal']}"
        table_lines.append(line)

    table_lines.append("</code>")
    footer = "\n\n⚠️ <i>Не является инвестиционной рекомендацией.</i>\n🤖 Claude Sonnet | Данные: MOEX ISS API"

    return header + "\n".join(table_lines) + footer
