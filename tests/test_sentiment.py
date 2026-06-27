"""Тесты сентимент-провайдеров и парсинга (analysis/sentiment.py)."""
import analysis.sentiment as sent


# ── Парсинг JSON ─────────────────────────────────────────────

def test_parse_valid_json():
    txt = 'мусор до {"sentiment_score": 70, "overall_sentiment": "positive", "key_event": "отчёт"} мусор после'
    d = sent._parse_sentiment_json(txt, "SBER")
    assert d["sentiment_score"] == 70
    assert d.get("error") is None


def test_parse_no_json():
    d = sent._parse_sentiment_json("совсем без скобок", "SBER")
    assert d["sentiment_score"] == 50 and d["error"]


def test_parse_invalid_json():
    d = sent._parse_sentiment_json("{это не json}", "SBER")
    assert d["sentiment_score"] == 50 and d["error"]


def test_parse_incomplete_json():
    d = sent._parse_sentiment_json('{"sentiment_score": 60}', "SBER")  # нет overall/key_event
    assert d["error"] == "неполный ответ"


def test_parse_empty():
    assert sent._parse_sentiment_json("", "SBER")["error"]


# ── Диспетчер провайдеров ────────────────────────────────────

def test_dispatch_gemini(monkeypatch):
    monkeypatch.setattr(sent, "SENTIMENT_PROVIDER", "gemini")
    monkeypatch.setattr(sent, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(sent, "_gemini_search",
                        lambda p: '{"sentiment_score": 75, "overall_sentiment": "positive", "key_event": "рост"}')
    d = sent.analyze_sentiment("SBER", "Сбербанк")
    assert d["sentiment_score"] == 75 and d.get("error") is None


def test_dispatch_gemini_no_key(monkeypatch):
    monkeypatch.setattr(sent, "SENTIMENT_PROVIDER", "gemini")
    monkeypatch.setattr(sent, "GEMINI_API_KEY", "")
    d = sent.analyze_sentiment("SBER", "Сбербанк")
    assert d["sentiment_score"] == 50 and "GEMINI" in d["error"]


def test_dispatch_gemini_error_falls_back(monkeypatch):
    monkeypatch.setattr(sent, "SENTIMENT_PROVIDER", "gemini")
    monkeypatch.setattr(sent, "GEMINI_API_KEY", "fake-key")
    def boom(p): raise RuntimeError("сеть упала")
    monkeypatch.setattr(sent, "_gemini_search", boom)
    d = sent.analyze_sentiment("SBER", "Сбербанк")
    assert d["sentiment_score"] == 50 and d["error"]


def test_dispatch_none(monkeypatch):
    monkeypatch.setattr(sent, "SENTIMENT_PROVIDER", "none")
    d = sent.analyze_sentiment("SBER", "Сбербанк")
    assert d["sentiment_score"] == 50 and "отключён" in d["error"]


def test_dispatch_unknown(monkeypatch):
    monkeypatch.setattr(sent, "SENTIMENT_PROVIDER", "openai")
    d = sent.analyze_sentiment("SBER", "Сбербанк")
    assert d["sentiment_score"] == 50 and "openai" in d["error"]


# ── Скоринг ──────────────────────────────────────────────────

def test_score_sentiment_penalty_no_news():
    # нет новостей → штраф -10
    assert sent.score_sentiment({"sentiment_score": 60, "positive_count": 0, "negative_count": 0}) == 50.0


def test_score_sentiment_with_news():
    assert sent.score_sentiment({"sentiment_score": 72, "positive_count": 3, "negative_count": 1}) == 72.0
