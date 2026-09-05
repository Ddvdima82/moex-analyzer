"""Тесты парсинга ключевой ставки ЦБ (data/macro.py)."""
from data.macro import _cbr_key_rate


def _soap_response(pairs: list[tuple[str, str]]) -> bytes:
    """Собирает SOAP-ответ КБР с парами (дата, ставка) в заданном порядке."""
    rows = "".join(
        f'<KR diffgr:id="KR{i}"><DT>{dt}T00:00:00+03:00</DT><Rate>{rate}</Rate></KR>'
        for i, (dt, rate) in enumerate(pairs)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><KeyRateResponse><KeyRateResult>'
        '<diffgr:diffgram xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1">'
        f'<KeyRate>{rows}</KeyRate>'
        "</diffgr:diffgram></KeyRateResult></KeyRateResponse></soap:Body></soap:Envelope>"
    ).encode("utf-8")


# ── Регресс: cbr.ru отдаёт записи в обратном хронологическом порядке ────────

def test_cbr_key_rate_reverse_chronological_order(monkeypatch):
    """Реальный формат ответа: новые даты первыми. Раньше rates[-1] брал
    САМУЮ СТАРУЮ ставку в диапазоне — регресс на этот баг (2026-07-27)."""
    import data.macro as macro

    raw = _soap_response([
        ("2026-07-27", "14.00"),
        ("2026-07-24", "14.25"),
        ("2026-06-19", "14.50"),
        ("2026-06-15", "14.50"),
    ])
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: raw)
    assert macro._fetch_cbr_key_rate() == 14.0


def test_cbr_key_rate_forward_chronological_order(monkeypatch):
    """Если формат вдруг изменится на прямой порядок — тоже корректно."""
    import data.macro as macro

    raw = _soap_response([
        ("2026-06-15", "14.50"),
        ("2026-06-19", "14.50"),
        ("2026-07-24", "14.25"),
        ("2026-07-27", "14.00"),
    ])
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: raw)
    assert macro._fetch_cbr_key_rate() == 14.0


def test_cbr_key_rate_comma_decimal(monkeypatch):
    """cbr.ru иногда отдаёт ставку через запятую (14,00 вместо 14.00)."""
    import data.macro as macro

    raw = _soap_response([("2026-07-27", "14,00")])
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: raw)
    assert macro._fetch_cbr_key_rate() == 14.0


def test_cbr_key_rate_no_data(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: _soap_response([]))
    assert macro._fetch_cbr_key_rate() is None


def test_cbr_key_rate_network_failure(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: None)
    assert macro._fetch_cbr_key_rate() is None


def test_cbr_key_rate_malformed_xml(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: b"not xml at all <<<")
    assert macro._fetch_cbr_key_rate() is None


# ── Фолбэк ставки на последнее известное значение ────────────────────────────

def test_cbr_rate_caches_successful_fetch(tmp_path, monkeypatch):
    """Успешная выборка кладётся в кэш — чтобы было чем фолбэчиться потом."""
    import data.macro as macro
    from data.store import load_macro_value

    db = tmp_path / "h.db"
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: _soap_response([("2026-09-05", "14.00")]))
    assert macro._cbr_key_rate(db_path=db) == 14.0

    cached = load_macro_value("cbr_rate", db_path=db)
    assert cached is not None
    assert cached[0] == 14.0


def test_cbr_rate_falls_back_to_cache_when_source_down(tmp_path, monkeypatch):
    """cbr.ru лежит → берём последнее известное значение, а не None."""
    import data.macro as macro
    from data.store import save_macro_value

    db = tmp_path / "h.db"
    save_macro_value("cbr_rate", 14.0, db_path=db)
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: None)   # источник недоступен
    assert macro._cbr_key_rate(db_path=db) == 14.0


def test_cbr_rate_ignores_stale_cache(tmp_path, monkeypatch):
    """Значение старше CBR_RATE_MAX_AGE_DAYS не используется — ставка могла смениться."""
    import sqlite3
    from datetime import timedelta

    import data.macro as macro
    from config import CBR_RATE_MAX_AGE_DAYS, today_msk
    from data.store import _connect, save_macro_value

    db = tmp_path / "h.db"
    save_macro_value("cbr_rate", 14.0, db_path=db)
    # Состариваем запись за пределы допустимого возраста
    stale = (today_msk() - timedelta(days=CBR_RATE_MAX_AGE_DAYS + 1)).strftime("%Y-%m-%d")
    conn = _connect(db)
    with conn:
        conn.execute("UPDATE macro_cache SET updated_at = ? WHERE key = 'cbr_rate'", (stale,))
    conn.close()

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: None)
    assert macro._cbr_key_rate(db_path=db) is None


def test_cbr_rate_no_cache_no_source_returns_none(tmp_path, monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: None)
    assert macro._cbr_key_rate(db_path=tmp_path / "empty.db") is None


def test_cbr_rate_fresh_value_overwrites_cache(tmp_path, monkeypatch):
    """Новая ставка затирает старую — фолбэк не залипает на устаревшем значении."""
    import data.macro as macro
    from data.store import load_macro_value, save_macro_value

    db = tmp_path / "h.db"
    save_macro_value("cbr_rate", 16.5, db_path=db)
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: _soap_response([("2026-09-05", "14.00")]))
    assert macro._cbr_key_rate(db_path=db) == 14.0
    assert load_macro_value("cbr_rate", db_path=db)[0] == 14.0
