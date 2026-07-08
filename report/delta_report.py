"""
Delta-отчёт: сравнение двух последних прогонов.
Отправляет Telegram-уведомление при изменении сигналов.
"""
from __future__ import annotations

from typing import Any

_EMOJI = {"BUY": "🟢BUY", "HOLD": "🟡HOLD", "SELL": "🔴SELL"}
_UPGRADES   = {"SELL→BUY", "SELL→HOLD", "HOLD→BUY"}
_DANGER     = {"BUY→SELL", "HOLD→SELL"}


def _fmt_date(d: str) -> str:
    """2026-07-08 → 08.07"""
    try:
        parts = d.split("-")
        return f"{parts[2]}.{parts[1]}"
    except Exception:
        return d


def build_delta_message(
    prev_rows: list[dict[str, Any]],
    curr_rows: list[dict[str, Any]],
    prev_date: str,
    curr_date: str,
) -> str | None:
    """Сравнивает два прогона. Возвращает Telegram HTML или None если изменений нет."""
    prev = {r["ticker"]: r for r in prev_rows}
    curr = {r["ticker"]: r for r in curr_rows}

    changes = []
    for ticker, c in curr.items():
        p = prev.get(ticker)
        if not p or p["signal"] == c["signal"]:
            continue
        changes.append({
            "ticker": ticker,
            "company": c.get("company", ticker),
            "from": p["signal"],
            "to": c["signal"],
            "score_prev": p.get("final_score"),
            "score_curr": c.get("final_score"),
            "price": c.get("price"),
            "target": c.get("target_price"),
            "upside": c.get("upside_pct"),
        })

    if not changes:
        return None

    upgrades = [ch for ch in changes if f"{ch['from']}→{ch['to']}" in _UPGRADES]
    dangers  = [ch for ch in changes if f"{ch['from']}→{ch['to']}" in _DANGER]
    others   = [ch for ch in changes if ch not in upgrades and ch not in dangers]

    def _fmt_change(ch: dict) -> str:
        key = f"{ch['from']}→{ch['to']}"
        icon = "🚀" if key == "SELL→BUY" else ("📈" if "BUY" in ch["to"] else ("📉" if key == "BUY→SELL" else "⬇️"))
        if key in _DANGER:
            icon = "📉" if key == "BUY→SELL" else "⚠️"

        sp = ch["score_prev"]
        sc = ch["score_curr"]
        if sp is not None and sc is not None:
            delta = sc - sp
            delta_str = f"+{delta:.0f}" if delta >= 0 else f"{delta:.0f}"
            score_str = f"{sp:.0f}→{sc:.0f} ({delta_str})"
        else:
            score_str = f"{sc:.0f}/100" if sc is not None else "—"

        price_str = f"{ch['price']:,.0f} ₽" if ch["price"] else "—"
        target_str = ""
        if ch["target"] and ch["price"]:
            upside = (ch["target"] / ch["price"] - 1) * 100
            sign = "+" if upside >= 0 else ""
            target_str = f" · цель {ch['target']:,.0f} ₽ ({sign}{upside:.1f}%)"

        from_lbl = _EMOJI.get(ch["from"], ch["from"])
        to_lbl   = _EMOJI.get(ch["to"],   ch["to"])
        return (
            f"{icon} <b>{ch['ticker']}</b> · {ch['company']}\n"
            f"   {from_lbl} → {to_lbl} · скор {score_str} · {price_str}{target_str}"
        )

    d1 = _fmt_date(prev_date)
    d2 = _fmt_date(curr_date)
    lines = [f"🔔 <b>Изменения сигналов</b> {d2} vs {d1}\n"]

    if upgrades:
        lines.append(f"<b>📈 Апгрейды ({len(upgrades)})</b>")
        lines.extend(_fmt_change(ch) for ch in sorted(upgrades, key=lambda x: x["ticker"]))

    if dangers:
        if upgrades:
            lines.append("")
        lines.append(f"<b>⚠️ Риски ({len(dangers)})</b>")
        lines.extend(_fmt_change(ch) for ch in sorted(dangers, key=lambda x: x["ticker"]))

    if others:
        if upgrades or dangers:
            lines.append("")
        lines.append(f"<b>⬇️ Прочие изменения ({len(others)})</b>")
        lines.extend(_fmt_change(ch) for ch in sorted(others, key=lambda x: x["ticker"]))

    lines.append("\n<i>⚠️ Не является инвестиционной рекомендацией</i>")
    return "\n".join(lines)
