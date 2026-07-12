"""
Сборщик заголовков новостей из публичных RSS-лент российских СМИ.
Без внешних зависимостей: urllib + xml.etree.ElementTree.

Ленты скачиваются ОДИН раз за процесс (параллельно) и кэшируются —
fetch_headlines() для каждого тикера фильтрует уже загруженные заголовки,
а не ходит в сеть повторно (раньше: 20 тикеров × 8 лент = 160 запросов).
"""
from __future__ import annotations

import email.utils
import logging
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

logger = logging.getLogger(__name__)

# RSS-ленты с финансовыми новостями (берём широкий охват, фильтруем по компании)
_RSS_FEEDS = [
    "https://www.interfax.ru/rss.asp",
    "https://rssexport.rbc.ru/rbcnews/news/20/full.rss",
    "https://tass.ru/rss/v2.xml",
    "https://www.vedomosti.ru/rss/news",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://1prime.ru/rss",
    "https://www.finam.ru/analysis/newsitem/rss/",
    "https://bcs-express.ru/rss",
]

_REQUEST_TIMEOUT = 10  # сек
_MAX_AGE_DAYS = 7      # смотрим новости за неделю
_MAX_HEADLINES = 10    # возвращаем не более N заголовков

# Кэш скачанных лент на процесс: список (title, description, pub_date)
_FEEDS_LOCK = threading.Lock()
_FEEDS_CACHE: list[tuple[str, str, str]] | None = None

# Служебные слова, бесполезные как ключи поиска: «ГК Самолет» не должен
# матчить каждый заголовок с «ГК», «Группа Позитив» — каждый с «группа»
_STOP_WORDS = {"ао", "пао", "оао", "зао", "нк", "гк", "мк", "группа", "компания", "group", "retail"}


class Headline(NamedTuple):
    title: str
    pub_date: str   # ISO строка, может быть пустой


def _fetch_rss(url: str) -> list[ET.Element]:
    """Загружает RSS и возвращает список <item> элементов."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; moex-analyzer/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        # Стандартный RSS: root → channel → item
        # Atom: root → entry (NS http://www.w3.org/2005/Atom)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        return items
    except Exception as exc:
        logger.debug("RSS %s — ошибка: %s", url, exc)
        return []


def _item_text(item: ET.Element, tag: str, atom_tag: str = "") -> str:
    """Безопасно извлекает текст тега из RSS/Atom элемента."""
    el = item.find(tag)
    if el is None and atom_tag:
        el = item.find(f"{{http://www.w3.org/2005/Atom}}{atom_tag}")
    return (el.text or "").strip() if el is not None else ""


def _parse_rfc2822(date_str: str) -> datetime | None:
    """Парсит RFC-2822 дату из RSS через стандартный email.utils."""
    try:
        tup = email.utils.parsedate_tz(date_str.strip())
        if tup is None:
            return None
        ts = email.utils.mktime_tz(tup)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _is_recent(pub_date_str: str, max_age_days: int) -> bool:
    """True если новость не старше max_age_days дней."""
    if not pub_date_str:
        return True  # без даты не отфильтровываем
    # Пробуем RFC-2822 и ISO-8601
    dt = _parse_rfc2822(pub_date_str)
    if dt is None:
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
        except ValueError:
            return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return dt >= cutoff


def _matches(text: str, keywords: list[str]) -> bool:
    """
    True если в тексте есть хотя бы одно ключевое слово (без учёта регистра).
    Длинные слова (>3) ищутся подстрокой — так ловятся падежи («Сбербанка»).
    Короткие («МТС», «ВТБ», «ПИК», «X5») — только целым словом, иначе они либо
    не искались вовсе, либо совпадали внутри случайных слов.
    """
    text_lower = text.lower()
    for kw in keywords:
        if len(kw) > 3:
            if kw in text_lower:
                return True
        elif re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text_lower):
            return True
    return False


def _load_all_items(force: bool = False) -> list[tuple[str, str, str]]:
    """
    Скачивает все RSS-ленты параллельно, один раз за процесс.
    Возвращает список (title, description, pub_date). Потокобезопасно.
    """
    global _FEEDS_CACHE
    with _FEEDS_LOCK:
        if _FEEDS_CACHE is not None and not force:
            return _FEEDS_CACHE

        items: list[tuple[str, str, str]] = []
        with ThreadPoolExecutor(max_workers=len(_RSS_FEEDS)) as ex:
            for feed_items in ex.map(_fetch_rss, _RSS_FEEDS):
                for item in feed_items:
                    title = _item_text(item, "title", "title")
                    desc = _item_text(item, "description", "summary")
                    pub = _item_text(item, "pubDate", "updated") or _item_text(item, "published")
                    if title:
                        items.append((title, desc, pub))

        # Пустой результат НЕ кэшируем: транзиентный сетевой сбой на старте
        # иначе оставил бы весь прогон без новостей без единой повторной попытки
        if items:
            _FEEDS_CACHE = items
            logger.info("RSS: загружено %d заголовков из %d лент", len(items), len(_RSS_FEEDS))
        else:
            logger.warning("RSS: ни одна из %d лент не вернула данных — повторим при следующем вызове", len(_RSS_FEEDS))
        return items


def fetch_headlines(
    ticker: str,
    company_name: str,
    max_headlines: int = _MAX_HEADLINES,
) -> list[Headline]:
    """
    Возвращает свежие заголовки новостей о компании из кэша RSS-лент
    (сеть — только при первом вызове за процесс). Не более max_headlines.
    """
    # Ключевые слова для фильтрации: тикер + слова из названия
    # (кроме односимвольных и служебных вроде «ГК»/«ПАО»)
    words = re.split(r"[\s\-]+", company_name)
    keywords = [ticker] + [w for w in words if len(w) > 1 and w.lower() not in _STOP_WORDS]
    keywords = list(dict.fromkeys(kw.lower() for kw in keywords))  # дедупликация

    found: list[Headline] = []
    seen: set[str] = set()

    for title, desc, pub in _load_all_items():
        if len(found) >= max_headlines:
            break
        if title in seen:
            continue
        if not _is_recent(pub, _MAX_AGE_DAYS):
            continue
        if _matches(title + " " + desc, keywords):
            seen.add(title)
            found.append(Headline(title=title, pub_date=pub[:25] if pub else ""))

    logger.info("Новости %s (%s): найдено %d заголовков", ticker, company_name, len(found))
    return found
