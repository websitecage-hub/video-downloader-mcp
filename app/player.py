"""Browser UI for analysis sessions.

One template serves two hosts:
- ``/watch/{session_id}`` renders it server-side with the session data inlined.
- the ``ui://video-analysis/player.html`` MCP Apps resource renders it inside a
  host iframe and receives the same data via ``ui/notifications/tool-result``.
"""

from __future__ import annotations

import html
import json
from typing import Any

_CSS = """
:root{color-scheme:dark;--bg:#0b0f17;--card:#141b27;--line:#243044;--text:#e8eef8;--muted:#93a4bd;--accent:#5eead4}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#16233a 0%,var(--bg) 60%);color:var(--text);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 18px 48px}
h1{font-size:20px;margin:0 0 6px;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:13px;margin-bottom:14px}
.meta b{color:var(--accent);font-weight:600}
video{width:100%;max-height:430px;background:#000;border:1px solid var(--line);border-radius:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
button,a.btn{background:#0f1722;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:8px 14px;font-size:13px;cursor:pointer;text-decoration:none}
button:hover,a.btn:hover{border-color:var(--accent);color:var(--accent)}
input[type=search]{flex:1;min-width:200px;background:#0a1018;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:9px 12px;font-size:14px}
.card{background:linear-gradient(180deg,#151d2a,#111823);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
.card h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.ch{display:block;width:100%;text-align:left;background:#0d141e;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;color:var(--text);cursor:pointer}
.ch:hover{border-color:var(--accent)}
.ch .t{color:var(--accent);font-family:ui-monospace,Menlo,monospace;font-size:12px}
.ch .s{display:block;color:var(--muted);font-size:13px;margin-top:4px}
.tr{padding:7px 10px;border-radius:8px;cursor:pointer;font-size:14px}
.tr:hover{background:#0d141e}
.tr .t{color:var(--accent);font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-right:8px}
.msg{color:var(--muted);font-size:14px;padding:10px}
.err{color:#fca5a5}
a{color:var(--accent)}
img.sheet{width:100%;border-radius:10px;border:1px solid var(--line)}
"""

_JS = r"""
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
function fmt(t){t=Math.max(0,Math.floor(t||0));const h=(t/3600)|0,m=((t%3600)/60)|0,s=t%60;return h?h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'):m+':'+String(s).padStart(2,'0')}
let connected=false,appId=1;
function send(msg){try{window.parent.postMessage(msg,'*')}catch(e){}}
function sizeChanged(){send({jsonrpc:'2.0',method:'ui/notifications/size-changed',params:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}})}
function openLink(url){
  if(connected){send({jsonrpc:'2.0',id:++appId,method:'ui/open-link',params:{url:url}})}
  try{window.open(url,'_blank')}catch(e){}
}
function onMessage(ev){
  const d=ev.data;if(!d||typeof d!=='object')return;
  if(d.id===1&&d.result){connected=true;send({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}});sizeChanged()}
  if(d.method==='ui/notifications/tool-result'){
    const p=d.params||{};let data=null;
    if(p.structuredContent&&p.structuredContent.session_id)data=p.structuredContent;
    if(!data){const t=(p.content||[]).find(c=>c.type==='text');if(t){try{data=JSON.parse(t.text)}catch(e){data=null;if(t.text&&!data){document.getElementById('view').innerHTML='<p class="msg">This tool result has no player data. Call open_video with a video URL first.</p>';return}}}}
    if(data&&data.status==='error'){document.getElementById('view').innerHTML='<p class="msg err">'+esc(data.error||'Analysis failed')+'</p>';return}
    if(data)render(data);
    sizeChanged();
  }
  if(d.method==='ui/notifications/tool-input'){document.getElementById('view').innerHTML='<p class="msg">Analyzing the video… this can take up to a couple of minutes on the free server.</p>'}
}
function render(d){
  const v=document.getElementById('view');
  const chapters=d.timeline||[];const transcript=(d.transcript_segments)||(d.segments)||[];
  let h='<h1>'+esc(d.title||'Video analysis')+'</h1>';
  h+='<div class="meta">'+esc(d.platform||'')+(d.uploader?' · '+esc(d.uploader):'')+(d.duration_string?' · <b>'+esc(d.duration_string)+'</b>':'')+(d.resolution?' · '+esc(d.resolution):'')+(d.transcript_source&&d.transcript_source!=='none'?' · captions: '+esc(d.transcript_source):'')+'</div>';
  if(d.playback_url){h+='<video id="v" controls preload="metadata" src="'+esc(d.playback_url)+'"></video>'}
  h+='<div class="row">';
  if(d.watch_url)h+='<button onclick="openLink('+JSON.stringify(d.watch_url)+')">Open standalone player</button>';
  if(d.download_url||d.playback_url)h+='<a class="btn" href="'+esc(d.download_url||d.playback_url)+'" target="_blank" rel="noopener">Download video</a>';
  if(transcript.length)h+='<input type="search" id="q" placeholder="Search the transcript…" oninput="filterTr(this.value)">';
  h+='</div>';
  if(d.contact_sheet_url)h+='<img class="sheet" src="'+esc(d.contact_sheet_url)+'" alt="contact sheet">';
  if(chapters.length){
    h+='<div class="card"><h2>Chapters</h2>';
    chapters.forEach(c=>{h+='<button class="ch" data-t="'+(c.start||0)+'"><span class="t">'+esc(c.time||(fmt(c.start)+'–'+fmt(c.end)))+' · Ch '+(c.chapter||c.number||'')+'</span> '+esc(c.title||'')+'<span class="s">'+esc(c.summary||'')+'</span></button>'});
    h+='</div>';
  }
  if(transcript.length){
    h+='<div class="card"><h2>Transcript</h2><div id="tr">';
    transcript.slice(0,600).forEach(s=>{h+='<div class="tr" data-t="'+(s.start||0)+'"><span class="t">'+esc(s.time||fmt(s.start))+'</span>'+esc(s.text||'')+'</div>'});
    h+='</div></div>';
  }
  if(!d.playback_url){h+='<p class="msg">Playback link expired. Ask the assistant to open the video again.</p>'}
  v.innerHTML=h;
  v.querySelectorAll('.ch,.tr').forEach(el=>el.addEventListener('click',()=>{const q=document.getElementById('v');if(q){q.currentTime=parseFloat(el.dataset.t||0);q.play().catch(()=>{})}}));
  sizeChanged();
}
function filterTr(q){q=(q||'').toLowerCase();document.querySelectorAll('#tr .tr').forEach(el=>{el.style.display=!q||el.textContent.toLowerCase().includes(q)?'':'none'})}
window.addEventListener('message',onMessage);
window.addEventListener('load',()=>{if(window.__SESSION_DATA__){render(window.__SESSION_DATA__)}else{send({jsonrpc:'2.0',id:1,method:'ui/initialize',params:{appInfo:{name:'Video Analysis Player',version:'1.0.0'},appCapabilities:{availableDisplayModes:['inline','fullscreen']},protocolVersion:'2026-01-26'}})}if(window.ResizeObserver){new ResizeObserver(sizeChanged).observe(document.body)}});
"""


def render_player(data: dict[str, Any] | None) -> str:
    boot = ""
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        boot = f"<script>window.__SESSION_DATA__={payload};</script>"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Video analysis</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\"><div id=\"view\">"
        "<p class=\"msg\">Waiting for the analysis result…</p></div></div>"
        f"{boot}<script>{_JS}</script></body></html>"
    )


def watch_page_data(session: Any) -> dict[str, Any]:
    """Flatten a Session into the JSON shape the player template expects."""
    payload: dict[str, Any] = {
        "status": "success",
        "session_id": session.id,
        "title": session.title,
        "platform": session.platform,
        "uploader": session.bundle.info.get("uploader")
        or session.bundle.info.get("channel")
        or session.bundle.info.get("creator"),
        "duration_string": _fmt(session.media_info.duration or session.duration),
        "resolution": f"{session.media_info.width}x{session.media_info.height}",
        "transcript_source": session.transcript_source,
        "playback_url": session.playback_url,
        "watch_url": session.watch_url,
        "timeline": [
            {
                "chapter": chapter["index"] + 1,
                "start": chapter["start"],
                "time": f"{_fmt(chapter['start'])}–{_fmt(chapter['end'])}",
                "title": chapter["title"],
                "summary": chapter["summary"],
            }
            for chapter in session.chapters
        ],
        "transcript_segments": [
            {"start": segment["start"], "time": _fmt(segment["start"]), "text": segment["text"]}
            for segment in session.transcript[:600]
        ],
    }
    if session.contact_sheet:
        payload["contact_sheet_url"] = session.asset_url(session.contact_sheet)
    return payload


def _fmt(seconds: float | int | None) -> str:
    try:
        value = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        value = 0
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
