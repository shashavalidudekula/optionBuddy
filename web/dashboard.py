"""
dashboard.py — read-only web dashboard for OptionBuddy (dense-terminal theme).

A separate, read-only mirror of the advisory database: live (active) calls,
closed calls, and the paper (shadow) account with its open positions and
closed-trade ledger. It does NOT call any broker or LLM and never writes — the
main advisory loop owns all of that and keeps the DB fresh. Safe to run as its
own container with zero risk of interfering with the live engine.

Run:  uvicorn web.dashboard:app --host 0.0.0.0 --port 8000
Optional basic auth: set DASHBOARD_USER and DASHBOARD_PASS in the environment.
"""

import os
import secrets
from datetime import datetime, date
from decimal import Decimal

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config.logger import get_logger
from data.advisory_store import (
    get_active_calls,
    get_closed_calls,
    get_paper_stats,
    get_open_paper_positions,
    get_closed_paper_positions,
    get_not_executed_calls,
    get_call_levels,
)
from data.paper_history import daily_pnl, weekly_pnl

log = get_logger("dashboard")

app = FastAPI(title="OptionBuddy Dashboard", docs_url=None, redoc_url=None)

_USER = os.getenv("DASHBOARD_USER", "")
_PASS = os.getenv("DASHBOARD_PASS", "")
_security = HTTPBasic(auto_error=True)


def _auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not (_USER and _PASS):
        return
    ok = secrets.compare_digest(credentials.username, _USER) and \
        secrets.compare_digest(credentials.password, _PASS)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})


_deps = [Depends(_auth)] if (_USER and _PASS) else []


def _clean(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _row(d: dict) -> dict:
    return {k: _clean(v) for k, v in d.items()}


def _state() -> dict:
    stats = get_paper_stats() or {}
    open_pos = get_open_paper_positions()
    levels = get_call_levels([p.get("call_id") for p in open_pos]) if open_pos else {}

    open_out = []
    for p in open_pos:
        last = float(p["last_price"]) if p["last_price"] is not None else float(p["entry_price"])
        entry = float(p["entry_price"])
        sign = 1 if str(p["action"]).upper() == "BUY" else -1
        upnl = round(sign * int(p["remaining_qty"]) * (last - entry), 2)
        lv = levels.get(p.get("call_id")) or {}
        row = _row(p)
        row.update({
            "ltp": round(last, 2),
            "unrealized_pnl": upnl,
            "target_1": _clean(lv.get("target_1")),
            "target_2": _clean(lv.get("target_2")),
            "stop_loss": _clean(lv.get("stop_loss")),
            "option_expiry": _clean(lv.get("option_expiry")),
        })
        open_out.append(row)

    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "paper": {
            "stats": {k: _clean(v) for k, v in stats.items()},
            "open": open_out,
            "closed": [_row(p) for p in get_closed_paper_positions(80)],
            "daily": [_row(d) for d in daily_pnl(30)],
            "weekly": [_row(d) for d in weekly_pnl(12)],
        },
        "calls": {
            "active": [_row(c) for c in get_active_calls()],
            "closed": [_row(c) for c in get_closed_calls(80)],
            "not_executed": [_row(c) for c in get_not_executed_calls(60)],
        },
    }


@app.get("/api/state", dependencies=_deps)
def api_state():
    try:
        return JSONResponse(_state(), headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        log.error("Dashboard state failed: %s", e)
        raise HTTPException(status_code=500, detail="state unavailable")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse, dependencies=_deps)
def index():
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OptionBuddy — Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0c0f16; --panel:#12161f; --panel2:#161c27; --card:#1a212e; --line:#232c3b;
    --fg:#e9eef5; --mut:#9aa6b7; --dim:#5e6a7d;
    --grn:#35d39a; --red:#fb7185; --amber:#f4c452; --accent:#8b93f8;
    --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
    --sans:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:13px;line-height:1.35}
  header{display:flex;align-items:center;gap:14px;padding:9px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
  header .logo{font-weight:600;letter-spacing:.3px}
  header .logo b{color:var(--grn)}
  header .live{color:var(--mut);font-family:var(--mono);font-size:11px}
  header .live .dot{color:var(--grn)}
  header .right{margin-left:auto;color:var(--dim);font-family:var(--mono);font-size:11px;letter-spacing:.3px}
  main{padding:12px 16px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(124px,1fr));gap:8px;margin-bottom:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 11px}
  .card .k{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.7px}
  .card .v{font-family:var(--mono);font-size:18px;font-weight:600;margin-top:3px}
  .card .sub{font-family:var(--mono);font-size:11px;margin-top:2px}
  #open{margin-bottom:12px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  @media(max-width:768px){.row{grid-template-columns:1fr}}
  .box{background:var(--panel2);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;overflow:hidden;min-height:0}
  .box.tall{height:60vh}
  .box.short{max-height:40vh}
  .cap{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);flex:0 0 auto}
  .cap .t{font-size:12px;font-weight:600;letter-spacing:.4px}
  .cap .tag{font-family:var(--mono);font-size:10px;color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:0 6px;text-transform:uppercase;letter-spacing:.5px}
  .cap .n{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--mut)}
  .tabs{display:flex;gap:5px;padding:7px 9px;border-bottom:1px solid var(--line);flex:0 0 auto}
  .tab{background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:6px;padding:4px 12px;font-family:var(--mono);font-size:11.5px;cursor:pointer;display:flex;gap:7px;align-items:center}
  .tab:hover{color:var(--fg)}
  .tab.active{background:#202a3a;color:var(--fg);border-color:#3a4860}
  .tab .b{color:var(--dim);font-size:10px}
  .tab.active .b{color:var(--accent)}
  .bd{overflow:auto;min-height:0}
  .box.tall .panel{flex:1 1 auto}
  .panel{overflow:auto;min-height:0}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th,td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child,.l{text-align:left}
  th{position:sticky;top:0;background:var(--panel2);color:var(--dim);font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.5px;z-index:1}
  tbody tr:hover{background:#1c2433}
  .pos{color:var(--grn)} .neg{color:var(--red)}
  .buy{color:var(--grn);font-weight:600} .sell{color:var(--red);font-weight:600}
  .dim{color:var(--dim)} .mut{color:var(--mut)}
  .pill{display:inline-block;padding:0 7px;border-radius:4px;font-size:10px;border:1px solid var(--line);color:var(--mut)}
  .arrow{font-size:10px}
  .foot{padding:6px 12px;font-family:var(--mono);font-size:10.5px;color:var(--dim);border-top:1px solid var(--line)}
  .empty{color:var(--dim);padding:16px 12px;font-family:var(--mono);font-size:12px}
  footer{color:var(--dim);font-size:11px;padding:10px 16px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<header>
  <span class="logo">⬢ Option<b>Buddy</b></span>
  <span class="live" id="updated"><span class="dot">●</span> connecting…</span>
  <span class="right">auto-refresh 20s · IST · shadow money · advisory only</span>
</header>
<main>
  <div class="cards" id="cards"></div>
  <div class="row">
    <div id="weekly"></div>
    <div id="daily"></div>
  </div>
  <div id="open"></div>
  <div class="row">
    <div id="col-opt"></div>
    <div id="col-oth"></div>
  </div>
  <div class="row">
    <div id="ledger"></div>
    <div id="closed"></div>
  </div>
  <div id="notexec"></div>
</main>
<footer>Read-only mirror · advisory only, not investment advice · markets carry risk.</footer>

<script>
const STAT = {active:"waiting", entry_triggered:"in trade", target1_hit:"T1 trail",
              target_hit:"target", sl_hit:"stop", expired:"expired", closed:"closed"};
const CAT = {equity:"Equity", futures:"Futures", commodity:"Commodity", index_option:"Index"};
const tabState = {opt:"NIFTY", oth:"FUTURES"};

const f = (v,d=2)=> (v===null||v===undefined||v==="")?"—":Number(v).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const f0 = v => (v===null||v===undefined||v==="")?"—":Number(v).toLocaleString("en-IN",{maximumFractionDigits:0});
const r0 = v => (v===null||v===undefined||v==="")?"—":"₹"+f0(v);
const r2 = v => (v===null||v===undefined||v==="")?"—":"₹"+f(v);
const sgn = v => v>0?"pos":(v<0?"neg":"");
const esc = s => (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const side = a => (String(a).toUpperCase()==="BUY")?'<span class="buy">BUY</span>':'<span class="sell">SELL</span>';
const xp = v => v?'<span class="dim">exp '+String(v).slice(5,10)+'</span> ':'';
const tm = v => v?String(v).replace("T"," ").slice(5,16):"—";
const arrowPct = v => v==null?"—":(v>=0?'<span class="pos"><span class="arrow">▲</span> +'+f(v)+'%</span>':'<span class="neg"><span class="arrow">▼</span> '+f(v)+'%</span>');

const entryLtp = c => {
  let e=(c.entry_min!=null&&c.entry_max!=null)?(r2(c.entry_min)+" – "+r2(c.entry_max)):r2(c.entry_price);
  let l=(c.last_price!=null?r2(c.last_price):(c.ltp!=null?r2(c.ltp):"—"));
  return e+' <span class="dim">→</span> '+l;
};
const targets = c => [c.target_1,c.target_2].filter(x=>x!=null).map(r2).join(" · ")||"—";

function tbl(head, rows){
  if(!rows.length) return '<div class="empty">— none —</div>';
  return '<table><thead><tr>'+head.map(h=>`<th class="${h.l?'l':''}">${h.t}</th>`).join('')+
    '</tr></thead><tbody>'+rows.join('')+'</tbody></table>';
}
const CALL_HEAD = [{t:"Instrument",l:1},{t:"Status"},{t:"Entry / LTP",l:1},{t:"Targets"},{t:"Stop"}];
function callRow(c){
  return `<tr>
    <td class="l">${side(c.action)} ${xp(c.option_expiry)}${esc(c.instrument)}</td>
    <td><span class="pill">${STAT[c.status]||c.status}</span></td>
    <td class="l">${entryLtp(c)}</td>
    <td>${targets(c)}</td>
    <td>${c.stop_loss!=null?r2(c.stop_loss):"—"}</td></tr>`;
}
const callTbl = arr => '<div class="bd panel">'+tbl(CALL_HEAD, arr.map(callRow))+'</div>';

function tabbedBox(group, tabs){
  const btns = tabs.map(t=>`<button class="tab" data-tg="${group}" data-tab="${t.k}" onclick="selectTab('${group}','${t.k}')">${t.k} <span class="b">${t.n}</span></button>`).join('');
  const panels = tabs.map(t=>`<div class="panel" data-tg="${group}" data-tab="${t.k}">${t.html}</div>`).join('');
  return `<div class="box tall"><div class="tabs">${btns}</div>${panels}</div>`;
}
function applyTabs(group){
  document.querySelectorAll('[data-tg="'+group+'"]').forEach(el=>{
    const on = el.dataset.tab===tabState[group];
    if(el.classList.contains('tab')) el.classList.toggle('active', on);
    if(el.classList.contains('panel')) el.style.display = on?'':'none';
  });
}
window.selectTab = function(group,label){ tabState[group]=label; applyTabs(group); };

function capBox(title, tag, count, inner, cls, foot){
  return `<div class="box ${cls||''}">
    <div class="cap"><span class="t">${title}</span><span class="tag">${tag}</span><span class="n">${count}</span></div>
    <div class="bd panel">${inner}</div>${foot?`<div class="foot">${foot}</div>`:''}</div>`;
}

function render(s){
  const st = s.paper.stats || {};
  document.getElementById('updated').innerHTML = '<span class="dot">●</span> live · updated ' + (s.now||"").replace("T"," ") + ' IST';
  const ret = st.total_return_pct;
  const cards = [
    ["Equity", r0(st.equity), arrowPct(ret)],
    ["Free cash", r0(st.free_cash), null],
    ["Deployed", r0(st.deployed_capital), null],
    ["Realised", r0(st.realized_pnl), null, sgn(st.realized_pnl)],
    ["Unrealised", r0(st.unrealized_pnl), null, sgn(st.unrealized_pnl)],
    ["Today", r0(st.today_realized), null, sgn(st.today_realized)],
    ["Win rate", f(st.win_rate,1)+"%", null],
    ["Closed", f0(st.closed_trades)+' <span class="dim">W'+f0(st.wins)+"/L"+f0(st.losses)+"</span>", null],
    ["Max DD", f(st.max_drawdown_pct)+"%", null, "neg"],
    ["Open", f0(st.open_positions), null],
  ];
  document.getElementById('cards').innerHTML = cards.map(c=>
    `<div class="card"><div class="k">${c[0]}</div><div class="v ${c[3]||''}">${c[1]}</div>${c[2]?`<div class="sub">${c[2]}</div>`:''}</div>`).join('');

  // P&L breakdown rows — shared between the weekly and daily tables.
  const dayfmt = v => { if(!v) return "—"; const d=new Date(v+"T00:00:00");
    return d.toLocaleDateString("en-IN",{weekday:"short",day:"2-digit",month:"short"}); };
  const weekfmt = v => { if(!v) return "—"; const a=new Date(v+"T00:00:00");
    const b=new Date(a); b.setDate(b.getDate()+4);  // Mon → Fri
    const o={day:"2-digit",month:"short"};
    return a.toLocaleDateString("en-IN",o)+" – "+b.toLocaleDateString("en-IN",o); };
  const PNL_HEAD = lbl => [{t:lbl,l:1},{t:"Trades"},{t:"Wins"},{t:"Losses"},{t:"Win %"},{t:"Profit"},{t:"Loss"},{t:"Net P&L"}];
  const pnlRow = (label, d) => `<tr>
      <td class="l">${label}</td>
      <td>${f0(d.trades)}</td>
      <td class="pos">${f0(d.wins)}</td>
      <td class="neg">${f0(d.losses)}</td>
      <td class="${d.win_rate>=50?'pos':'neg'}">${f(d.win_rate,1)}%</td>
      <td class="pos">${d.gross_profit>0?"+"+r0(d.gross_profit):"—"}</td>
      <td class="neg">${d.gross_loss<0?r0(d.gross_loss):"—"}</td>
      <td class="${sgn(d.net_pnl)}">${d.net_pnl>=0?"+":""}${r0(d.net_pnl)}</td></tr>`;

  // Weekly P&L — resets each Monday with fresh capital; history kept for tracking.
  const wk = s.paper.weekly || [];
  const weeklyTbl = wk.length
    ? tbl(PNL_HEAD("Week"), wk.map(d=>pnlRow(weekfmt(d.period), d)))
    : '<div class="empty">No closed trades yet.</div>';
  document.getElementById('weekly').innerHTML = capBox('Weekly P&L', 'by week · Mon–Fri', wk.length, weeklyTbl, 'short');

  // Daily P&L
  const dd = s.paper.daily || [];
  const dailyTbl = dd.length
    ? tbl(PNL_HEAD("Date"), dd.map(d=>pnlRow(dayfmt(d.period), d)))
    : '<div class="empty">No closed trades yet.</div>';
  document.getElementById('daily').innerHTML = capBox('Daily P&L', 'by day', dd.length, dailyTbl, 'short');

  // Open positions
  const op = s.paper.open;
  const opTbl = op.length ? tbl(
    [{t:"Instrument",l:1},{t:"Status"},{t:"Entry / LTP",l:1},{t:"Targets"},{t:"Stop"},{t:"uP&L"}],
    op.map(p=>`<tr><td class="l">${side(p.action)} ${xp(p.option_expiry)}${esc(p.instrument)}</td>
      <td><span class="pill">in trade</span></td><td class="l">${entryLtp(p)}</td>
      <td>${targets(p)}</td><td>${p.stop_loss!=null?r2(p.stop_loss):"—"}</td>
      <td class="${sgn(p.unrealized_pnl)}">${p.unrealized_pnl>=0?"+":""}${r0(p.unrealized_pnl)}</td></tr>`))
    : '<div class="empty">No open positions — engine is flat.</div>';
  document.getElementById('open').innerHTML = capBox('Open Positions', 'paper · live', op.length, opTbl, 'short');

  const act = s.calls.active;
  const byU = u => act.filter(c=>c.category==='index_option' && (c.underlying||'').toUpperCase()===u);
  document.getElementById('col-opt').innerHTML = tabbedBox('opt', [
    {k:"NIFTY",     n:byU('NIFTY').length,     html:callTbl(byU('NIFTY'))},
    {k:"BANKNIFTY", n:byU('BANKNIFTY').length, html:callTbl(byU('BANKNIFTY'))},
    {k:"SENSEX",    n:byU('SENSEX').length,    html:callTbl(byU('SENSEX'))},
  ]);
  document.getElementById('col-oth').innerHTML = tabbedBox('oth', [
    {k:"FUTURES", n:act.filter(c=>c.category==='futures').length, html:callTbl(act.filter(c=>c.category==='futures'))},
    {k:"STOCKS",  n:act.filter(c=>c.category==='equity').length,  html:callTbl(act.filter(c=>c.category==='equity'))},
  ]);

  // Paper ledger (under options) — Flow in/out format
  const led = s.paper.closed;
  document.getElementById('ledger').innerHTML = capBox('Paper Ledger', 'executed', led.length, tbl(
    [{t:"Instrument",l:1},{t:"Flow",l:1},{t:"P&L"},{t:"Closed",l:1}],
    led.slice(0,40).map(p=>`<tr><td class="l">${side(p.action)} ${esc(p.instrument)}</td>
      <td class="l"><span class="dim">in</span> ${r2(p.entry_price)} <span class="dim">out</span> ${p.last_price!=null?r2(p.last_price):"—"}</td>
      <td class="${sgn(p.realized_pnl)}">${p.realized_pnl>=0?"+":""}${r0(p.realized_pnl)}</td>
      <td class="l">${tm(p.closed_at)}</td></tr>`)),
    'short', led.length>40?('showing 40 of '+led.length):'');

  // Closed calls (under futures) — futures + stocks only, no index options
  const cc = s.calls.closed.filter(c=>c.category==='futures'||c.category==='equity');
  document.getElementById('closed').innerHTML = capBox('Closed Calls', 'advisory log', cc.length, tbl(
    [{t:"Cat",l:1},{t:"Side"},{t:"Instrument",l:1},{t:"Result"},{t:"Time",l:1}],
    cc.slice(0,40).map(c=>`<tr><td class="l mut">${CAT[c.category]||c.category}</td><td>${side(c.action)}</td>
      <td class="l">${esc(c.instrument)}</td><td>${arrowPct(c.result_pct)}</td>
      <td class="l">${tm(c.issued_at)}</td></tr>`)),
    'short', cc.length>40?('showing 40 of '+cc.length):'');

  // Generated but NOT executed (capital exhausted / position cap / loss halt)
  const REASON = {unfunded:"capital exhausted", capped:"max positions", halted_daily_loss:"daily-loss halt"};
  const ne = s.calls.not_executed || [];
  document.getElementById('notexec').innerHTML = capBox('Generated · Not Executed', 'no capital / limits', ne.length, tbl(
    [{t:"Cat",l:1},{t:"Side"},{t:"Instrument",l:1},{t:"Reason"},{t:"Conf"},{t:"Time",l:1}],
    ne.slice(0,40).map(c=>`<tr><td class="l mut">${CAT[c.category]||c.category}</td><td>${side(c.action)}</td>
      <td class="l">${esc(c.instrument)}</td><td><span class="pill">${REASON[c.paper_status]||c.paper_status}</span></td>
      <td>${c.confidence!=null?c.confidence+"%":"—"}</td><td class="l">${tm(c.issued_at)}</td></tr>`)),
    'short', ne.length>40?('showing 40 of '+ne.length):'');

  applyTabs('opt'); applyTabs('oth');
}

async function tick(){
  try{
    const r = await fetch('api/state',{cache:'no-store'});
    if(!r.ok) throw new Error('http '+r.status);
    const data = await r.json();
    try{ render(data); }
    catch(err){ document.getElementById('updated').textContent = "render error: " + (err && err.message); throw err; }
  }catch(e){
    const u=document.getElementById('updated');
    if(!/render error/.test(u.textContent)) u.innerHTML = '<span class="dot" style="color:var(--red)">●</span> connection error — retrying…';
  }
}
tick(); setInterval(tick, 20000);
</script>
</body>
</html>"""
