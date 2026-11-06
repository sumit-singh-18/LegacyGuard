"""Session map (FR-30, D-038): this session's changed functions and what depends on them, as one offline HTML file.

A view over the existing lookup tables and the findings ledger. It is not a call graph: edges are name matches,
and an edge counts as observed only when one test runs both ends.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from guardlib import impact, ledger, report
from indexer import db as idb
from indexer import names

CHANGED_KINDS = ("behavior_difference", "verified", "untested")
MAX_CALLERS = 60
NOTE = ("Edges come from matching function names, so one can be wrong and dynamic calls are missing. "
        "A solid edge means one test runs both functions; a dashed edge is a name match only. "
        "Only API routes are labeled as entry points; scheduled jobs are not indexed.")


def _rank(f: dict) -> int:
    """Which finding speaks for a changed function: open evidence first, turned-back history last."""
    if f["status"] in (ledger.DIFFERENCE, ledger.FAILURE):
        return {"open": 0, "waived": 1}.get(f["state"], 4)
    return 2 if f["status"] == ledger.UNTESTED else 3


def _changed(data: dict) -> dict[tuple, dict]:
    groups: dict[tuple, list[dict]] = {}
    for f in data["findings"]:
        if f["kind"] in CHANGED_KINDS and f["state"] != "superseded":
            groups.setdefault((f["file"], f["item"]), []).append(f)
    out = {}
    for (file, item), fs in groups.items():
        fs.sort(key=lambda f: (_rank(f), -int(f["id"].split("-")[1])))
        top = fs[0]
        out[(file, item)] = {
            "id": f"{file}::{item}", "name": item, "file": file, "kind": "changed", "layer": 0, "status": top["status"], "state": top["state"],
            "outcome": report.outcome(top), "turned_back": top["status"] == ledger.VERIFIED and any(f["state"] == "resolved" for f in fs),
            "tests": None, "routes": [], "sources": [], "via": None,
            "findings": [{"id": f["id"], "status": f["status"], "state": f["state"], "outcome": report.outcome(f), "evidence": str(f["evidence"])[:600]} for f in fs]}
    return out


def build(cfg, data: dict) -> dict:
    counts = report.counts(data)
    visible = report.visible(data)
    changed = _changed(data)
    nodes: dict[str, dict] = {n["id"]: n for n in changed.values()}
    edges: dict[tuple, dict] = {}
    model = {"session": data.get("session_id"), "mode": cfg.mode, "baseline": (data.get("baseline_commit") or "")[:10], "budget": cfg.budget,
             "prompts": data.get("prompts_used", 0), "wakeups": data.get("wakeups", 0), "generated": dt.datetime.now().isoformat(timespec="minutes"),
             "counts": counts, "turned_back": sum(1 for f in visible if f["state"] == "resolved"), "statuses": list(ledger.STATUSES),
             "index": idb.exists(cfg.root), "coverage": True, "truncated": 0, "note": NOTE, "footer": report.FOOTER}
    if model["index"] and changed:
        conn = idb.connect(cfg.root)
        try:
            try:  # callers added during this session should show up; re-parsing changed files is cheap
                names.update(conn, cfg.root, cfg.scope)
                conn.commit()
            except Exception:  # noqa: BLE001 - a stale index still gives a useful map
                pass
            tests_cache: dict[tuple, set] = {}

            def tests_of(file: str, qualname: str) -> set:
                key = (file, qualname)
                if key not in tests_cache:
                    tests_cache[key] = {r["test_id"] for r in conn.execute("SELECT test_id FROM test_cover WHERE file = ? AND qualname = ?", key)}
                return tests_cache[key]

            callers_added = 0
            for (file, item), node in changed.items():
                imp = impact.compute(conn, cfg, node["id"])
                if imp is None:
                    node["sources"] = ["not in the index, so its callers are unknown"]
                    continue
                model["coverage"] = model["coverage"] and imp.coverage_known
                node["tests"] = bool(imp.tests) if imp.coverage_known else None
                node["test_count"] = len(imp.tests)
                node["cochange"] = [f"{c['file']} ({round(c['confidence'] * 100)}%)" for c in imp.cochange[:3]]
                route_of: dict[tuple, list[str]] = {}
                for r in imp.routes:
                    route_of.setdefault((r["handler_file"], r["handler_qualname"]), []).append(r["route"])
                node["routes"] = sorted(set(node["routes"]) | set(route_of.get((file, item), [])))
                by_hop: dict[int, list[dict]] = {}
                for c in imp.callers:
                    cid = f"{c['file']}::{c['qualname']}"
                    if cid not in nodes:
                        if callers_added >= MAX_CALLERS:
                            model["truncated"] += 1
                            continue
                        callers_added += 1
                        nodes[cid] = {"id": cid, "name": c["qualname"], "file": c["file"], "kind": "caller", "layer": c["hop"], "status": None, "state": None,
                                      "tests": (not c.get("no_test")) if imp.coverage_known else None, "routes": [], "sources": c["sources"], "via": c["via"], "findings": []}
                    target = nodes[cid]
                    if target["kind"] == "caller":
                        target["layer"] = min(target["layer"], c["hop"])
                    target["routes"] = sorted(set(target["routes"]) | set(route_of.get((c["file"], c["qualname"]), [])))
                    by_hop.setdefault(c["hop"], []).append(c)
                    parents = [node["id"]] if c["hop"] == 1 else [f"{p['file']}::{p['qualname']}" for p in by_hop.get(c["hop"] - 1, [])
                                                                 if p["qualname"].rsplit(".", 1)[-1] == c["via"]]
                    for pid in parents:
                        if pid in nodes and pid != cid:
                            pfile, pqual = pid.split("::", 1)
                            edges[(cid, pid)] = {"from": cid, "to": pid, "observed": bool(tests_of(c["file"], c["qualname"]) & tests_of(pfile, pqual))}
        finally:
            conn.close()
    order = {n["id"]: i for i, n in enumerate(changed.values())}
    model["nodes"] = sorted(nodes.values(), key=lambda n: (n["layer"], order.get(n["id"], 0), n["id"]))
    model["edges"] = sorted(edges.values(), key=lambda e: (e["from"], e["to"]))
    model["story"] = [{**st, "label": report.STORY_LABEL[st["kind"]]} for st in report.story(data)]
    model["findings"] = [{"id": f["id"], "status": f["status"], "subject": f["subject"], "evidence": str(f["evidence"])[:400], "outcome": report.outcome(f),
                          "node": f"{f['file']}::{f['item']}" if f"{f['file']}::{f['item']}" in nodes else None} for f in visible]
    return model


CSS = r"""
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d2125;--muted:#5d6670;--line:#d4d8dc;--soft:#eef0f1;--edge:#8b949e;
--ok:#1a7f5a;--ok-bg:#e3f4ea;--fail:#c81e4a;--fail-bg:#fde6ec;--diff:#c2410c;--diff-bg:#fdeadd;--risk:#9a6406;--risk-bg:#fcf1cf;--unt:#5b55b0;--unt-bg:#eceafb}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--panel:#1d2125;--ink:#e8eaec;--muted:#a2abb3;--line:#3a4047;--soft:#272c31;--edge:#7b848d;
--ok:#4cc38a;--ok-bg:#16332a;--fail:#f5688d;--fail-bg:#451b28;--diff:#f0894f;--diff-bg:#44271a;--risk:#e0ad3f;--risk-bg:#40330f;--unt:#a9a2f2;--unt-bg:#2b2850}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:16px 20px 4px}h1{font-size:19px;margin:0 0 2px}h2{font-size:14px;margin:22px 0 8px}.meta{color:var(--muted);font-size:13px;margin:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}.chip{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12.5px;background:var(--panel);white-space:nowrap}
.chip b{font-weight:600}.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;border:1px solid}
main{display:flex;gap:16px;align-items:flex-start;padding:12px 20px 4px}#left{flex:1;min-width:0}
.box{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}
aside{width:340px;flex:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}
aside h3{font:600 13.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0 0 2px;overflow-wrap:anywhere}aside dt{font-weight:600;margin-top:9px;font-size:13px}aside dd{margin:0;overflow-wrap:anywhere}
code,pre{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--soft);padding:8px;border-radius:4px;margin:3px 0}
.tools{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;color:var(--muted);font-size:12.5px;margin:0 0 8px}.tools label{cursor:pointer;color:var(--ink)}
svg text{fill:var(--ink)}.col{font-size:12px;fill:var(--muted)}.nm{font:600 12px ui-monospace,SFMono-Regular,Menlo,monospace}.sb{font-size:11px;fill:var(--muted)}
.nd{cursor:pointer}.nd rect{fill:var(--panel);stroke:var(--line);stroke-width:1}.nd:focus{outline:none}.nd:focus-visible rect,.nd.sel rect{stroke:var(--ink);stroke-width:2.5}
.s-ok rect{fill:var(--ok-bg);stroke:var(--ok);stroke-width:2}.s-fail rect{fill:var(--fail-bg);stroke:var(--fail);stroke-width:2}.s-diff rect{fill:var(--diff-bg);stroke:var(--diff);stroke-width:2}
.s-risk rect{fill:var(--risk-bg);stroke:var(--risk);stroke-width:2}.s-unt rect{fill:var(--unt-bg);stroke:var(--unt);stroke-width:2}
.c-notest rect{fill:var(--risk-bg);stroke:var(--risk)}.c-unknown rect{stroke-dasharray:3 3}
.e{fill:none;stroke:var(--edge);stroke-width:1.4}.e.guess{stroke-dasharray:5 4}.dim{opacity:.16}.fade{opacity:.3}
table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid var(--soft);text-align:left;vertical-align:top;font-size:13px}th{color:var(--muted);font-weight:600;white-space:nowrap}
td.ev{overflow-wrap:anywhere;min-width:220px}tr.go{cursor:pointer}tr.go:hover td{background:var(--soft)}
#story{margin:12px 20px 0;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 14px}#story:empty{display:none}
#story h2{margin:0 0 6px}#story ol{margin:0;padding:0;list-style:none}#story li{display:flex;gap:10px;padding:4px 0;align-items:baseline}
.tag{flex:none;width:104px;font-size:12px;font-weight:600;border-radius:4px;padding:1px 8px;border:1px solid var(--line);background:var(--soft);text-align:center}
.t-broke{background:var(--diff-bg);border-color:var(--diff)}.t-fixed,.t-checked{background:var(--ok-bg);border-color:var(--ok)}.t-open{background:var(--fail-bg);border-color:var(--fail)}
.t-unchecked{background:var(--unt-bg);border-color:var(--unt)}.t-asked_you,.t-blocked,.t-accepted{background:var(--risk-bg);border-color:var(--risk)}#story code{background:var(--soft);padding:0 3px;border-radius:3px}
.note{color:var(--muted);font-size:12.5px;padding:0 20px 22px;max-width:980px}.empty{padding:16px;color:var(--muted)}
@media (max-width:980px){main{flex-direction:column}aside{width:100%;position:static;max-height:none}}
"""

JS = r"""
const M=JSON.parse(document.getElementById('data').textContent),$=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cut=(s,n)=>s.length>n?s.slice(0,n-1)+'…':s;
const N=new Map(M.nodes.map(n=>[n.id,n])),W=224,H=46,GX=78,GY=12,PX=44,PY=34;
const SC={'Verified pass':'ok','Confirmed failure':'fail','Behavior difference — intent unknown':'diff','Potential risk (static only)':'risk','Untested':'unt'};
let sel=null,fade=false;
const cls=n=>n.kind==='changed'?'s-'+(SC[n.status]||'unt'):(n.tests===false?'c-notest':(n.tests===true?'c-test':'c-unknown'));
function sub(n){let t;if(n.kind==='changed')t=n.status+(n.turned_back?' · turned back':(n.state==='waived'?' · waived':''));
 else t=n.tests===false?'no test reaches it':(n.tests===true?'tests reach it':'test reach unknown');
 return n.routes.length?t+' · '+n.routes[0]:t}
function layout(){const cols=[];for(const n of M.nodes)(cols[n.layer]=cols[n.layer]||[]).push(n);const pos=new Map();
 for(let c=0;c<cols.length;c++){const col=cols[c]=cols[c]||[];
  if(c>0){const rank=n=>Math.min(1e9,...M.edges.filter(e=>e.from===n.id&&pos.has(e.to)).map(e=>pos.get(e.to).y));col.sort((a,b)=>(rank(a)-rank(b))||a.id.localeCompare(b.id))}
  col.forEach((n,r)=>pos.set(n.id,{x:PX+c*(W+GX),y:PY+r*(H+GY)}))}
 return{cols,pos}}
function linked(id){const out=new Set([id]);for(const[a,b]of[['from','to'],['to','from']]){const q=[id];while(q.length){const x=q.pop();for(const e of M.edges)if(e[a]===x&&!out.has(e[b])){out.add(e[b]);q.push(e[b])}}}return out}
function draw(){const v=$('#map');if(!M.nodes.length){v.innerHTML='<div class=empty>No changed functions were compared this session, so there is no radius to draw. Other findings are listed below.</div>';return}
 const{cols,pos}=layout(),rows=Math.max(...cols.map(c=>c.length)),wd=PX*2+cols.length*W+(cols.length-1)*GX,ht=PY+rows*(H+GY)+8,on=sel?linked(sel):null;
 const heads=['Changed this session','Calls it directly','One step further','Two steps further'];
 let s=`<svg width="${wd}" height="${ht}" role="img" aria-label="Changed functions and the functions that depend on them">`;
 cols.forEach((c,i)=>{if(c.length)s+=`<text class=col x="${PX+i*(W+GX)}" y="18">${heads[i]||'Further'}</text>`});
 for(const e of M.edges){const a=pos.get(e.to),b=pos.get(e.from);if(!a||!b)continue;const y1=a.y+H/2,y2=b.y+H/2;let d;
  if(b.x>a.x){const x1=a.x+W,x2=b.x,m=(x2-x1)/2;d=`M${x1} ${y1}C${x1+m} ${y1} ${x2-m} ${y2} ${x2} ${y2}`}
  else if(b.x<a.x){const x1=a.x,x2=b.x+W,m=(x1-x2)/2;d=`M${x1} ${y1}C${x1-m} ${y1} ${x2+m} ${y2} ${x2} ${y2}`}
  else d=`M${a.x} ${y1}C${a.x-30} ${y1} ${a.x-30} ${y2} ${a.x} ${y2}`;
  s+=`<path class="e${e.observed?'':' guess'}${on&&!(on.has(e.from)&&on.has(e.to))?' dim':''}" d="${d}"/>`}
 for(const n of M.nodes){const p=pos.get(n.id),k=[cls(n)];if(sel===n.id)k.push('sel');if(on&&!on.has(n.id))k.push('dim');else if(fade&&n.kind==='caller'&&n.tests===true)k.push('fade');
  s+=`<g class="nd ${k.join(' ')}" data-id="${esc(n.id)}" tabindex="0" role="button" aria-label="${esc(n.name+', '+sub(n))}"><title>${esc(n.name+' ('+n.file+')')}</title><rect x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="5"/><text class=nm x="${p.x+10}" y="${p.y+19}">${esc(cut(n.name,28))}</text><text class=sb x="${p.x+10}" y="${p.y+36}">${esc(cut(sub(n),38))}</text></g>`}
 v.innerHTML=s+'</svg>';
 v.querySelectorAll('.nd').forEach(g=>{const go=ev=>{ev.stopPropagation();pick(g.dataset.id===sel?null:g.dataset.id)};g.onclick=go;g.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();go(ev)}}});
 v.querySelector('svg').onclick=()=>pick(null)}
function panel(){const a=$('aside'),n=sel&&N.get(sel);if(!n){a.innerHTML='<p class=meta>Select a function to see why it is on the map and what was checked.</p>';return}
 const f=(k,x)=>x?`<dt>${k}</dt><dd>${x}</dd>`:'';let h=`<h3>${esc(n.name)}</h3><div class=meta>${esc(n.file)}</div><dl>`;
 if(n.kind==='changed'){h+=f('Status',esc(n.status)+(n.outcome?' ('+esc(n.outcome)+')':''));
  for(const x of n.findings){h+=`<dt>${esc(x.id)} · ${esc(x.status)}${x.outcome?' · '+esc(x.outcome):''}</dt><dd><pre>${esc(x.evidence)}</pre></dd>`;
   if(x.status===M.statuses[2]&&x.state==='open')h+=`<dd>If intended: <code>/legacyguard:waive ${esc(x.id)} "&lt;reason&gt;"</code><br>To keep it from recurring: <code>/legacyguard:pin ${esc(x.id)}</code></dd>`}
  h+=f('Tests',n.tests===null?'Unknown: no test map yet.':(n.tests?esc(n.test_count)+' existing tests run it.':'No existing test runs it.'));
  if(n.cochange&&n.cochange.length)h+=f('Usually changes with',esc(n.cochange.join(', ')));
  if(n.sources.length)h+=f('Note',esc(n.sources.join(', ')))}
 else{h+=f('Why it is here',`It calls <code>${esc(n.via)}</code>, ${n.layer===1?'which changed':'which leads to a changed function'}. Sources: ${esc(n.sources.join(', '))}.`);
  h+=f('Tests',n.tests===null?'Unknown: no test map yet.':(n.tests?'Existing tests reach it.':'No existing test reaches it, so nothing here would catch a break.'));
  h+=f('Checked by the guard?','No. The behavioral diff runs only functions whose own code changed.')}
 if(n.routes.length)h+=f('Reached from',n.routes.map(esc).join('<br>'));a.innerHTML=h+'</dl>'}
function pick(id){sel=id;draw();panel()}
function chips(){const c=M.counts,x=[];for(const s of M.statuses)x.push(`<span class=chip><span class=sw style="background:var(--${SC[s]}-bg);border-color:var(--${SC[s]})"></span><b>${c[s]||0}</b> ${esc(s)}</span>`);
 x.push(`<span class=chip><b>${M.turned_back}</b> turned back</span>`,`<span class=chip>Prompts <b>${M.prompts} of ${M.budget}</b></span>`,`<span class=chip>Wake-ups <b>${M.wakeups}</b></span>`);$('#chips').innerHTML=x.join('')}
function table(){if(!M.findings.length){$('#findings').innerHTML='<div class=empty>No findings this session.</div>';return}
 let h='<table><thead><tr><th>ID</th><th>Status</th><th>Subject</th><th>Evidence</th><th>Outcome</th></tr></thead><tbody>';
 for(const f of M.findings)h+=`<tr${f.node?` class=go data-id="${esc(f.node)}"`:''}><td>${esc(f.id)}</td><td><span class=sw style="background:var(--${SC[f.status]}-bg);border-color:var(--${SC[f.status]})"></span>${esc(f.status)}</td><td>${esc(f.subject)}</td><td class=ev>${esc(f.evidence)}</td><td>${esc(f.outcome)}</td></tr>`;
 $('#findings').innerHTML=h+'</tbody></table>';document.querySelectorAll('tr.go').forEach(r=>r.onclick=()=>{pick(r.dataset.id);$('#map').scrollIntoView({block:'nearest'})})}
function storyHtml(){if(!M.story.length)return;const md=t=>esc(t).replace(/`([^`]+)`/g,'<code>$1</code>');
 $('#story').innerHTML='<h2>What happened</h2><ol>'+M.story.map(s=>`<li><span class="tag t-${s.kind}">${esc(s.label)}</span><span>${md(s.text)}</span></li>`).join('')+'</ol>'}
storyHtml();
$('#fade').onchange=e=>{fade=e.target.checked;draw()};
const notes=[];if(!M.index)notes.push('There is no index, so dependents are unknown. Run /legacyguard:index.');else if(!M.coverage)notes.push('There is no test map yet, so test reach is unknown. Run /legacyguard:index.');
if(M.truncated)notes.push(M.truncated+' more callers are not drawn.');$('#warn').textContent=notes.join(' ');
chips();table();const first=M.nodes.find(n=>n.kind==='changed');pick(first?first.id:null);
"""


def render_html(model: dict) -> str:
    blob = json.dumps(model, ensure_ascii=False).replace("<", "\\u003c")
    meta = f"Session {model['session'] or 'unknown'} · mode {model['mode']} · baseline {model['baseline'] or 'none'} · written {model['generated']}"
    safe_meta = meta.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LegacyGuard session map</title><style>{CSS}</style></head><body>
<header><h1>LegacyGuard session map</h1><p class="meta">{safe_meta}</p><div class="chips" id="chips"></div></header>
<section id="story"></section>
<main><div id="left">
<div class="tools"><span><svg width="30" height="8"><path class="e" d="M0 4H30"/></svg> one test runs both ends</span>
<span><svg width="30" height="8"><path class="e guess" d="M0 4H30"/></svg> matched by name only</span>
<span><span class="sw" style="background:var(--risk-bg);border-color:var(--risk)"></span>depends on a change, no test reaches it</span>
<label><input type="checkbox" id="fade"> Fade callers that tests reach</label></div>
<div class="box" id="map"></div><p class="meta" id="warn" style="margin-top:6px"></p>
<h2>Findings</h2><div class="box" id="findings"></div></div><aside></aside></main>
<p class="note">{model['note']} {model['footer']}</p>
<script type="application/json" id="data">{blob}</script><script>{JS}</script></body></html>
"""


def write(cfg, data: dict) -> Path:
    folder = cfg.state_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "session"))
    path = folder / f"{name}.html"
    path.write_text(render_html(build(cfg, data)))
    return path
