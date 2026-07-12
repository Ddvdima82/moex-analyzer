"""
Delta-отчёт: сравнение двух последних прогонов.
Отправляет Telegram-уведомление при изменении сигналов и/или
технических алертах (RSI-экстремумы, пробой SMA200, всплеск объёма).
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EMOJI = {"BUY": "🟢BUY", "HOLD": "🟡HOLD", "SELL": "🔴SELL"}
_UPGRADES   = {"SELL→BUY", "SELL→HOLD", "HOLD→BUY"}
_DANGER     = {"BUY→SELL", "HOLD→SELL"}

# Пороги технических алертов. Срабатывают только на ПЕРЕХОДЕ через порог
# между прогонами — иначе один и тот же экстремум спамил бы каждый день.
_RSI_OVERSOLD = 25.0
_RSI_OVERBOUGHT = 75.0
_VOLUME_SPIKE_PCT = 100.0


def _indicators(row: dict[str, Any]) -> dict[str, Any]:
    """Индикаторы строки прогона: dict как есть или разбор indicators_json из SQLite."""
    raw = row.get("indicators") or row.get("indicators_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        logger.warning("Битый indicators_json для %s", row.get("ticker"))
        return {}


def _build_alerts(
    prev: dict[str, dict[str, Any]],
    curr: dict[str, dict[str, Any]],
) -> list[str]:
    """Технические алерты по переходам индикаторов между двумя прогонами."""
    alerts: list[str] = []
    for ticker in sorted(curr):
        p_ind = _indicators(prev.get(ticker, {}))
        c_ind = _indicators(curr[ticker])
        if not p_ind or not c_ind:
            continue  # старые строки без индикаторов — тихо пропускаем
        if p_ind.get("fallback") or c_ind.get("fallback"):
            continue  # нейтральные заглушки (сбой истории MOEX) — не рыночные данные

        p_rsi, c_rsi = p_ind.get("rsi"), c_ind.get("rsi")
        if p_rsi is not None and c_rsi is not None:
            if p_rsi > _RSI_OVERSOLD >= c_rsi:
                alerts.append(f"🚨 <b>{ticker}</b>: RSI {c_rsi:.0f} — экстремальная перепроданность")
            elif p_rsi < _RSI_OVERBOUGHT <= c_rsi:
                alerts.append(f"🔥 <b>{ticker}</b>: RSI {c_rsi:.0f} — перекупленность")

        p_above, c_above = p_ind.get("above_sma200"), c_ind.get("above_sma200")
        if p_above is not None and c_above is not None and p_above != c_above:
            if c_above:
                alerts.append(f"📈 <b>{ticker}</b>: цена пробила SMA200 вверх")
            else:
                alerts.append(f"📉 <b>{ticker}</b>: цена ушла под SMA200")

        p_vol = p_ind.get("volume_trend_pct")
        c_vol = c_ind.get("volume_trend_pct")
        if p_vol is not None and c_vol is not None and p_vol <= _VOLUME_SPIKE_PCT < c_vol:
            alerts.append(f"📊 <b>{ticker}</b>: всплеск объёма (+{c_vol:.0f}% к 30-дн. среднему)")

    return alerts


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
    """
    Сравнивает два прогона: изменения сигналов + технические алерты.
    Возвращает Telegram HTML или None если ни изменений, ни алертов нет.
    """
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

    alerts = _build_alerts(prev, curr)

    if not changes and not alerts:
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
        # «Ориентир», не «цель»: это σ-масштабированная статистическая оценка
        # на 4 недели, а не аналитический таргет (DCF/мультипликаторы)
        target_str = ""
        if ch["target"] and ch["price"]:
            upside = (ch["target"] / ch["price"] - 1) * 100
            sign = "+" if upside >= 0 else ""
            target_str = f" · ориентир {ch['target']:,.0f} ₽ ({sign}{upside:.1f}%)"

        from_lbl = _EMOJI.get(ch["from"], ch["from"])
        to_lbl   = _EMOJI.get(ch["to"],   ch["to"])
        return (
            f"{icon} <b>{ch['ticker']}</b> · {ch['company']}\n"
            f"   {from_lbl} → {to_lbl} · скор {score_str} · {price_str}{target_str}"
        )

    d1 = _fmt_date(prev_date)
    d2 = _fmt_date(curr_date)
    title = "Изменения сигналов" if changes else "Технические алерты"
    lines = [f"🔔 <b>{title}</b> {d2} vs {d1}\n"]

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

    if alerts:
        if changes:
            lines.append("")
        lines.append(f"<b>📟 Алерты ({len(alerts)})</b>")
        lines.extend(alerts)

    lines.append("\n<i>⚠️ Не является инвестиционной рекомендацией</i>")
    return "\n".join(lines)
