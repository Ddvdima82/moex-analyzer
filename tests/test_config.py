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


def test_today_msk_is_date():
    from datetime import date
    assert isinstance(config.today_msk(), date)
