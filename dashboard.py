"""
Генератор самодостаточного HTML-дашборда (публикуется на GitHub Pages).

Собирает три среза:
  • текущий прогон (таблица топ-20 + разбивка по трём столпам, индикаторы);
  • история трендов из SQLite (динамика скора/сигналов по неделям);
  • бэктест (hit-rate сигналов), если в БД накопилось ≥2 прогонов.

Один HTML-файл с встроенными данными (JSON) и клиентским JS — без сервера.
Графики через Chart.js (CDN).

Запуск: python dashboard.py [--out PATH]  — пересобрать из последнего прогона в БД.
Также вызывается автоматически из main.save_results после анализа.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import DASHBOARD_FILE, today_msk

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Сбор данных
# ──────────────────────────────────────────────────────────────

def _latest_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Богатый срез последнего прогона (с индикаторами и сентиментом)."""
    out = []
    for r in results:
        scores = r.get("scores", {})
        ind = r.get("indicators", {})
        fund = r.get("fundamental", {})
        sent = r.get("sentiment", {})
        out.append({
            "ticker": r["ticker"],
            "company": r.get("company", r["ticker"]),
            "price": r.get("price"),
            "final_score": r.get("final_score"),
            "signal": r.get("signal"),
            "target_price": r.get("target_price"),
            "upside_pct": r.get("upside_pct"),
            "f_score": scores.get("fundamental"),
            "t_score": scores.get("technical"),
            "s_score": scores.get("sentiment"),
            "rsi": ind.get("rsi"),
            "macd_hist": ind.get("macd_histogram"),
            "above_sma200": ind.get("above_sma200"),
            "pe": fund.get("pe_ratio"),
            "div_yield": fund.get("div_yield_pct"),
            "roe": fund.get("roe_pct"),
            "sector": fund.get("sector"),
            "sentiment": sent.get("overall"),
            "key_event": sent.get("key_event"),
        })
    return out


def _latest_from_store(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Срез последнего прогона из SQLite (без индикаторов/сентимента)."""
    out = []
    for r in rows:
        try:
            scores = json.loads(r.get("scores_json") or "{}")
        except (ValueError, TypeError):
            scores = {}
        out.append({
            "ticker": r["ticker"],
            "company": r.get("company", r["ticker"]),
            "price": r.get("price"),
            "final_score": r.get("final_score"),
            "signal": r.get("signal"),
            "target_price": r.get("target_price"),
            "upside_pct": r.get("upside_pct"),
            "f_score": scores.get("fundamental"),
            "t_score": scores.get("technical"),
            "s_score": scores.get("sentiment"),
        })
    return out


def gather_dashboard_data(
    results: list[dict[str, Any]] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Собирает все данные для дашборда из переданного прогона и SQLite."""
    from data.store import _connect

    # История из SQLite
    history: dict[str, list[dict[str, Any]]] = {}
    timeline: dict[str, dict[str, int]] = {}
    all_rows: list[dict[str, Any]] = []
    distinct_dates: list[str] = []
    try:
        conn = _connect(db_path)
        import sqlite3
        conn.row_factory = sqlite3.Row
        all_rows = [dict(r) for r in conn.execute(
            "SELECT run_date, ticker, company, price, final_score, signal, "
            "target_price, upside_pct, scores_json FROM runs ORDER BY run_date"
        ).fetchall()]
        distinct_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT run_date FROM runs ORDER BY run_date"
        ).fetchall()]
        conn.close()
    except Exception as exc:
        logger.error("Дашборд: не удалось прочитать историю: %s", exc)

    for r in all_rows:
        history.setdefault(r["ticker"], []).append({
            "date": r["run_date"], "score": r["final_score"], "signal": r["signal"],
        })
        bucket = timeline.setdefault(r["run_date"], {"BUY": 0, "HOLD": 0, "SELL": 0})
        sig = r["signal"] if r["signal"] in bucket else "HOLD"
        bucket[sig] += 1

    timeline_list = [
        {"date": d, **timeline[d]} for d in sorted(timeline)
    ]

    # Текущий прогон: из results (богато) или из последней даты в БД
    if results:
        latest = _latest_from_results(results)
        run_date = today_msk().strftime("%Y-%m-%d")
    elif distinct_dates:
        run_date = distinct_dates[-1]
        last_rows = [r for r in all_rows if r["run_date"] == run_date]
        last_rows.sort(key=lambda x: -(x["final_score"] or 0))
        latest = _latest_from_store(last_rows)
    else:
        latest, run_date = [], today_msk().strftime("%Y-%m-%d")

    # Бэктест — только когда есть форвардная история (>=2 прогонов)
    backtest = None
    if len(distinct_dates) >= 2:
        try:
            from backtest import evaluate_stored_runs
            backtest = evaluate_stored_runs(db_path=db_path)
        except Exception as exc:
            logger.warning("Дашборд: бэктест недоступен: %s", exc)

    stats = {
        "BUY": sum(1 for x in latest if x["signal"] == "BUY"),
        "HOLD": sum(1 for x in latest if x["signal"] == "HOLD"),
        "SELL": sum(1 for x in latest if x["signal"] == "SELL"),
        "total": len(latest),
        "runs": len(distinct_dates),
    }

    return {
        "generated": today_msk().strftime("%d.%m.%Y"),
        "run_date": run_date,
        "stats": stats,
        "latest": latest,
        "history": history,
        "timeline": timeline_list,
        "backtest": backtest,
    }


# ──────────────────────────────────────────────────────────────
# Рендер
# ──────────────────────────────────────────────────────────────

def render_html(data: dict[str, Any]) -> str:
    """Встраивает данные в самодостаточный HTML-шаблон."""
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("/*__DATA__*/", payload)


def build_dashboard(
    results: list[dict[str, Any]] | None = None,
    db_path: Path | None = None,
    out_path: Path | None = None,
    macro: dict | None = None,
) -> Path | None:
    """Собирает данные, рендерит HTML и пишет в out_path (по умолчанию docs/index.html)."""
    out = out_path or DASHBOARD_FILE
    try:
        data = gather_dashboard_data(results, db_path)
        data["macro"] = macro or {}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(data), encoding="utf-8")
        logger.info("Дашборд собран: %s (%d акций, %d прогонов)",
                    out, data["stats"]["total"], data["stats"]["runs"])
        return out
    except Exception as exc:
        logger.error("Ошибка сборки дашборда: %s", exc, exc_info=True)
        return None


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Сборка HTML-дашборда из истории прогонов")
    ap.add_argument("--out", type=str, help="Путь к выходному HTML")
    args = ap.parse_args()
    path = build_dashboard(out_path=Path(args.out) if args.out else None)
    if path:
        print(f"Дашборд: {path}")
    else:
        print("Не удалось собрать дашборд (нет данных?).")


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MOEX Анализатор — дашборд</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0e141b; --panel:#161f2b; --panel2:#1d2836; --line:#2a3849;
    --txt:#e6edf3; --muted:#8b98a5; --accent:#2dd4bf;
    --buy:#22c55e; --hold:#f59e0b; --sell:#ef4444;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}
  a{color:var(--accent)}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
  header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:6px}
  h1{font-size:22px;margin:0;font-weight:650;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0 26px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .v{font-size:26px;font-weight:680;line-height:1.1}
  .card .l{color:var(--muted);font-size:12px;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
  .buy{color:var(--buy)} .hold{color:var(--hold)} .sell{color:var(--sell)}
  .sec{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin:30px 0 12px;font-weight:600}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  input,select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
    padding:8px 11px;border-radius:8px;font-size:14px;outline:none}
  input:focus,select:focus{border-color:var(--accent)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
    border-radius:12px;overflow:hidden}
  th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;user-select:none;
    text-transform:uppercase;letter-spacing:.4px}
  th:hover{color:var(--txt)}
  tbody tr{cursor:pointer}
  tbody tr:hover{background:var(--panel2)}
  tbody tr:last-child td{border-bottom:none}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .pill.BUY{background:rgba(34,197,94,.15);color:var(--buy)}
  .pill.HOLD{background:rgba(245,158,11,.15);color:var(--hold)}
  .pill.SELL{background:rgba(239,68,68,.15);color:var(--sell)}
  .bar{height:6px;border-radius:3px;background:var(--line);position:relative;min-width:54px}
  .bar>i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--accent)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:760px){.grid2{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  .muted{color:var(--muted)}
  .bt{display:flex;gap:18px;flex-wrap:wrap}
  .bt .item{flex:1;min-width:120px}
  .bt .big{font-size:24px;font-weight:680}
  /* drawer */
  .scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:9}
  .scrim.open{display:block}
  .drawer{position:fixed;top:0;right:0;height:100%;width:min(460px,92vw);background:var(--panel);
    border-left:1px solid var(--line);transform:translateX(100%);transition:.22s;z-index:10;
    overflow-y:auto;padding:22px}
  .drawer.open{transform:none}
  .drawer h2{margin:0 0 2px;font-size:20px}
  .close{position:absolute;top:16px;right:18px;background:none;border:none;color:var(--muted);
    font-size:24px;cursor:pointer;padding:0}
  .kv{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}
  .kv .muted{color:var(--muted)}
  .pillars{margin:16px 0}
  .pillars .row{display:flex;align-items:center;gap:10px;margin:8px 0}
  .pillars .row .lbl{width:90px;color:var(--muted);font-size:13px}
  .pillars .row .bar{flex:1}
  .pillars .row .num{width:42px;text-align:right;font-variant-numeric:tabular-nums}
  canvas{max-width:100%}
  footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>MOEX Анализатор</h1>
      <div class="sub">Прогон <b id="runDate"></b> · топ-20 акций Мосбиржи</div></div>
    <div class="sub">Сгенерировано <span id="gen"></span></div>
  </header>

  <div class="sec" id="macroSec" style="display:none">Макроконтекст</div>
  <div class="cards" id="macroCards" style="display:none"></div>

  <div class="sec">Сигналы</div>
  <div class="cards" id="cards"></div>

  <div class="sec">Сигналы по неделям</div>
  <div class="panel"><canvas id="timeline" height="90"></canvas></div>

  <div class="sec" id="btSec" style="display:none">Бэктест сигналов</div>
  <div class="panel" id="btPanel" style="display:none"></div>

  <div class="sec">Текущий прогон</div>
  <div class="toolbar">
    <input id="q" placeholder="Поиск по тикеру/компании…" style="flex:1;min-width:180px">
    <select id="sigF">
      <option value="">Все сигналы</option><option>BUY</option><option>HOLD</option><option>SELL</option>
    </select>
  </div>
  <table id="tbl">
    <thead><tr>
      <th data-k="ticker">Тикер</th>
      <th data-k="price">Цена</th>
      <th data-k="final_score">Score</th>
      <th data-k="signal">Сигнал</th>
      <th data-k="upside_pct">Потенциал</th>
      <th data-k="f_score">Фунд</th>
      <th data-k="t_score">Техн</th>
      <th data-k="s_score">Сент</th>
    </tr></thead>
    <tbody></tbody>
  </table>

  <footer>
    ⚠️ Не является инвестиционной рекомендацией. Данные: MOEX ISS API.
    Скоринг — взвешенная эвристика, целевые цены не являются оценкой стоимости.
  </footer>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer">
  <button class="close" id="close">×</button>
  <h2 id="dTitle"></h2>
  <div class="sub muted" id="dSub"></div>
  <div class="pillars" id="dPillars"></div>
  <div id="dKv"></div>
  <div class="sec" style="margin:18px 0 8px">История скора</div>
  <canvas id="dChart" height="150"></canvas>
  <div class="sec" style="margin:18px 0 8px" id="dSentSec">Сентимент</div>
  <div class="muted" id="dSent"></div>
</aside>

<script>
const DATA = /*__DATA__*/;
const SIGCLR = {BUY:'#22c55e',HOLD:'#f59e0b',SELL:'#ef4444'};
const fmt = (v,d=2)=> v==null||isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:d});
const pct = v=> v==null ? '—' : (v>=0?'+':'')+fmt(v,1)+'%';

// Карточки сигналов
(function(){
  const s=DATA.stats;
  const c=[['Всего',s.total,''],['BUY',s.BUY,'buy'],['HOLD',s.HOLD,'hold'],
           ['SELL',s.SELL,'sell'],['Прогонов',s.runs,'']];
  document.getElementById('cards').innerHTML = c.map(([l,v,cl])=>
    `<div class="card"><div class="v ${cl}">${v}</div><div class="l">${l}</div></div>`).join('');
  document.getElementById('runDate').textContent = DATA.run_date;
  document.getElementById('gen').textContent = DATA.generated;
})();

// Макроконтекст
(function(){
  const m=DATA.macro||{};
  const items=[];
  if(m.imoex!=null) items.push(['IMOEX',fmt(m.imoex,0),'пунктов','']);
  if(m.usd_rub!=null) items.push(['USD/RUB',fmt(m.usd_rub,2),'₽','']);
  if(m.cny_rub!=null) items.push(['CNY/RUB',fmt(m.cny_rub,2),'₽','']);
  if(m.cbr_rate!=null) items.push(['Ставка ЦБ',fmt(m.cbr_rate,1)+'%','','']);
  if(m.rgbi!=null) items.push(['RGBI',fmt(m.rgbi,2),'пунктов','']);
  if(m.brent!=null) items.push(['Brent','$'+fmt(m.brent,1),'за барр.','']);
  if(!items.length) return;
  document.getElementById('macroSec').style.display='';
  const mc=document.getElementById('macroCards'); mc.style.display='';
  mc.innerHTML=items.map(([l,v,u,cl])=>
    `<div class="card"><div class="v ${cl}">${v}</div><div class="l">${l}${u?' · '+u:''}</div></div>`).join('');
})();

// Таблица
let rows = DATA.latest.slice();
let sortK='final_score', sortDir=-1;
const tbody = document.querySelector('#tbl tbody');
function draw(){
  const q=document.getElementById('q').value.toLowerCase();
  const sf=document.getElementById('sigF').value;
  let r=rows.filter(x=>(!sf||x.signal===sf) &&
    ((x.ticker||'').toLowerCase().includes(q)||(x.company||'').toLowerCase().includes(q)));
  r.sort((a,b)=>{const x=a[sortK],y=b[sortK];
    if(x==null)return 1; if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*sortDir;});
  tbody.innerHTML = r.map(x=>`<tr data-t="${x.ticker}">
    <td><b>${x.ticker}</b> <span class="muted">${x.company||''}</span></td>
    <td>${fmt(x.price)}</td>
    <td><b>${fmt(x.final_score,1)}</b></td>
    <td><span class="pill ${x.signal}">${x.signal}</span></td>
    <td class="${x.upside_pct>=0?'buy':'sell'}">${pct(x.upside_pct)}</td>
    <td>${fmt(x.f_score,0)}</td><td>${fmt(x.t_score,0)}</td><td>${fmt(x.s_score,0)}</td>
  </tr>`).join('');
  tbody.querySelectorAll('tr').forEach(tr=>tr.onclick=()=>openDrawer(tr.dataset.t));
}
document.querySelectorAll('#tbl th').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k; if(k===sortK)sortDir*=-1; else{sortK=k;sortDir=(k==='ticker'||k==='signal')?1:-1;}
  draw();});
document.getElementById('q').oninput=draw;
document.getElementById('sigF').onchange=draw;
draw();

// Timeline (stacked bars)
(function(){
  const t=DATA.timeline;
  new Chart(document.getElementById('timeline'),{type:'bar',
    data:{labels:t.map(x=>x.date),datasets:[
      {label:'BUY',data:t.map(x=>x.BUY),backgroundColor:SIGCLR.BUY,stack:'s'},
      {label:'HOLD',data:t.map(x=>x.HOLD),backgroundColor:SIGCLR.HOLD,stack:'s'},
      {label:'SELL',data:t.map(x=>x.SELL),backgroundColor:SIGCLR.SELL,stack:'s'}]},
    options:{responsive:true,plugins:{legend:{labels:{color:'#8b98a5'}}},
      scales:{x:{stacked:true,ticks:{color:'#8b98a5'},grid:{display:false}},
              y:{stacked:true,ticks:{color:'#8b98a5'},grid:{color:'#2a3849'}}}}});
})();

// Backtest
(function(){
  const b=DATA.backtest;
  if(!b||!b.runs_evaluated){return;}
  document.getElementById('btSec').style.display='';
  const p=document.getElementById('btPanel'); p.style.display='';
  const bs=b.by_signal||{};
  const cell=(name,o)=>`<div class="item"><div class="muted">${name}</div>
    <div class="big">${o&&o.hit_rate!=null?o.hit_rate+'%':'—'}</div>
    <div class="muted">n=${o?o.n:0} · ср.дох ${o?fmt(o.mean_return,1):'—'}%</div></div>`;
  p.innerHTML=`<div class="bt">
    <div class="item"><div class="muted">Общий hit-rate (${b.runs_evaluated} набл., ${b.horizon_days} дн.)</div>
      <div class="big">${b.hit_rate}%</div><div class="muted">ср.доходность ${fmt(b.mean_return,1)}%</div></div>
    ${cell('BUY',bs.BUY)}${cell('SELL',bs.SELL)}</div>`;
})();

// Drawer
const drawer=document.getElementById('drawer'), scrim=document.getElementById('scrim');
function closeD(){drawer.classList.remove('open');scrim.classList.remove('open');}
document.getElementById('close').onclick=closeD; scrim.onclick=closeD;
let dChart=null;
function bar(v){const w=Math.max(0,Math.min(100,v||0));return `<div class="bar"><i style="width:${w}%"></i></div>`;}
function openDrawer(ticker){
  const x=rows.find(r=>r.ticker===ticker); if(!x)return;
  document.getElementById('dTitle').textContent=`${x.ticker} · ${fmt(x.final_score,1)}`;
  document.getElementById('dSub').textContent=`${x.company||''} · ${fmt(x.price)} ₽ · цель ${fmt(x.target_price)} (${pct(x.upside_pct)})`;
  document.getElementById('dPillars').innerHTML=
    [['Фундамент',x.f_score],['Технический',x.t_score],['Сентимент',x.s_score]].map(([l,v])=>
      `<div class="row"><div class="lbl">${l}</div>${bar(v)}<div class="num">${fmt(v,0)}</div></div>`).join('');
  const kv=[];
  const add=(k,v)=>{if(v!==undefined&&v!==null)kv.push(`<div class="kv"><span class="muted">${k}</span><span>${v}</span></div>`);};
  add('Сигнал',`<span class="pill ${x.signal}">${x.signal}</span>`);
  if(x.rsi!=null)add('RSI',fmt(x.rsi,1));
  if(x.macd_hist!=null)add('MACD гист.',fmt(x.macd_hist,3));
  if(x.above_sma200!=null)add('Выше SMA200',x.above_sma200?'да':'нет');
  if(x.pe!=null)add('P/E',fmt(x.pe,1));
  if(x.div_yield!=null)add('Див.доходность',fmt(x.div_yield,1)+'%');
  if(x.roe!=null)add('ROE',fmt(x.roe,1)+'%');
  if(x.sector)add('Сектор',x.sector);
  document.getElementById('dKv').innerHTML=kv.join('');
  // Сентимент
  const sentSec=document.getElementById('dSentSec'), sent=document.getElementById('dSent');
  if(x.key_event||x.sentiment){sentSec.style.display='';sent.style.display='';
    sent.textContent=(x.sentiment?('['+x.sentiment+'] '):'')+(x.key_event||'');}
  else{sentSec.style.display='none';sent.style.display='none';}
  // График истории скора
  const h=(DATA.history[ticker]||[]);
  if(dChart)dChart.destroy();
  dChart=new Chart(document.getElementById('dChart'),{type:'line',
    data:{labels:h.map(p=>p.date),datasets:[{label:'Score',data:h.map(p=>p.score),
      borderColor:'#2dd4bf',backgroundColor:'rgba(45,212,191,.12)',fill:true,tension:.25,
      pointRadius:3,pointBackgroundColor:h.map(p=>SIGCLR[p.signal]||'#8b98a5')}]},
    options:{plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:'#8b98a5'},grid:{display:false}},
              y:{suggestedMin:0,suggestedMax:100,ticks:{color:'#8b98a5'},grid:{color:'#2a3849'}}}}});
  drawer.classList.add('open');scrim.classList.add('open');
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
