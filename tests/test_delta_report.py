"""Тесты delta-отчёта: изменения сигналов и технические алерты."""
import json

from report.delta_report import build_delta_message


def _row(ticker="SBER", signal="BUY", score=70.0, indicators=None):
    """Строка прогона как из SQLite (indicators_json — сериализованная строка)."""
    return {
        "ticker": ticker, "company": "Компания", "price": 300.0,
        "final_score": score, "signal": signal, "target_price": 330.0,
        "upside_pct": 10.0,
        "indicators_json": json.dumps(indicators) if indicators is not None else None,
    }


def test_no_changes_no_alerts_returns_none():
    prev = [_row(signal="HOLD")]
    curr = [_row(signal="HOLD")]
    assert build_delta_message(prev, curr, "2026-07-01", "2026-07-02") is None


def test_signal_change_reported():
    prev = [_row(signal="HOLD", score=55.0)]
    curr = [_row(signal="BUY", score=65.0)]
    msg = build_delta_message(prev, curr, "2026-07-01", "2026-07-02")
    assert msg and "SBER" in msg and "Апгрейды" in msg
    # Честная терминология: «ориентир», не «цель»
    assert "ориентир" in msg and "цель" not in msg


def test_rsi_oversold_alert_on_transition():
    prev = [_row(indicators={"rsi": 32.0})]
    curr = [_row(indicators={"rsi": 22.0})]
    msg = build_delta_message(prev, curr, "2026-07-01", "2026-07-02")
    assert msg and "перепроданность" in msg and "Алерты" in msg


def test_rsi_alert_not_repeated_while_extreme():
    """RSI остаётся в зоне — повторного алерта нет (только переход через порог)."""
    prev = [_row(indicators={"rsi": 22.0})]
    curr = [_row(indicators={"rsi": 20.0})]
    assert build_delta_message(prev, curr, "2026-07-01", "2026-07-02") is None


def test_sma200_cross_alert():
    prev = [_row(indicators={"above_sma200": True})]
    curr = [_row(indicators={"above_sma200": False})]
    msg = build_delta_message(prev, curr, "2026-07-01", "2026-07-02")
    assert msg and "SMA200" in msg


def test_volume_spike_alert():
    prev = [_row(indicators={"volume_trend_pct": 40.0})]
    curr = [_row(indicators={"volume_trend_pct": 150.0})]
    msg = build_delta_message(prev, curr, "2026-07-01", "2026-07-02")
    assert msg and "объём" in msg.lower()


def test_fallback_indicators_do_not_alert():
    """Заглушки _empty_indicators (сбой истории MOEX) — не рыночные данные."""
    prev = [_row(indicators={"rsi": 22.0, "above_sma200": True, "fallback": False})]
    curr = [_row(indicators={"rsi": 50.0, "above_sma200": False, "fallback": True})]
    assert build_delta_message(prev, curr, "2026-07-01", "2026-07-02") is None


def test_old_rows_without_indicators_are_silent():
    """Строки старой схемы (indicators_json=None) не дают ни алертов, ни падений."""
    prev = [_row(indicators=None)]
    curr = [_row(indicators={"rsi": 20.0})]
    assert build_delta_message(prev, curr, "2026-07-01", "2026-07-02") is None


def test_alerts_appended_to_signal_changes():
    prev = [_row(signal="HOLD", indicators={"rsi": 40.0}),
            _row(ticker="GAZP", signal="HOLD", indicators={"above_sma200": False})]
    curr = [_row(signal="BUY", indicators={"rsi": 38.0}),
            _row(ticker="GAZP", signal="HOLD", indicators={"above_sma200": True})]
    msg = build_delta_message(prev, curr, "2026-07-01", "2026-07-02")
    assert msg and "Изменения сигналов" in msg
    assert "GAZP" in msg and "SMA200" in msg
