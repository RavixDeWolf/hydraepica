"""stage_report.py - a stage html page tells the head that published it when it breaks.

The stage injects a small reporter into every html stage page (REPORTER below). Inside the sandbox it catches
uncaught errors, rejected promises, console.error, and a canvas that drew nothing; it hands them to the app page
(postMessage), which POSTs them to /api/stage-report. Here they are cleaned, rate-limited (one report per stage per
10 min), saved under <state>/stage/stage_reports/, and handed to the publishing head's lane as a task (tg/handoff.py).
Why: a page that went NaN and drew nothing was only noticed from the owner's screenshot.
"""
import base64
import json
import os
import re
import sys
import time

_H = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # harness/
sys.path[:0] = [_H, os.path.join(_H, "tg")]
import config  # noqa: E402

DIR = os.path.join(os.environ.get("STAGE_STATE", os.path.join(config.STATE_DIR, "stage")), "stage_reports")
GAP_S = 600
MAX_MSGS = 10
LANE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_last = {}

REPORTER = r"""<script>(()=>{const P=[],T={};let sent=0;
const add=(k,m)=>{if(P.length<10)P.push({k,m:String(m).slice(0,300)})};
addEventListener('error',e=>add('error',(e.message||e)+(e.lineno?' (line '+e.lineno+')':'')));
addEventListener('unhandledrejection',e=>add('rejection',e.reason&&e.reason.stack||e.reason));
const ce=console.error;console.error=function(...a){add('console',a.join(' '));return ce.apply(this,a)};
const gc=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(t,...r){this.__t=t;return gc.call(this,t,...r)};
const blank=c=>{try{if(c.__t!=='2d'||!c.width||!c.height)return false;const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
for(let i=4;i<d.length;i+=4*Math.max(1,(d.length/4/4000)|0))if(d[i]!==d[0]||d[i+1]!==d[1]||d[i+2]!==d[2]||d[i+3]!==d[3])return false;return true}catch(e){return false}};
const send=()=>{const b=[...document.querySelectorAll('canvas')].filter(blank);b.forEach(()=>add('blank','a 2d canvas drew nothing (one flat colour)'));
if(!P.length||sent)return;sent=1;let snap=null;try{const c=document.querySelector('canvas');if(c&&c.width)snap=c.toDataURL('image/png')}catch(e){}
parent.postMessage({hydraStage:1,problems:P,snap},'*')};
addEventListener('load',()=>setTimeout(send,4000));setTimeout(send,8000);})();</script>"""


def inject(html):
    """Put the reporter first in <head> (or first of all), once. Never before a doctype (quirks mode). Pure."""
    if "hydraStage" in html:
        return html
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[:m.end()] + REPORTER + html[m.end():]
    d = re.match(r"\s*<!doctype[^>]*>", html, re.I)
    return html[:d.end()] + REPORTER + html[d.end():] if d else REPORTER + html


def clean(body):
    """-> list of {k, m} or [] . Pure."""
    out = []
    for p in (body.get("problems") or [])[:MAX_MSGS]:
        if isinstance(p, dict) and p.get("k") in ("error", "rejection", "console", "blank"):
            out.append({"k": p["k"], "m": str(p.get("m") or "")[:300]})
    return out


def lane_of(item):
    lane = (item or {}).get("lane")
    return lane if isinstance(lane, str) and LANE_RE.match(lane) else "main"


def message(item, problems, snap_path):
    """The task text the head gets. Pure."""
    lines = [f"{p['k']}: {p['m']}" for p in problems]
    t = item.get("title") or item.get("id")
    s = f"\nSnapshot: {snap_path}" if snap_path else ""
    return (f"🧩 Your stage page \"{t}\" (id {item.get('id')}) reported problems when it opened on the Mini App. "
            "Fix it and re-publish, and tell the owner only if it changes what they saw.\n" + "\n".join(lines) + s)


def _handoff(lane, text):
    import handoff
    try:
        return handoff.handoff(lane, text, frm="stage")
    except SystemExit as e:                          # unknown lane etc.: said back to the caller, never swallowed
        return {"ok": False, "why": str(e)}


def report(person, item, body, now=time.time, send=_handoff):
    """-> dict for the API. Saves + hands off, at most once per stage per GAP_S."""
    problems = clean(body)
    if not problems:
        return {"ok": True, "sent": False, "why": "nothing to report"}
    key = f"{person}:{item['id']}"
    if now() - _last.get(key, 0) < GAP_S:
        return {"ok": True, "sent": False, "why": "already reported"}
    _last[key] = now()
    os.makedirs(DIR, exist_ok=True)
    stem = os.path.join(DIR, f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^A-Za-z0-9_-]', '', str(item['id']))}")
    snap_path = None
    snap = body.get("snap")
    if isinstance(snap, str) and snap.startswith("data:image/png;base64,") and len(snap) < 3_000_000:
        snap_path = stem + ".png"
        with open(snap_path, "wb") as f:
            f.write(base64.b64decode(snap.split(",", 1)[1]))
    lane = lane_of(item)
    with open(stem + ".json", "w") as f:
        json.dump({"person": person, "id": item["id"], "title": item.get("title"), "lane": lane,
                   "problems": problems, "snap": snap_path}, f, indent=1)
    r = send(lane, message(item, problems, snap_path)) or {}
    return {"ok": bool(r.get("ok")), "sent": bool(r.get("ok")), "lane": lane,
            **({} if r.get("ok") else {"why": str(r.get("why") or r)[-200:]})}
