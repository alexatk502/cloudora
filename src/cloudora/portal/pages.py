"""Portal HTML pages. Every dynamic value is HTML-escaped. The only scripts are Cloudora's own: the sign-in script
(pinned in the CSP by its hash) and the viewer (/static/viewer.js, CSP script-src 'self')."""

from __future__ import annotations

import base64
import hashlib
from html import escape as e
from pathlib import Path

CSS = """
:root{
 --bg:#0e0f11;--surface:#16171a;--surface-2:#1d1f23;--line:#2a2c31;--line-2:#383b42;
 --text:#ececee;--text-2:#a9acb3;--text-3:#757981;
 --red:#e5383b;--red-ink:#ff7477;--red-deep:#3a1416;--ok:#4cc38a;--ok-deep:#11291d;--warn:#e9a640;--warn-deep:#2c2210;
 --sans:Inter,"Inter var",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif;
 --mono:ui-monospace,"JetBrains Mono","SF Mono","Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;
 --r:6px;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 var(--sans);font-feature-settings:"cv11","ss01";
 -webkit-font-smoothing:antialiased}
a{color:var(--red-ink);text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{color:#fff}
:focus-visible{outline:2px solid var(--red);outline-offset:2px}
.top{display:flex;align-items:center;gap:14px;max-width:1240px;margin:0 auto;padding:16px 24px}
.top-line{border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;color:var(--text);text-decoration:none}
.brand svg{width:26px;height:26px;color:var(--red);flex:none}
.brand b{font-weight:650;letter-spacing:.02em;font-size:16px}
.brand i{font-style:normal;color:var(--text-3);font:500 11px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
 border-left:1px solid var(--line-2);padding-left:10px;margin-left:2px}
main{max-width:680px;margin:40px auto 64px;padding:0 20px}
h1.page{font-size:30px;line-height:1.15;letter-spacing:-.015em;font-weight:650;margin:0 0 6px}
p.lede{color:var(--text-2);margin:0 0 28px;max-width:56ch}
code,.mono{font-family:var(--mono);font-size:.92em}
code{background:var(--surface-2);border:1px solid var(--line);padding:1px 6px;border-radius:4px}
input{width:100%;padding:11px 13px;border-radius:var(--r);border:1px solid var(--line-2);background:var(--bg);
 color:var(--text);font:15px var(--sans);transition:border-color .12s}
input::placeholder{color:var(--text-3)}
input:focus{outline:none;border-color:var(--text-2);box-shadow:0 0 0 3px rgba(229,56,59,.18)}
button{margin-top:16px;padding:10px 18px;border-radius:var(--r);border:1px solid var(--red);background:var(--red);
 color:#fff;font:600 14.5px var(--sans);cursor:pointer;transition:background .12s,border-color .12s}
button:hover{background:#f04447}
.err,.warn{border-radius:var(--r);padding:11px 14px;margin-bottom:14px;font-size:14px;border:1px solid}
.err{background:var(--red-deep);border-color:#6b2023;color:#ffd9da}
.warn{background:var(--warn-deep);border-color:#5b4313;color:#f6e2bd}
label{display:block;margin:14px 0 6px;color:var(--text-2);font-size:13px;font-weight:500}
.note{display:flex;gap:10px;align-items:baseline;color:var(--text-2);font-size:13.5px;margin:22px 0 0}
.note b{color:var(--text);font-weight:600;white-space:nowrap}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:8px 24px 24px}
.panel button[type=submit]{width:100%;padding:12px;margin-top:20px}
.panel h2{margin:16px 0 8px;font-size:17px}
.panel p{color:var(--text-2);margin:0 0 8px}
/* the screen viewer */
body.viewer{height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden;background:#000}
#stage{flex:1;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;min-height:0}
#screen{display:block;flex:none;touch-action:none;outline:none;background:#000;cursor:default;user-select:none;
 -webkit-user-select:none;-webkit-touch-callout:none}
#cursor{position:absolute;left:0;top:0;width:16px;height:22px;pointer-events:none;z-index:2;
 background:no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='22'%3E%3Cpath d='M1 1v17l4.5-4.2 3 6.7 2.6-1.2-3-6.6H14z' fill='%23fff' stroke='%23000' stroke-width='1.3' stroke-linejoin='round'/%3E%3C/svg%3E")}
#cursor[hidden]{display:none}
#msg{position:absolute;left:16px;right:16px;top:50%;transform:translateY(-50%);margin:0 auto;max-width:460px;z-index:3;
 background:var(--surface);border:1px solid var(--line-2);border-radius:12px;padding:18px 20px;display:flex;
 flex-direction:column;gap:6px;text-align:center}
#msg[hidden]{display:none}
#msg b{font-size:15px}#msg span{color:var(--text-2);font-size:14px}
#msg button{align-self:center;margin-top:8px}
@media (max-width:640px){
 main{margin:26px auto 48px}
 h1.page{font-size:25px}
 .top{padding:14px 18px}
 .panel{padding:4px 18px 20px}
}
"""

CSS_VERSION = hashlib.sha256(CSS.encode()).hexdigest()[:10]
VIEWER_JS = (Path(__file__).with_name("viewer.js")).read_text(encoding="utf-8")
VIEWER_VERSION = hashlib.sha256(VIEWER_JS.encode()).hexdigest()[:10]

# The mark: a cloud, drawn with presentation attributes only (the portal allows no inline styles).
MARK = ('<svg viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 24h14a6 6 0 0 0 .9-11.93A8.5 8.5 0 0 0 8.1 '
        '14.6 4.8 4.8 0 0 0 9.5 24z"/><path d="M12.5 19.5h7" stroke-width="1.5"/></svg>')


def layout(title: str, body: str, head_first: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head>{head_first}<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="same-origin"><title>{e(title)} · Cloudora</title>
<link rel="stylesheet" href="/static/portal.css?v={CSS_VERSION}"></head>
<body><header class="top-line"><div class="top"><a class="brand" href="/">{MARK}<b>Cloudora</b><i>Remote</i></a>
</div></header>
<main>{body}</main></body></html>"""


def message(kind: str, text: str) -> str:
    return f'<div class="{kind}">{e(text)}</div>' if text else ""


# The sign-in page's only script (pinned in the CSP by its hash): it turns the typed key into a one-time proof, so
# the key itself never leaves the browser (see cloudora/secret.py). The key field is disabled before the form is sent,
# so browsers never submit it.
LOGIN_JS = r"""(()=>{"use strict";
const d=document,sub=HTMLFormElement.prototype.submit,S=self.crypto&&self.crypto.subtle,TE=TextEncoder;
const hex=b=>Array.from(new Uint8Array(b),x=>x.toString(16).padStart(2,"0")).join("");
const norm=k=>{k=k.toUpperCase().replace(/[^A-Z0-9]/g,"");return k.startsWith("CLDR")||k.startsWith("SPDR")?k.slice(4):k;};
d.addEventListener("submit",async ev=>{const f=ev.target;if(!(f instanceof HTMLFormElement)||f.getAttribute("action")!=="/login")return;
ev.preventDefault();const k=f.elements.key,p=f.elements.proof,c=f.elements.challenge,m=d.getElementById("login-msg");
if(!S){m.textContent="This browser cannot sign in safely here (no WebCrypto). Use a current browser over https://.";m.hidden=false;return;}
try{const dg=await S.digest("SHA-256",new TE().encode(norm(k.value)));
const hk=await S.importKey("raw",dg,{name:"HMAC",hash:"SHA-256"},false,["sign"]);
p.value=hex(await S.sign("HMAC",hk,new TE().encode("cloudora-sign-in-v1|"+c.value+"|"+location.origin)));
k.value="";k.disabled=true;sub.call(f);}catch(e){m.textContent="Signing in failed in this browser: "+e;m.hidden=false;}
},true);})();"""
LOGIN_JS_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(LOGIN_JS.encode()).digest()).decode() + "'"


def login_page(csrf: str, challenge: str, error: str = "") -> str:
    return layout("Sign in", f"""<h1 class="page">Sign in</h1>
<p class="lede">Paste this computer&#39;s secret key to see and control its screen.</p>
{message("err", error)}<div class="err" id="login-msg" role="alert" hidden></div>
<noscript><div class="warn">Signing in needs JavaScript: your browser proves it has the key without ever sending the
key itself.</div></noscript>
<form method="post" action="/login" autocomplete="off" class="panel">
<input type="hidden" name="csrf" value="{e(csrf)}"><input type="hidden" name="challenge" value="{e(challenge)}">
<input type="hidden" name="proof" value="">
<label for="key">Secret key</label>
<input id="key" name="key" type="password" maxlength="80" required autofocus autocomplete="off" spellcheck="false"
 autocapitalize="characters" class="mono" placeholder="CLDR-XXXXX-XXXXX-...">
<button type="submit">Sign in</button></form>
<p class="note"><b>Safety</b><span>The key never leaves this browser: it only proves it has it, once, to this address.
Anyone with the key can control this computer, so keep it private.</span></p>""",
                  head_first=f"<script>{LOGIN_JS}</script>")


def viewer_page(screen_token: str, webtransport: str = "") -> str:
    """The screen, full window - nothing else. All behaviour is in /static/viewer.js; the page only carries its token."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="referrer" content="same-origin"><title>Cloudora</title>
<link rel="stylesheet" href="/static/portal.css?v={CSS_VERSION}"></head>
<body class="viewer"><div id="app" data-token="{e(screen_token)}" data-wt="{e(webtransport)}" hidden></div>
<div id="stage"><canvas id="screen" tabindex="0" aria-label="The remote computer's screen"></canvas>
<div id="cursor" hidden></div>
<div id="msg" role="status"><b id="msg-title">Connecting…</b><span id="msg-text"></span>
<button id="msg-retry" type="button" hidden>Reconnect</button></div></div>
<script src="/static/viewer.js?v={VIEWER_VERSION}"></script></body></html>"""


def error_page(title: str, text: str, status_hint: str = "") -> str:
    hint = f"<p>{e(status_hint)}</p>" if status_hint else ""
    return layout(title, f"""<div class="panel"><h2>{e(title)}</h2><p>{e(text)}</p>{hint}
<p><a href="/">Back to Cloudora</a></p></div>""")
