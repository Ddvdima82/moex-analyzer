"""Тесты фильтрации новостей и кэша RSS-лент (data/news.py)."""
import pytest

import data.news as news


@pytest.fixture(autouse=True)
def clear_feeds_cache(monkeypatch):
    """Сбрасывает кэш лент перед каждым тестом."""
    monkeypatch.setattr(news, "_FEEDS_CACHE", None)


# ── _matches: длинные слова подстрокой, короткие — целым словом ──

def test_matches_long_word_substring_inflection():
    # Падежи ловятся подстрокой
    assert news._matches("прибыль сбербанка выросла", ["сбербанк"])


def test_matches_short_word_whole_word():
    assert news._matches("ВТБ отчитался о прибыли".lower(), ["втб"])
    assert news._matches("тарифы МТС вырастут".lower(), ["мтс"])
    assert news._matches("акции X5 обновили максимум".lower(), ["x5"])


def test_matches_short_word_not_inside_other_words():
    # «пик» не должен совпадать внутри «пикник», «втб» внутри случайных слов
    assert not news._matches("пикник на обочине", ["пик"])
    assert not news._matches("оптика и фотоника", ["пик"])


def test_matches_no_keywords_hit():
    assert not news._matches("новости о погоде", ["сбербанк", "втб"])


# ── keywords для коротких имён компаний ──────────────────────

def test_fetch_headlines_finds_short_company_names(monkeypatch):
    """МТС/ВТБ/ПИК: короткие названия (3 буквы) должны находиться."""
    items = [
        ("ВТБ увеличил прибыль в 2 раза", "", ""),
        ("Пикники подорожали", "", ""),
        ("Совет директоров МТС рекомендовал дивиденды", "", ""),
    ]
    monkeypatch.setattr(news, "_load_all_items", lambda force=False: items)

    got_vtbr = news.fetch_headlines("VTBR", "ВТБ")
    assert [h.title for h in got_vtbr] == ["ВТБ увеличил прибыль в 2 раза"]

    got_mtss = news.fetch_headlines("MTSS", "МТС")
    assert [h.title for h in got_mtss] == ["Совет директоров МТС рекомендовал дивиденды"]

    got_pikk = news.fetch_headlines("PIKK", "ПИК")
    assert got_pikk == []  # «Пикники» не считаются упоминанием ПИК


def test_fetch_headlines_respects_max(monkeypatch):
    items = [(f"Сбербанк новость {i}", "", "") for i in range(20)]
    monkeypatch.setattr(news, "_load_all_items", lambda force=False: items)
    got = news.fetch_headlines("SBER", "Сбербанк", max_headlines=5)
    assert len(got) == 5


def test_fetch_headlines_dedupes_titles(monkeypatch):
    items = [("Сбербанк отчитался", "", ""), ("Сбербанк отчитался", "", "")]
    monkeypatch.setattr(news, "_load_all_items", lambda force=False: items)
    assert len(news.fetch_headlines("SBER", "Сбербанк")) == 1


def test_stop_words_excluded_from_keywords(monkeypatch):
    """Служебные слова («ГК», «группа») не должны тащить чужие новости."""
    items = [("ГК ПИК построит квартал", "", "")]
    monkeypatch.setattr(news, "_load_all_items", lambda force=False: items)
    # Гипотетический «ГК Самолет»: ключ «гк» отброшен → новость ПИК не матчится
    assert news.fetch_headlines("SMLT", "ГК Самолет") == []


# ── кэш лент: сеть дёргается один раз за процесс ─────────────

def _fake_item(title="Заголовок"):
    import xml.etree.ElementTree as ET
    return ET.fromstring(f"<item><title>{title}</title></item>")


def test_load_all_items_fetches_feeds_once(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return [_fake_item()]

    monkeypatch.setattr(news, "_fetch_rss", fake_fetch)
    news._load_all_items()
    news._load_all_items()  # второй вызов — из кэша
    assert len(calls) == len(news._RSS_FEEDS)


def test_load_all_items_empty_result_not_cached(monkeypatch):
    """Сетевой сбой (все ленты пустые) не должен кэшироваться на весь процесс."""
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return []

    monkeypatch.setattr(news, "_fetch_rss", fake_fetch)
    assert news._load_all_items() == []
    assert news._load_all_items() == []  # повторная попытка, не кэш
    assert len(calls) == 2 * len(news._RSS_FEEDS)


def test_load_all_items_force_refetch(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return [_fake_item()]

    monkeypatch.setattr(news, "_fetch_rss", fake_fetch)
    news._load_all_items()
    news._load_all_items(force=True)
    assert len(calls) == 2 * len(news._RSS_FEEDS)
