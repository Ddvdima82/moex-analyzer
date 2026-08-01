"""Тесты валидации конфигурации (config.validate_config)."""
import pytest

import config


def test_validate_config_ok():
    # Дефолтная конфигурация валидна (возвращает только нефатальные предупреждения)
    warns = config.validate_config()
    assert isinstance(warns, list)


def test_weights_sum_to_one():
    assert abs(sum(config.WEIGHTS.values()) - 1.0) < 1e-6


def test_thresholds_ordered():
    assert config.SIGNAL_THRESHOLDS["SELL"] < config.SIGNAL_THRESHOLDS["BUY"]


def test_validate_rejects_bad_weights(monkeypatch):
    monkeypatch.setattr(config, "WEIGHTS",
                        {"fundamental": 0.5, "technical": 0.5, "sentiment": 0.5})
    with pytest.raises(ValueError):
        config.validate_config()


def test_validate_rejects_negative_weight(monkeypatch):
    monkeypatch.setattr(config, "WEIGHTS",
                        {"fundamental": -0.1, "technical": 0.6, "sentiment": 0.5})
    with pytest.raises(ValueError):
        config.validate_config()


def test_validate_rejects_bad_thresholds(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_THRESHOLDS", {"BUY": 30, "SELL": 70})
    with pytest.raises(ValueError):
        config.validate_config()


def test_validate_rejects_bear_extra_overflow(monkeypatch):
    """BUY + BEAR_BUY_EXTRA*STRONG_MULT не должен вылезать за 100."""
    monkeypatch.setattr(config, "SIGNAL_THRESHOLDS", {"BUY": 95, "SELL": 10})
    with pytest.raises(ValueError):
        config.validate_config()


def test_validate_rejects_negative_trend_mult(monkeypatch):
    monkeypatch.setattr(config, "BEAR_TREND_STRONG_MULT", -1.0)
    with pytest.raises(ValueError):
        config.validate_config()


def test_validate_rejects_dampen_out_of_range(monkeypatch):
    monkeypatch.setattr(config, "ADX_STRONG_DOWNTREND_DAMPEN", 1.5)
    with pytest.raises(ValueError):
        config.validate_config()


def test_today_msk_is_date():
    from datetime import date
    assert isinstance(config.today_msk(), date)


# ── _load_calibrated_weights: оверлей калибровки с фолбэком на дефолт ────────

_DEFAULT = {"fundamental": 0.35, "technical": 0.35, "sentiment": 0.30}


def test_load_calibrated_weights_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CALIBRATION_FILE", tmp_path / "missing.json")
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT


def test_load_calibrated_weights_valid_file(tmp_path, monkeypatch):
    import json
    f = tmp_path / "calibration.json"
    calibrated = {"fundamental": 0.20, "technical": 0.50, "sentiment": 0.30}
    f.write_text(json.dumps({"weights": calibrated}), encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == calibrated


def test_load_calibrated_weights_corrupted_json_falls_back(tmp_path, monkeypatch):
    f = tmp_path / "calibration.json"
    f.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT


def test_load_calibrated_weights_wrong_keys_falls_back(tmp_path, monkeypatch):
    import json
    f = tmp_path / "calibration.json"
    f.write_text(json.dumps({"weights": {"fundamental": 0.5, "technical": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT


def test_load_calibrated_weights_negative_value_falls_back(tmp_path, monkeypatch):
    import json
    f = tmp_path / "calibration.json"
    bad = {"fundamental": -0.1, "technical": 0.6, "sentiment": 0.5}
    f.write_text(json.dumps({"weights": bad}), encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT


def test_load_calibrated_weights_sum_not_one_falls_back(tmp_path, monkeypatch):
    import json
    f = tmp_path / "calibration.json"
    bad = {"fundamental": 0.5, "technical": 0.5, "sentiment": 0.5}
    f.write_text(json.dumps({"weights": bad}), encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT


def test_load_calibrated_weights_missing_weights_key_falls_back(tmp_path, monkeypatch):
    import json
    f = tmp_path / "calibration.json"
    f.write_text(json.dumps({"computed_at": "2026-01-01"}), encoding="utf-8")
    monkeypatch.setattr(config, "CALIBRATION_FILE", f)
    assert config._load_calibrated_weights(_DEFAULT) == _DEFAULT
