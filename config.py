"""
Конфигурация системы анализа акций Мосбиржи.
Ключи API хранятся в переменных окружения / GitHub Secrets.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

# Московское время (UTC+3, без перехода на летнее время с 2014).
# CI GitHub Actions работает в UTC — без этого дата отчёта «съезжает» у полуночи.
MSK = timezone(timedelta(hours=3))


def today_msk() -> date:
    """Текущая дата по московскому времени."""
    return datetime.now(MSK).date()

# ──────────────────────────────────────────────────────────────
# Ключи API (задаются через .env или GitHub Secrets)
# ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ──────────────────────────────────────────────────────────────
# Топ-20 акций Мосбиржи (обновлять раз в квартал)
# ──────────────────────────────────────────────────────────────
TOP20_TICKERS: list[str] = [
    "SBER", "LKOH", "GAZP", "NVTK", "GMKN",
    "ROSN", "YDEX", "TATN", "SNGS", "MTSS",
    "MOEX", "MGNT", "PLZL", "NLMK", "CHMF",
    "ALRS", "VTBR", "X5",  "AFLT", "PIKK",
]

# Названия компаний для поиска новостей
COMPANY_NAMES: dict[str, str] = {
    "SBER": "Сбербанк",
    "LKOH": "Лукойл",
    "GAZP": "Газпром",
    "NVTK": "Новатэк",
    "GMKN": "Норникель",
    "ROSN": "Роснефть",
    "YDEX": "Яндекс",
    "TATN": "Татнефть",
    "SNGS": "Сургутнефтегаз",
    "MTSS": "МТС",
    "MOEX": "Московская биржа",
    "MGNT": "Магнит",
    "PLZL": "Полюс",
    "NLMK": "НЛМК",
    "CHMF": "Северсталь",
    "ALRS": "Алроса",
    "VTBR": "ВТБ",
    "X5":   "X5 Retail Group",
    "AFLT": "Аэрофлот",
    "PIKK": "ПИК",
}

# ──────────────────────────────────────────────────────────────
# Веса трёх столпов анализа (сумма должна быть 1.0)
# ──────────────────────────────────────────────────────────────
WEIGHTS: dict[str, float] = {
    "fundamental": 0.35,
    "technical":   0.35,
    "sentiment":   0.30,
}

# ──────────────────────────────────────────────────────────────
# Пороги для торговых сигналов
# ──────────────────────────────────────────────────────────────
SIGNAL_THRESHOLDS: dict[str, int] = {
    "BUY":  70,   # score >= 70 → BUY
    "SELL": 30,   # score <= 30 → SELL, иначе HOLD
}

# ──────────────────────────────────────────────────────────────
# MOEX ISS API
# ──────────────────────────────────────────────────────────────
MOEX_BASE_URL: str = "https://iss.moex.com/iss"
MOEX_BOARD: str = "TQBR"  # Основной режим торгов
REQUEST_TIMEOUT: int = 30  # секунд
RETRY_COUNT: int = 3
RETRY_DELAY: int = 5       # секунд между попытками

# ──────────────────────────────────────────────────────────────
# Claude API
# ──────────────────────────────────────────────────────────────
CLAUDE_MODEL: str = "claude-sonnet-4-5"
# Максимальное число токенов для ответа
CLAUDE_MAX_TOKENS: int = 4096
# Таймаут запроса (сек) и число повторов. SDK сам ретраит 429/5xx с backoff.
CLAUDE_TIMEOUT: float = 120.0
CLAUDE_MAX_RETRIES: int = 3
# Параллелизм обработки тикеров (история + сентимент — I/O-bound).
# Держим умеренным, чтобы не упереться в rate-limit Claude и MOEX.
TICKER_MAX_WORKERS: int = 6

# ──────────────────────────────────────────────────────────────
# Пути к папкам
# ──────────────────────────────────────────────────────────────
import pathlib
BASE_DIR: pathlib.Path = pathlib.Path(__file__).parent
REPORTS_DIR: pathlib.Path = BASE_DIR / "reports"
LOGS_DIR: pathlib.Path = BASE_DIR / "logs"
FUNDAMENTALS_FILE: pathlib.Path = BASE_DIR / "data" / "fundamentals.json"
# Максимальный возраст фундаментальных данных (дни) — старше → предупреждение
FUNDAMENTALS_MAX_AGE_DAYS: int = 120
# SQLite-хранилище истории прогонов (для будущего бэктеста)
STORE_FILE: pathlib.Path = BASE_DIR / "data" / "history.db"

# Создаём папки если не существуют
REPORTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


# ──────────────────────────────────────────────────────────────
# Валидация конфигурации (вызывается на старте main)
# ──────────────────────────────────────────────────────────────

def validate_config() -> list[str]:
    """
    Проверяет внутреннюю согласованность конфигурации.
    Возвращает список предупреждений (нефатальных). При фатальной ошибке
    (битые веса/пороги) бросает ValueError — лучше упасть на старте, чем
    выдать молча неверные сигналы.
    """
    warnings: list[str] = []

    # Веса: неотрицательные и в сумме 1.0
    if any(w < 0 for w in WEIGHTS.values()):
        raise ValueError(f"WEIGHTS содержит отрицательные значения: {WEIGHTS}")
    total = sum(WEIGHTS.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"Сумма WEIGHTS должна быть 1.0, получено {total}")

    # Пороги сигналов: BUY строго выше SELL и оба в [0, 100]
    buy, sell = SIGNAL_THRESHOLDS["BUY"], SIGNAL_THRESHOLDS["SELL"]
    if not (0 <= sell < buy <= 100):
        raise ValueError(f"Некорректные SIGNAL_THRESHOLDS: BUY={buy}, SELL={sell}")

    if TICKER_MAX_WORKERS < 1:
        raise ValueError(f"TICKER_MAX_WORKERS должен быть >= 1, получено {TICKER_MAX_WORKERS}")

    # Нефатальные предупреждения о настройке окружения
    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY не задан — сентимент и отчёт пойдут в фолбэк")
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        warnings.append("Telegram не настроен — отчёт не будет отправлен")

    return warnings
