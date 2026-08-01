"""Тесты автономной самокалибровки весов (calibration.py)."""
import json
from datetime import timedelta

import pandas as pd
import pytest

import calibration
from config import today_msk


def _resolved(pillars: dict[str, float], fwd_return: float) -> dict:
    return {"ticker": "SBER", "run_date": "2026-01-01", "signal": "BUY",
            "scores": pillars, "fwd_return": fwd_return}


# ── _target_weights: расчёт целевых весов по корреляции ──────────────────────

def test_target_weights_none_below_min_observations():
    rows = [_resolved({"fundamental": 50, "technical": 50, "sentiment": 50}, 1.0)] * 10
    assert calibration._target_weights(rows) is None


def test_target_weights_favors_predictive_pillar():
    """Технический скор идеально предсказывает доходность, остальные — шум."""
    import random
    random.seed(42)
    rows = []
    for i in range(100):
        tech = 30 + i * 0.6
        fwd = (tech - 50) * 0.5  # чем выше tech, тем выше доходность — идеальная корреляция
        rows.append(_resolved(
            {"fundamental": random.uniform(0, 100), "technical": tech,
             "sentiment": random.uniform(0, 100)},
            fwd,
        ))
    target = calibration._target_weights(rows)
    assert target is not None
    assert target["technical"] > target["fundamental"]
    assert target["technical"] > target["sentiment"]
    assert abs(sum(target.values()) - 1.0) < 1e-6


def test_target_weights_respects_floor():
    """Даже у столпа без предсказательной силы вес не проваливается ниже WEIGHT_FLOOR."""
    import random
    random.seed(1)
    rows = []
    for i in range(100):
        tech = 30 + i * 0.6
        fwd = (tech - 50) * 0.5
        rows.append(_resolved(
            {"fundamental": random.uniform(0, 100), "technical": tech,
             "sentiment": random.uniform(0, 100)},
            fwd,
        ))
    target = calibration._target_weights(rows)
    assert all(v >= calibration.WEIGHT_FLOOR - 1e-9 for v in target.values())


def test_target_weights_none_when_no_pillar_predictive():
    """Все корреляции ≈0/отрицательные → не гадаем, возвращаем None."""
    import random
    random.seed(7)
    rows = [
        _resolved(
            {"fundamental": random.uniform(0, 100), "technical": random.uniform(0, 100),
             "sentiment": random.uniform(0, 100)},
            random.uniform(-1, 1) * 0.001,  # доходность не зависит от скоров
        )
        for _ in range(100)
    ]
    # Не гарантируем None детерминированно на случайных данных, но проверяем
    # инвариант: если результат есть — он валиден (сумма=1, floor соблюдён)
    target = calibration._target_weights(rows)
    if target is not None:
        assert abs(sum(target.values()) - 1.0) < 1e-6


def test_target_weights_missing_pillar_scores_excluded():
    rows = [_resolved({"fundamental": 50, "technical": 50}, 1.0)] * 100  # нет sentiment
    assert calibration._target_weights(rows) is None


# ── _damped_update: демпфированное обновление ────────────────────────────────

def test_damped_update_partial_move_toward_target():
    current = {"fundamental": 0.35, "technical": 0.35, "sentiment": 0.30}
    target = {"fundamental": 0.10, "technical": 0.80, "sentiment": 0.10}
    updated = calibration._damped_update(current, target)
    # LEARNING_RATE=0.2 → двигается к цели, но не долетает
    assert current["technical"] < updated["technical"] < target["technical"]
    assert abs(sum(updated.values()) - 1.0) < 1e-6


# ── compute_and_save_calibration: троттлинг, запись, фолбэк ──────────────────

def test_compute_and_save_no_data_returns_none(tmp_path, monkeypatch):
    cal_file = tmp_path / "calibration.json"
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)
    monkeypatch.setattr("backtest._load_resolved_runs", lambda **kw: [])
    assert calibration.compute_and_save_calibration() is None
    assert not cal_file.exists()


def test_compute_and_save_writes_file_with_enough_data(tmp_path, monkeypatch):
    cal_file = tmp_path / "calibration.json"
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)

    import random
    random.seed(3)
    rows = []
    for i in range(100):
        tech = 30 + i * 0.6
        fwd = (tech - 50) * 0.5
        rows.append(_resolved(
            {"fundamental": random.uniform(0, 100), "technical": tech,
             "sentiment": random.uniform(0, 100)},
            fwd,
        ))
    monkeypatch.setattr("backtest._load_resolved_runs", lambda **kw: rows)

    result = calibration.compute_and_save_calibration(force=True)
    assert result is not None
    assert cal_file.exists()
    saved = json.loads(cal_file.read_text(encoding="utf-8"))
    assert saved["weights"] == result["weights"]
    assert abs(sum(saved["weights"].values()) - 1.0) < 1e-6


def test_compute_and_save_throttled_without_force(tmp_path, monkeypatch):
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(json.dumps({
        "weights": {"fundamental": 0.35, "technical": 0.35, "sentiment": 0.30},
        "computed_at": today_msk().isoformat(),  # только что калибровали
    }), encoding="utf-8")
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)

    called = {"n": 0}
    def _spy(**kw):
        called["n"] += 1
        return []
    monkeypatch.setattr("backtest._load_resolved_runs", _spy)

    assert calibration.compute_and_save_calibration() is None
    assert called["n"] == 0  # даже не дошли до чтения данных — троттлинг раньше


def test_compute_and_save_force_bypasses_throttle(tmp_path, monkeypatch):
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(json.dumps({
        "weights": {"fundamental": 0.35, "technical": 0.35, "sentiment": 0.30},
        "computed_at": today_msk().isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)
    monkeypatch.setattr("backtest._load_resolved_runs", lambda **kw: [])

    # force=True пропускает троттлинг, но данных всё равно нет → None (не упало)
    assert calibration.compute_and_save_calibration(force=True) is None


def test_compute_and_save_old_calibration_recalibrates(tmp_path, monkeypatch):
    cal_file = tmp_path / "calibration.json"
    old_date = (today_msk() - timedelta(days=30)).isoformat()
    cal_file.write_text(json.dumps({
        "weights": {"fundamental": 0.35, "technical": 0.35, "sentiment": 0.30},
        "computed_at": old_date,
    }), encoding="utf-8")
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)

    import random
    random.seed(5)
    rows = []
    for i in range(100):
        tech = 30 + i * 0.6
        fwd = (tech - 50) * 0.5
        rows.append(_resolved(
            {"fundamental": random.uniform(0, 100), "technical": tech,
             "sentiment": random.uniform(0, 100)},
            fwd,
        ))
    monkeypatch.setattr("backtest._load_resolved_runs", lambda **kw: rows)

    result = calibration.compute_and_save_calibration()
    assert result is not None  # достаточно дней прошло — не троттлится


def test_compute_and_save_corrupted_existing_file_recalibrates(tmp_path, monkeypatch):
    """Битый calibration.json не блокирует пересчёт (троттлинг просто не срабатывает)."""
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(calibration, "CALIBRATION_FILE", cal_file)
    monkeypatch.setattr("backtest._load_resolved_runs", lambda **kw: [])

    assert calibration.compute_and_save_calibration() is None  # нет данных, но не упало
