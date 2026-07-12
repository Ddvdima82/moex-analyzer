"""
Отправка отчётов в Telegram.
Учитывает лимит 4096 символов — разбивает длинные сообщения.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4096


def _post(method: str, payload: dict[str, Any]) -> bool:
    """Отправляет запрос к Telegram Bot API."""
    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            logger.error("Telegram API ошибка: %s", result.get("description"))
            return False
        return True
    except Exception as exc:
        logger.error("Ошибка отправки в Telegram: %s", exc, exc_info=True)
        return False


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    """Убирает HTML-теги — для фолбэка отправки без parse_mode."""
    return _TAG_RE.sub("", text)


def _balance_code(part: str, open_before: bool) -> tuple[str, bool]:
    """
    Балансирует тег <code> в одной части сообщения.
    Если часть начинается внутри открытого <code> — дописываем открывающий тег
    в начало; если внутри части <code> остаётся открытым — закрываем его в конце.
    Возвращает (исправленная часть, открыт ли <code> в конце).
    """
    prefix = "<code>" if open_before else ""
    opens = part.count("<code>")
    closes = part.count("</code>")
    open_after = (1 if open_before else 0) + opens - closes > 0
    suffix = "</code>" if open_after else ""
    return prefix + part + suffix, open_after


def _split_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    """
    Разбивает длинное сообщение на части по max_len символов.
    Режет по границам строк (теги Telegram однострочные, поэтому разрыв тега
    посередине исключён) и балансирует многострочный <code>-блок между частями,
    чтобы не оставить незакрытых/осиротевших тегов — иначе Telegram вернёт 400.
    """
    if len(text) <= max_len:
        return [text]

    raw_parts: list[str] = []
    # Резерв под возможные <code>/</code> (по 7 символов с каждой стороны)
    budget = max_len - 14
    while text:
        if len(text) <= budget:
            raw_parts.append(text)
            break
        split_at = text.rfind("\n", 0, budget)
        if split_at == -1:
            split_at = budget
        raw_parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    parts: list[str] = []
    open_code = False
    for raw in raw_parts:
        fixed, open_code = _balance_code(raw, open_code)
        parts.append(fixed)
    return parts


def send_report(text: str) -> bool:
    """
    Отправляет отчёт в Telegram-чат.
    Длинные сообщения разбиваются на части.
    Возвращает True если все части отправлены.
    """
    if not TELEGRAM_ENABLED:
        logger.info("TELEGRAM_ENABLED не задан — отправка пропущена (боевой канал шлёт только CI)")
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "Telegram не настроен: TELEGRAM_BOT_TOKEN=%s, TELEGRAM_CHAT_ID=%s",
            bool(TELEGRAM_BOT_TOKEN),
            bool(TELEGRAM_CHAT_ID),
        )
        return False

    parts = _split_message(text)
    success = True

    for i, part in enumerate(parts, 1):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        ok = _post("sendMessage", payload)

        # Фолбэк: битая HTML-разметка (от Claude или нашей) → Telegram 400.
        # Повторяем без parse_mode со снятыми тегами, чтобы сообщение дошло.
        if not ok:
            logger.warning("HTML-отправка части %d/%d не удалась — повтор без разметки", i, len(parts))
            plain_payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": _strip_tags(part),
                "disable_web_page_preview": True,
            }
            ok = _post("sendMessage", plain_payload)

        if not ok:
            logger.error("Не удалось отправить часть %d/%d", i, len(parts))
            success = False
        elif len(parts) > 1:
            time.sleep(0.5)  # небольшая пауза между частями

    return success


def send_error(message: str) -> bool:
    """
    Отправляет уведомление об ошибке в Telegram.
    Используется при сбое pipeline.
    """
    error_text = f"⛔ <b>Ошибка анализа Мосбиржи</b>\n\n<code>{message[:500]}</code>"
    return send_report(error_text)


def check_connection() -> bool:
    """Проверяет доступность Telegram Bot API (getMe). False при выключенной отправке."""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return False

    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="getMe")
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("ok"):
            bot_name = data["result"].get("username", "unknown")
            logger.info("Telegram бот подключён: @%s", bot_name)
            return True
    except Exception as exc:
        logger.error("Telegram недоступен: %s", exc)

    return False
