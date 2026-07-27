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
    assert macro._cbr_key_rate() == 14.0


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
    assert macro._cbr_key_rate() == 14.0


def test_cbr_key_rate_comma_decimal(monkeypatch):
    """cbr.ru иногда отдаёт ставку через запятую (14,00 вместо 14.00)."""
    import data.macro as macro

    raw = _soap_response([("2026-07-27", "14,00")])
    monkeypatch.setattr(macro, "_get", lambda *a, **kw: raw)
    assert macro._cbr_key_rate() == 14.0


def test_cbr_key_rate_no_data(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: _soap_response([]))
    assert macro._cbr_key_rate() is None


def test_cbr_key_rate_network_failure(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: None)
    assert macro._cbr_key_rate() is None


def test_cbr_key_rate_malformed_xml(monkeypatch):
    import data.macro as macro

    monkeypatch.setattr(macro, "_get", lambda *a, **kw: b"not xml at all <<<")
    assert macro._cbr_key_rate() is None
