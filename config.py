"""
Конфигурация системы анализа акций Мосбиржи.
Ключи API хранятся в переменных окружения / GitHub Secrets.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone


def _load_local_env() -> None:
    """
    Подхватывает .env рядом с config.py (KEY=VALUE) в окружение для локальных
    запусков. Без зависимостей. Реальное окружение / GitHub Secrets имеют
    приоритет (setdefault не перезаписывает уже заданные переменные).
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_local_env()

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
# Боевая отправка в Telegram — только при явном разрешении (ставится в CI).
# Локальный прогон без флага печатает отчёт в консоль и не дублирует канал.
TELEGRAM_ENABLED: bool = os.environ.get("TELEGRAM_ENABLED", "").lower() in ("1", "true", "yes")
# Gemini (Google) — дешёвый сентимент со встроенным поиском. SDK читает GEMINI_API_KEY.
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
GEMINI_PROXY: str = os.environ.get("GEMINI_PROXY", "")

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
    "BUY":  60,   # score >= 60 → BUY
    "SELL": 35,   # score <= 35 → SELL, иначе HOLD
}
# Гистерезис сигналов: вход в BUY/SELL по основному порогу, выход — только
# когда скор отходит от порога дальше этой величины (BUY держится до <56).
# Убирает хлопанье BUY↔HOLD при дрожании скора на 1–2 пункта (шум сентимента).
SIGNAL_HYSTERESIS: float = 4.0
# Режимный фильтр рынка: при медвежьем режиме (IMOEX ниже своей SMA200)
# порог входа в BUY поднимается на эту величину. Система контрарная
# (mean-reversion), и в системном обвале «перепродано» — не повод покупать:
# фильтр требует от бумаги заметно больше качества, чтобы ловить нож.
BEAR_BUY_EXTRA: float = 5.0
# Множители BEAR_BUY_EXTRA по силе тренда IMOEX (ADX): слабый тренд/боковик —
# контрарная ставка всё ещё разумна (меньше надбавки), сильный подтверждённый
# тренд — заметно опаснее (больше надбавки). Moderate/unknown → множитель 1.0.
BEAR_TREND_WEAK_MULT: float = 0.4
BEAR_TREND_STRONG_MULT: float = 2.0
# ADX-демпфер контрарной 52w-компоненты технического скора на уровне ОТДЕЛЬНОЙ
# бумаги (не индекса): подтверждённый нисходящий тренд (−DI>+DI) снижает вес
# ставки на отскок — сильнее при сильном тренде, слабее при умеренном.
ADX_STRONG_DOWNTREND_DAMPEN: float = 0.3
ADX_MODERATE_DOWNTREND_DAMPEN: float = 0.65
# Маркер «скорая отсечка» в отчёте: ex-date в пределах стольких дней
EX_DATE_SOON_DAYS: int = 7

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
# Провайдер сентимент-анализа: gemini (дёшево, поиск Google) | anthropic | none
SENTIMENT_PROVIDER: str = os.environ.get("SENTIMENT_PROVIDER", "gemini").lower()
# Модель Gemini для сентимента (flash — дешёвый, есть бесплатный tier).
# 2.5-flash: устоявшийся free-tier + grounding. Переопределяется GEMINI_MODEL.
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Free-tier Gemini жёстко лимитирует RPM (особенно grounding). Ограничиваем
# число одновременных вызовов и ретраим 429 с backoff, чтобы не ловить фолбэк.
GEMINI_CONCURRENCY: int = int(os.environ.get("GEMINI_CONCURRENCY", "2"))
GEMINI_MAX_RETRIES: int = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))
GEMINI_RETRY_DELAY: float = float(os.environ.get("GEMINI_RETRY_DELAY", "6"))

CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
# Генерировать отчёт через Claude (дорого). False → всегда fallback-отчёт.
USE_CLAUDE_REPORT: bool = os.environ.get("USE_CLAUDE_REPORT", "false").lower() == "true"
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
# Жёсткий порог устаревания (дни): старше → фундаментальный столп ИСКЛЮЧАЕТСЯ
# из финального скора (перенормировка весов, как при фолбэке). Два пропущенных
# квартальных обновления — данные уже не отражают отчётность.
FUNDAMENTALS_STALE_DAYS: int = 240
# Порог дневного разрыва CLOSE (доля цены) для детекции сплита/корпособытия.
# Выше любого дивидендного гэпа (<20%), но ловит сплиты (VTBR 5000:1 в 2024).
PRICE_GAP_THRESHOLD: float = 0.40
# SQLite-хранилище истории прогонов (для будущего бэктеста)
STORE_FILE: pathlib.Path = BASE_DIR / "data" / "history.db"
# Самодостаточный HTML-дашборд (публикуется на GitHub Pages из docs/)
DASHBOARD_FILE: pathlib.Path = BASE_DIR / "docs" / "index.html"

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
    # Полосы гистерезиса не должны перекрываться, иначе BUY и SELL спорят за скор
    if SIGNAL_HYSTERESIS < 0 or buy - SIGNAL_HYSTERESIS <= sell + SIGNAL_HYSTERESIS:
        raise ValueError(
            f"Некорректный SIGNAL_HYSTERESIS={SIGNAL_HYSTERESIS} для порогов BUY={buy}/SELL={sell}"
        )
    # Сдвинутый bear-порог BUY должен оставаться валидным даже в худшем случае
    # (сильный тренд, множитель BEAR_TREND_STRONG_MULT)
    if BEAR_BUY_EXTRA < 0 or BEAR_TREND_WEAK_MULT < 0 or BEAR_TREND_STRONG_MULT < 0:
        raise ValueError(
            f"BEAR_BUY_EXTRA/множители силы тренда не могут быть отрицательными: "
            f"extra={BEAR_BUY_EXTRA}, weak_mult={BEAR_TREND_WEAK_MULT}, strong_mult={BEAR_TREND_STRONG_MULT}"
        )
    worst_case_extra = BEAR_BUY_EXTRA * max(BEAR_TREND_WEAK_MULT, BEAR_TREND_STRONG_MULT, 1.0)
    if buy + worst_case_extra > 100:
        raise ValueError(
            f"Некорректный BEAR_BUY_EXTRA={BEAR_BUY_EXTRA} с множителями: "
            f"BUY+extra={buy + worst_case_extra} выходит за 100"
        )
    for name, dampen in (
        ("ADX_STRONG_DOWNTREND_DAMPEN", ADX_STRONG_DOWNTREND_DAMPEN),
        ("ADX_MODERATE_DOWNTREND_DAMPEN", ADX_MODERATE_DOWNTREND_DAMPEN),
    ):
        if not (0.0 <= dampen <= 1.0):
            raise ValueError(f"{name}={dampen} должен быть в [0, 1]")

    if TICKER_MAX_WORKERS < 1:
        raise ValueError(f"TICKER_MAX_WORKERS должен быть >= 1, получено {TICKER_MAX_WORKERS}")

    # Нефатальные предупреждения о настройке окружения.
    # Ключ проверяем у АКТИВНОГО провайдера сентимента — предупреждение про
    # неиспользуемый ключ только маскирует реальную проблему.
    if SENTIMENT_PROVIDER == "gemini" and not GEMINI_API_KEY:
        warnings.append("GEMINI_API_KEY не задан — сентимент уйдёт в нейтральный фолбэк")
    if SENTIMENT_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY не задан — сентимент уйдёт в нейтральный фолбэк")
    if USE_CLAUDE_REPORT and not ANTHROPIC_API_KEY:
        warnings.append("USE_CLAUDE_REPORT=true, но ANTHROPIC_API_KEY не задан — отчёт будет программным")
    if not TELEGRAM_ENABLED:
        warnings.append("TELEGRAM_ENABLED не задан — отчёты только в консоль/лог (боевая отправка из CI)")
    elif not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        warnings.append("Telegram не настроен — отчёт не будет отправлен")

    return warnings
