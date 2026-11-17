"""stage_comments.py - highlight-to-comment on the stage's html pages.

Inside a stage page (sandboxed iframe) the injected COMMENTER shows a floating 💬 Comment button when text is
selected; the note is posted to the app page, which stores it here via /api/comments. Notes show under the selected
text. Send hands every unsent note to the lane that published the page (item "lane", else main) as a task, so that
head can make the edits and publish it again. Store: <state>/stage/comments/<person>.json {item_id: [comment, ...]}.
Made for reviewing drafts on a phone: highlight a sentence, say what should change, send the batch.
"""
import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import stage_report  # noqa: E402  (puts harness/ and tg/ on sys.path; lane_of + the handoff)

DIR = os.path.join(os.path.dirname(stage_report.DIR), "comments")
MAX_PER_ITEM = 200
_lock = threading.Lock()


def _path(person):
    return os.path.join(DIR, f"{person}.json")


def _load(person):
    try:
        with open(_path(person)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(person, d):
    os.makedirs(DIR, exist_ok=True)
    p = _path(person)
    with open(p + ".tmp", "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _s(v, n):
    return str(v or "")[:n]


def listing(person, iid):
    return _load(person).get(str(iid), [])


def add(person, iid, body, now=time.time):
    """-> the item's comment list after adding one. Raises ValueError on an empty note or quote, or a full item."""
    c = {"id": f"c{int(now() * 1000)}", "quote": _s(body.get("quote"), 2000).strip(),
         "prefix": _s(body.get("prefix"), 80), "suffix": _s(body.get("suffix"), 80),
         "note": _s(body.get("note"), 4000).strip(), "ts": now(), "sent_ts": None}
    if not c["quote"] or not c["note"]:
        raise ValueError("a comment needs selected text and a note")
    with _lock:
        d = _load(person)
        lst = d.setdefault(str(iid), [])
        if len(lst) >= MAX_PER_ITEM:
            raise ValueError("too many comments on this stage")
        lst.append(c)
        _save(person, d)
        return lst


def delete(person, iid, cid):
    with _lock:
        d = _load(person)
        lst = [c for c in d.get(str(iid), []) if c["id"] != cid]
        d[str(iid)] = lst
        _save(person, d)
        return lst


def message(person, item, comments):
    """The task text the publishing head gets. Pure."""
    t = item.get("title") or f"stage {item.get('id')}"
    lines = [f"{i}. On \"{c['quote'][:300]}\"\n   -> {c['note']}" for i, c in enumerate(comments, 1)]
    return (f"💬 {person.capitalize()} commented on your stage page \"{t}\" (id {item.get('id')}). "
            f"Make the edits, publish it to {person} again, and reply in your thread with what changed.\n\n"
            + "\n".join(lines))


def send(person, item, now=time.time, handoff=stage_report._handoff):
    """Hand every unsent comment to the publishing lane. -> dict for the API. A failed handoff is said, nothing marked."""
    iid = str(item["id"])
    with _lock:
        todo = [c for c in _load(person).get(iid, []) if not c.get("sent_ts")]
    if not todo:
        return {"ok": True, "sent": 0, "why": "no new comments"}
    lane = stage_report.lane_of(item)
    r = handoff(lane, message(person, item, todo)) or {}
    if not r.get("ok"):
        return {"ok": False, "sent": 0, "lane": lane, "why": str(r.get("why") or r)[-200:]}
    ids = {c["id"] for c in todo}
    with _lock:
        d = _load(person)
        for c in d.get(iid, []):
            if c["id"] in ids:
                c["sent_ts"], c["sent_to"] = now(), lane
        _save(person, d)
        lst = d.get(iid, [])
    return {"ok": True, "sent": len(ids), "lane": lane, "comments": lst}


# Injected into every html stage page by the app (index.html adds it to srcdoc, so pages published earlier get it too).
COMMENTER = r"""<script>(()=>{if(window.__jc)return;window.__jc=1;
const css=`.jc-btn{position:absolute;z-index:2147483646;background:#0ea5e9;color:#fff;border:0;border-radius:18px;padding:8px 14px;font:600 15px system-ui;box-shadow:0 2px 10px #0006;cursor:pointer}
.jc-box{position:absolute;z-index:2147483647;left:8px;right:8px;background:#1e293b;color:#e2e8f0;border:1px solid #0ea5e9;border-radius:10px;padding:10px;font:14px system-ui;box-shadow:0 4px 20px #0008}
.jc-box textarea{width:100%;box-sizing:border-box;min-height:70px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:6px;font:15px system-ui}
.jc-box button{margin:6px 6px 0 0;padding:6px 12px;border-radius:6px;border:0;font:600 14px system-ui;cursor:pointer}
mark.jc-hl{background:#fde68a;color:inherit}
.jc-note{display:block;margin:4px 0 8px;padding:6px 8px;border-left:3px solid #f59e0b;background:#fff7ed;color:#9a3412;font:14px system-ui;border-radius:4px;white-space:pre-wrap}
.jc-note .x{float:right;cursor:pointer;opacity:.6;margin-left:8px}.jc-note.sent{border-left-color:#22c55e;background:#f0fdf4;color:#166534}`;
let st=document.createElement('style');st.textContent=css;
const ready=f=>document.readyState==='loading'?addEventListener('DOMContentLoaded',f):f();
let btn,box,sel=null;const post=m=>parent.postMessage(Object.assign({hydraComment:1},m),'*');
const hide=()=>{btn&&(btn.style.display='none')};
function ctx(r){const t=document.body.innerText||'';const q=r.toString();const i=t.indexOf(q);
return{quote:q,prefix:i>0?t.slice(Math.max(0,i-40),i):'',suffix:i>=0?t.slice(i+q.length,i+q.length+40):''}}
ready(()=>{document.head.appendChild(st);
btn=document.createElement('button');btn.className='jc-btn';btn.textContent='💬 Comment';btn.style.display='none';document.body.appendChild(btn);
const show=()=>{const s=getSelection();if(!s||s.isCollapsed||!s.toString().trim()||(box&&box.contains(s.anchorNode))){hide();return}
const r=s.getRangeAt(0),b=r.getBoundingClientRect();sel=ctx(r);
btn.style.top=(scrollY+b.bottom+8)+'px';btn.style.left=Math.max(8,Math.min(scrollX+b.left,innerWidth-130))+'px';btn.style.display='block'};
document.addEventListener('selectionchange',()=>setTimeout(show,50));
const open=e=>{e.preventDefault();if(!sel)return;const top=btn.style.top;hide();box&&box.remove();
box=document.createElement('div');box.className='jc-box';box.style.top=top;
box.innerHTML='<div style="opacity:.8;margin-bottom:6px"></div><textarea placeholder="Your note"></textarea><br><button style="background:#0ea5e9;color:#fff">Save</button><button style="background:#334155;color:#e2e8f0">Cancel</button>';
box.firstChild.textContent='“'+sel.quote.slice(0,120)+(sel.quote.length>120?'…':'')+'”';document.body.appendChild(box);
const [ok,no]=box.querySelectorAll('button'),ta=box.querySelector('textarea');ta.focus();
no.onclick=()=>{box.remove();box=null};ok.onclick=()=>{const n=ta.value.trim();if(!n)return;post({op:'add',...sel,note:n});box.remove();box=null;getSelection().removeAllRanges()}};
btn.addEventListener('pointerdown',open);
post({op:'hello'})});
function find(c){const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode:n=>n.parentNode.closest('.jc-note,.jc-box,script,style')?2:1});
const ns=[];let all='',n;while(n=w.nextNode()){ns.push([n,all.length]);all+=n.data}
let i=all.indexOf(c.prefix+c.quote);i=i>=0?i+c.prefix.length:all.indexOf(c.quote);if(i<0)return null;
const at=k=>{for(let j=ns.length-1;j>=0;j--)if(ns[j][1]<=k)return[ns[j][0],k-ns[j][1]]};
const r=document.createRange();const[a,ao]=at(i),[b,bo]=at(i+c.quote.length-1);r.setStart(a,ao);r.setEnd(b,bo+1);return r}
function paint(list){document.querySelectorAll('.jc-note').forEach(x=>x.remove());
document.querySelectorAll('mark.jc-hl').forEach(m=>{m.replaceWith(...m.childNodes)});document.body.normalize();
for(const c of list){const r=find(c);let m=null;
if(r){try{m=document.createElement('mark');m.className='jc-hl';m.appendChild(r.extractContents());r.insertNode(m)}catch(e){m=null}}
const blk=m?m.closest('p,li,h1,h2,h3,h4,h5,h6,td,blockquote,div,section')||m:null;
const d=document.createElement('div');d.className='jc-note'+(c.sent_ts?' sent':'');d.textContent=(c.sent_ts?'✓ ':'💬 ')+c.note+(m?'':'  (on: “'+c.quote.slice(0,60)+'”)');
if(!c.sent_ts){const x=document.createElement('span');x.className='x';x.textContent='✕';x.onclick=()=>post({op:'del',cid:c.id});d.prepend(x)}
blk?(blk.tagName==='TD'?blk.appendChild(d):blk.after(d)):document.body.appendChild(d)}}
addEventListener('message',e=>{if(e.source===parent&&e.data&&e.data.hydraComments)ready(()=>paint(e.data.hydraComments))});})();</script>"""
