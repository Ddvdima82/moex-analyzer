"""
Автономная самокалибровка весов WEIGHTS по накопленной статистике прогонов.

Идея: у какого столпа (fundamental/technical/sentiment) историческая
корреляция его скора с реализованной форвардной доходностью выше — тому
столпу и должен доставаться больший вес. Никакого одобрения человеком не
требуется — это сознательный выбор (см. обсуждение при внедрении), но с
жёсткими предохранителями, потому что весами управляется реальный сигнал:

  • MIN_OBSERVATIONS — ниже этого числа наблюдений калибровка вообще не
    считается (шум малой выборки хуже отсутствия калибровки).
  • MIN_RECALIBRATION_INTERVAL_DAYS — троттлинг: не чаще раза в N дней,
    иначе веса дёргались бы от каждого дневного прогона.
  • LEARNING_RATE — обновление ДЕМПФИРОВАННОЕ (доля пути к целевым весам за
    цикл), а не мгновенный скачок к «оптимуму» одной выборки.
  • WEIGHT_FLOOR — ни один столп не может провалиться в 0 от шума; система
    контрарно-мультифакторная по дизайну, полное отключение столпа — решение,
    которое не должно приниматься автоматически.

Результат пишется в data/calibration.json — тот же класс сгенерированного
артефакта, что data/history.db (гитигнорится, кэшируется в CI между
прогонами). config.py читает его при импорте с фолбэком на хардкод-дефолты
при отсутствии файла или проваленной валидации.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import pandas as pd

from config import CALIBRATION_FILE
from config import WEIGHTS as _CURRENT_WEIGHTS
from config import today_msk

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS = 60
MIN_RECALIBRATION_INTERVAL_DAYS = 7
LEARNING_RATE = 0.2
WEIGHT_FLOOR = 0.10
PILLARS = ("fundamental", "technical", "sentiment")


def _target_weights(resolved: list[dict[str, Any]]) -> dict[str, float] | None:
    """
    Целевые веса по Pearson-корреляции столп↔форвардная доходность.
    Отрицательная/нулевая корреляция клампится в 0 — недоказанный сигнал не
    получает бонус, но и не наказывается сверх этого. WEIGHT_FLOOR не даёт
    столпу свалиться в 0 от шума малой выборки. None — калибровка невозможна
    (мало наблюдений или ни один столп не показал предсказательной силы).
    """
    rows = [
        {**r["scores"], "fwd_return": r["fwd_return"]}
        for r in resolved
        if r.get("scores") and all(p in r["scores"] for p in PILLARS)
    ]
    if len(rows) < MIN_OBSERVATIONS:
        return None

    df = pd.DataFrame(rows)
    corr = {p: df[p].corr(df["fwd_return"]) for p in PILLARS}
    if any(pd.isna(v) for v in corr.values()):
        return None

    clamped = {p: max(0.0, v) for p, v in corr.items()}
    total = sum(clamped.values())
    if total <= 0:
        # Ни один столп не подтвердил предсказательную силу — не гадаем,
        # оставляем текущие веса как есть.
        return None

    raw = {p: clamped[p] / total for p in PILLARS}
    # Пол + доля ОСТАВШЕГОСЯ бюджета (1 − n·floor), а не floor-then-renormalize:
    # деление суммы флорированных значений на их же сумму способно вернуть
    # значение НИЖЕ пола (нашлось тестом) — эта формула держит пол ровно.
    remaining = 1.0 - len(PILLARS) * WEIGHT_FLOOR
    return {p: WEIGHT_FLOOR + remaining * raw[p] for p in PILLARS}


def _damped_update(current: dict[str, float], target: dict[str, float]) -> dict[str, float]:
    """
    Шаг LEARNING_RATE к целевым весам, а не мгновенный скачок. Без округления
    здесь: config.validate_config() требует |сумма−1.0| < 1e-6, а округление
    каждого веса независимо до N знаков это условие ломает (нашлось тестом).
    """
    updated = {p: current[p] + LEARNING_RATE * (target[p] - current[p]) for p in PILLARS}
    total = sum(updated.values())
    return {p: updated[p] / total for p in PILLARS}


def _load_existing() -> dict[str, Any] | None:
    try:
        if CALIBRATION_FILE.exists():
            return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("calibration.json повреждён, игнорируем: %s", exc)
    return None


def compute_and_save_calibration(
    horizon_days: int = 28,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    Считает и (если прошло достаточно времени и данных) сохраняет
    откалиброванные веса в data/calibration.json. Возвращает записанный
    словарь, либо None если калибровка пропущена в этот раз (рано по
    троттлингу, мало наблюдений, либо ни один столп не подтвердил силу).
    force=True игнорирует троттлинг по времени (ручной запуск/тесты).
    """
    existing = _load_existing()
    if existing and not force:
        last = str(existing.get("computed_at", ""))[:10]
        try:
            days_since = (today_msk() - date.fromisoformat(last)).days
            if days_since < MIN_RECALIBRATION_INTERVAL_DAYS:
                logger.info(
                    "Калибровка весов: %d дн. с прошлой (< %d) — пропуск",
                    days_since, MIN_RECALIBRATION_INTERVAL_DAYS,
                )
                return None
        except ValueError:
            pass  # битая дата в файле — не блокируем калибровку из-за этого

    from backtest import _load_resolved_runs
    resolved = _load_resolved_runs(horizon_days=horizon_days)
    target = _target_weights(resolved)
    if target is None:
        logger.info(
            "Калибровка весов: недостаточно данных или сигнала (наблюдений=%d, нужно %d)",
            len(resolved), MIN_OBSERVATIONS,
        )
        return None

    current = dict(existing["weights"]) if existing and "weights" in existing else dict(_CURRENT_WEIGHTS)
    new_weights = _damped_update(current, target)

    payload = {
        "weights": new_weights,
        "computed_at": today_msk().isoformat(),
        "sample_size": len(resolved),
        "previous_weights": current,
        "target_weights_uncapped": {p: round(v, 4) for p, v in target.items()},
    }
    try:
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Калибровка весов обновлена: %s (выборка=%d)", new_weights, len(resolved))
    except Exception as exc:
        logger.error("Не удалось сохранить calibration.json: %s", exc)
        return None
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = compute_and_save_calibration(force=True)
    if result is None:
        print("Калибровка не выполнена (недостаточно данных или сигнала).")
    else:
        print(f"Новые веса: {result['weights']}")
        print(f"Было: {result['previous_weights']}")
        print(f"Целевые (без демпфирования): {result['target_weights_uncapped']}")
        print(f"Выборка: {result['sample_size']} наблюдений")
