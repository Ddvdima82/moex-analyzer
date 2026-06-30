"""
Delta-отчёт: сравнение двух последних прогонов.
Отправляет Telegram-уведомление при изменении сигналов.
"""
from __future__ import annotations

from typing import Any

_EMOJI = {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}
_ARROW = {
    "BUY→SELL": "📉", "SELL→BUY": "🚀",
    "HOLD→BUY": "📈", "BUY→HOLD": "⬇️",
    "HOLD→SELL": "⚠️", "SELL→HOLD": "↗️",
}


def build_delta_message(
    prev_rows: list[dict[str, Any]],
    curr_rows: list[dict[str, Any]],
    prev_date: str,
    curr_date: str,
) -> str | None:
    """
    Сравнивает два прогона. Возвращает Telegram HTML или None если изменений нет.
    """
    prev = {r["ticker"]: r for r in prev_rows}
    curr = {r["ticker"]: r for r in curr_rows}

    changes = []
    for ticker, c in curr.items():
        p = prev.get(ticker)
        if p and p["signal"] != c["signal"]:
            changes.append({
                "ticker": ticker,
                "company": c.get("company", ticker),
                "from": p["signal"],
                "to": c["signal"],
                "score": c.get("final_score"),
                "price": c.get("price"),
            })

    if not changes:
        return None

    # BUY-связанные изменения первыми
    changes.sort(key=lambda x: (0 if "BUY" in (x["from"], x["to"]) else 1, x["ticker"]))

    lines = [f"🔔 <b>Изменения сигналов</b> ({curr_date} vs {prev_date})\n"]
    for ch in changes:
        key = f"{ch['from']}→{ch['to']}"
        arrow = _ARROW.get(key, "→")
        fe = _EMOJI.get(ch["from"], "⚪")
        te = _EMOJI.get(ch["to"], "⚪")
        score_str = f"{ch['score']:.0f}" if ch["score"] is not None else "—"
        price_str = f"{ch['price']:,.0f} ₽" if ch["price"] is not None else "—"
        lines.append(
            f"{arrow} <b>{ch['ticker']}</b> {ch['company']} "
            f"{fe}{ch['from']} → {te}{ch['to']} · {score_str}/100 · {price_str}"
        )

    lines.append("\n⚠️ <i>Не является инвестиционной рекомендацией</i>")
    return "\n".join(lines)
