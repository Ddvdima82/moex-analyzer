"""Тесты безопасного Telegram-HTML: экранирование и нарезка."""
from report import telegram_bot as tg
from report.claude_report import format_full_table, _fallback_report


def _stock(ticker="SBER", company="Сбербанк", signal="BUY", score=80.0):
    return {
        "ticker": ticker, "company": company, "signal": signal,
        "signal_emoji": "🟢", "final_score": score, "price": 300.0,
        "target_price": 330.0, "upside_pct": 10.0,
    }


def test_strip_tags():
    assert tg._strip_tags("<b>Привет</b> <code>x</code>") == "Привет x"


def test_balance_code_opens_and_closes():
    # Часть начинается внутри открытого <code> и сама его не закрывает
    fixed, still_open = tg._balance_code("строка данных", open_before=True)
    assert fixed.startswith("<code>") and fixed.endswith("</code>")
    assert still_open is True


def test_balance_code_closed_within():
    fixed, still_open = tg._balance_code("<code>x</code>", open_before=False)
    assert still_open is False
    assert fixed.count("<code>") == fixed.count("</code>")


def test_split_message_balances_code_tags():
    # Большой <code>-блок, заведомо превышающий лимит сообщения
    body = "\n".join(f"row {i}" for i in range(2000))
    text = f"<code>{body}</code>"
    parts = tg._split_message(text, max_len=1000)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 1000
        assert p.count("<code>") == p.count("</code>")  # теги сбалансированы в каждой части


def test_split_message_short_passthrough():
    assert tg._split_message("коротко") == ["коротко"]


def test_format_full_table_escapes_and_balances():
    # Тикер с инъекцией HTML не должен ломать разметку
    stocks = [_stock(ticker="E<v>L", company="<b>x</b>")]
    out = format_full_table(stocks)
    assert "E<v>L" not in out                 # угловые скобки экранированы
    assert "E&lt;v&gt;L" in out
    assert out.count("<code>") == out.count("</code>")


def test_fallback_report_escapes_company():
    stocks = [_stock(company="Зло<script>")]
    out = _fallback_report(stocks)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
