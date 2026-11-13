// Cloudora's screen viewer: the remote screen, full window, with its sound; mouse and keyboard go back.
// Everything adapts by itself - there are no settings.
//
// Pictures arrive as video (AV1, HEVC, VP9 or H.264: the best this browser decodes, with WebCodecs, usually in the
// video hardware) or, in browsers without WebCodecs, as JPEG patches of the changed areas. Every picture is
// acknowledged once decoded, so the computer always knows how far behind the viewer is and never builds a queue.
// Video is shown at the pace it was made, smoothing out the network's bursts (see "Pacing").
//
// Ctrl+Alt+M captures the mouse for games (mouse-look: relative, unaccelerated movement; Esc releases it). While it
// is captured the computer also switches to its lowest-latency settings.
(() => {
"use strict";
const $ = id => document.getElementById(id);
const TOKEN = $("app").dataset.token;
const cv = $("screen"), stage = $("stage"), cur = $("cursor");
const ctx = cv.getContext("2d", {alpha: false, desynchronized: true});
const msgBox = $("msg"), msgTitle = $("msg-title"), msgText = $("msg-text"), retryBtn = $("msg-retry");

let ws = null, retry = 0, ended = false, W = 0, H = 0, chain = Promise.resolve();
let viewOnly = false, showCursor = false, hostRepeats = true, mouseInside = false, serverVideo = false;
let serverAudio = false;
let decodable = [];          // video codecs this browser decodes (WebCodecs), best first
let decoder = null, decCodec = "", decW = 0, decH = 0, needKey = true, keyAsked = 0;
const decodeStart = new Map();   // sequence -> [when its chunk went to the decoder, host time it was made]
let pending = null, raf = 0;
const wheel = {dx: 0, dy: 0};
const held = new Set();      // keys held down on this side
const buttons = new Set();   // mouse buttons held down on this side

// ------------------------------------------------------------------ messages on screen
let msgTimer = 0;
function show(title, text, canRetry) {
  clearTimeout(msgTimer); msgTimer = 0;
  msgTitle.textContent = title; msgText.textContent = text || "";
  retryBtn.hidden = !canRetry; msgBox.hidden = false;
}
function hideMsg() { msgBox.hidden = true; }
function note(text) {                      // a passing notice over the picture
  show(text, "", false);
  msgTimer = setTimeout(() => { msgTimer = 0; hideMsg(); }, 4000);
}
const send = o => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };

// ------------------------------------------------------------------ size and settings (all automatic)
function wanted() {
  const r = stage.getBoundingClientRect(), d = Math.min(window.devicePixelRatio || 1, 2);
  return {w: Math.max(160, Math.round(r.width * d)), h: Math.max(120, Math.round(r.height * d))};
}
const locked = () => document.pointerLockElement === cv;
function configure() {
  const s = wanted(), video = decodable.length && serverVideo;
  send({t: "cfg", w: s.w, h: s.h, q: 80, auto: true, fps: video ? 60 : 30, codecs: video ? decodable : [],
        p: locked() ? "game" : "auto", audio: soundPlaying()});
}
const PROBES = [["av1", "av01.0.12M.08"], ["hevc", "hvc1.1.6.L150.B0"], ["vp9", "vp09.00.50.08"],
                ["h264", "avc1.640032"]];
async function detectCodecs() {
  if (typeof VideoDecoder !== "function" || typeof EncodedVideoChunk !== "function") return [];
  const out = [];
  for (const [name, codec] of PROBES) {
    try {
      if ((await VideoDecoder.isConfigSupported({codec, optimizeForLatency: true})).supported) out.push(name);
    } catch (e) { /* not this one */ }
  }
  return out;
}
function layout() {
  if (!W || !H) return;
  const r = stage.getBoundingClientRect(), k = Math.min(r.width / W, r.height / H);
  cv.style.width = Math.floor(W * k) + "px"; cv.style.height = Math.floor(H * k) + "px";
}
let resizeTimer = 0;
new ResizeObserver(() => { layout(); clearTimeout(resizeTimer); resizeTimer = setTimeout(configure, 250); }).observe(stage);

// ------------------------------------------------------------------ pictures
function askKey() {
  const n = performance.now();
  if (n - keyAsked > 500) { keyAsked = n; send({t: "ref"}); }
}
// Pacing (what live players and WebRTC do): pictures leave the host evenly but the network - Wi-Fi above all -
// delivers them in bursts. Each picture carries the host's time; the viewer measures how unevenly they arrive and
// holds each one just long enough to show them at the pace they were made, one per screen refresh. The buffer
// follows the network: none on a steady link, at most 50 ms on a jittery one, and none while the mouse is captured
// (games), when the newest picture always goes up at once.
const PLAYOUT_MAX = 50;
const arrivals = [];         // [local time, local time - host time] of the last 2 s
let base = 0, playout = 0, decodeAvg = 0;
const paced = [];            // decoded pictures waiting for their moment: {f, due}
let paceRaf = 0;
function arrived(hostMs) {
  const now = performance.now(), off = now - hostMs;
  arrivals.push([now, off]);
  while (now - arrivals[0][0] > 2000) arrivals.shift();
  let lo = Infinity;
  for (const a of arrivals) lo = Math.min(lo, a[1]);
  base = lo;                                           // the quickest trip (plus the clocks' difference)
  const delays = arrivals.map(a => a[1] - lo).sort((x, y) => x - y);
  const want = Math.min(delays[Math.floor(delays.length * 0.95)] || 0, locked() ? 0 : PLAYOUT_MAX);
  // up at once (a stall is worse than a little delay), down slowly (so one calm moment does not undo it)
  playout = want > playout ? want : playout * 0.98 + want * 0.02;
  const ms = String(Math.round(playout));
  if (stage.dataset.smoothing !== ms) stage.dataset.smoothing = ms;   // visible in the browser's developer tools
}
function draw(f) {
  if (cv.width !== f.displayWidth || cv.height !== f.displayHeight) {
    W = cv.width = f.displayWidth; H = cv.height = f.displayHeight; layout();
  }
  ctx.drawImage(f, 0, 0);
  f.close();
  if (!ended && !msgBox.hidden && !msgTimer) hideMsg();
}
function pace() {
  paceRaf = 0;
  const soon = performance.now() + 8;                  // shown at this refresh: due before about half a frame
  let pick = -1;
  for (let i = 0; i < paced.length; i++) if (paced[i].due <= soon) pick = i;
  if (pick >= 0) {
    for (let i = 0; i < pick; i++) paced[i].f.close();     // older ones were overtaken: skip them
    draw(paced[pick].f);
    paced.splice(0, pick + 1);
  }
  if (paced.length) paceRaf = requestAnimationFrame(pace);
}
function presented(f, host) {
  if (host === undefined || locked() || document.hidden || playout < 1) {
    while (paced.length) paced.shift().f.close();
    draw(f);
    return;
  }
  paced.push({f, due: host + base + playout + decodeAvg});
  while (paced.length > 3) paced.shift().f.close();      // decoders have few buffers: never hold many
  if (!paceRaf) paceRaf = requestAnimationFrame(pace);
}
function newDecoder(codec, w, h) {
  if (decoder && decoder.state !== "closed") { try { decoder.close(); } catch (e) { /* closing */ } }
  decCodec = codec; decW = w; decH = h;
  decoder = new VideoDecoder({
    output: f => {
      const seq = Math.round(f.timestamp / 1000), started = decodeStart.get(seq);
      decodeStart.delete(seq);
      if (started !== undefined) decodeAvg = decodeAvg * 0.9 + (performance.now() - started[0]) * 0.1;
      presented(f, started && started[1]);
      send({t: "ack", s: seq});                 // decoded: the network's part is done (the buffer is not its delay)
    },
    error: () => { needKey = true; askKey(); },
  });
  decoder.configure({codec, codedWidth: w, codedHeight: h, optimizeForLatency: true});
}
function videoChunk(buf) {
  const dv = new DataView(buf);
  const seq = dv.getUint32(1, true), w = dv.getUint16(5, true), h = dv.getUint16(7, true), key = dv.getUint8(9) & 1;
  const host = dv.getUint32(10, true);
  arrived(host);
  let off = 14;
  if (key) {
    const n = dv.getUint8(off);
    const codec = new TextDecoder().decode(new Uint8Array(buf, off + 1, n));
    off += 1 + n;
    if (!decoder || decoder.state === "closed" || codec !== decCodec || w !== decW || h !== decH) {
      try { newDecoder(codec, w, h); } catch (e) {          // this codec failed here: offer the others
        decodable = decodable.filter(c => !codec.startsWith({av1: "av01", hevc: "hvc1", vp9: "vp09", h264: "avc1"}[c]));
        configure(); return;
      }
    }
    needKey = false;
  } else if (needKey || !decoder || decoder.state !== "configured") {
    send({t: "ack", s: seq});              // cannot use it without a keyframe: ask for one, keep the flow going
    askKey();
    return;
  }
  try {
    decodeStart.set(seq, [performance.now(), host]);
    if (decodeStart.size > 200) decodeStart.delete(decodeStart.keys().next().value);
    decoder.decode(new EncodedVideoChunk({type: key ? "key" : "delta", timestamp: seq * 1000,
                                          data: new Uint8Array(buf, off)}));
  } catch (e) { needKey = true; send({t: "ack", s: seq}); askKey(); }
}

// ------------------------------------------------------------------ sound: Opus packets (10 ms), decoded by the
// browser (WebCodecs) and played with a small cushion that includes the picture's buffer (so they stay in step).
// Browsers allow sound only after a click or key press: the first one on the page starts it.
let actx = null, adec = null, playAt = 0;
const soundPlaying = () => !!(adec && adec.state === "configured");
function startSound() {
  if (!serverAudio || typeof AudioDecoder !== "function" || soundPlaying()) return;
  try {
    if (!actx) actx = new AudioContext({latencyHint: "interactive", sampleRate: 48000});
    if (actx.state === "suspended") actx.resume();
    adec = new AudioDecoder({output: playSound, error: () => { adec = null; }});
    adec.configure({codec: "opus", sampleRate: 48000, numberOfChannels: 2});
  } catch (e) { adec = null; }
  configure();
}
function playSound(data) {
  if (!actx) { data.close(); return; }
  const n = data.numberOfFrames, ch = data.numberOfChannels, buf = actx.createBuffer(ch, n, data.sampleRate);
  const plane = new Float32Array(n);
  for (let c = 0; c < ch; c++) { data.copyTo(plane, {planeIndex: c, format: "f32-planar"}); buf.copyToChannel(plane, c); }
  data.close();
  const now = actx.currentTime, cushion = 0.04 + playout / 1000;
  if (playAt < now + 0.01 || playAt > now + cushion + 0.2) playAt = now + cushion;
  const src = actx.createBufferSource();
  src.buffer = buf; src.connect(actx.destination); src.start(playAt);
  playAt += buf.duration;
}
function soundChunk(buf) {
  if (!soundPlaying()) return;
  const seq = new DataView(buf).getUint32(1, true);
  try { adec.decode(new EncodedAudioChunk({type: "key", timestamp: seq * 10000, data: new Uint8Array(buf, 5)})); }
  catch (e) { /* skip one packet */ }
}
window.addEventListener("pointerdown", startSound, true);
window.addEventListener("keydown", startSound, true);

function picture(buf) {
  const dv = new DataView(buf);
  if (dv.getUint8(0) === 3) { soundChunk(buf); return; }
  if (dv.getUint8(0) === 2) { videoChunk(buf); return; }
  if (dv.getUint8(0) !== 1) return;
  const seq = dv.getUint32(1, true), fw = dv.getUint16(5, true), fh = dv.getUint16(7, true), n = dv.getUint16(9, true);
  let off = 11;
  const parts = [];
  for (let i = 0; i < n; i++) {
    const x = dv.getUint16(off, true), y = dv.getUint16(off + 2, true), len = dv.getUint32(off + 8, true);
    off += 12;
    // decoding starts now, for every patch at once; drawing happens in order below
    parts.push({x, y, bm: createImageBitmap(new Blob([new Uint8Array(buf, off, len)], {type: "image/jpeg"}))});
    off += len;
  }
  chain = chain.then(async () => {
    if (fw !== W || fh !== H) { W = fw; H = fh; cv.width = W; cv.height = H; layout(); }
    for (const p of parts) {
      try { const b = await p.bm; ctx.drawImage(b, p.x, p.y); b.close(); } catch (e) { /* skip a bad patch */ }
    }
    send({t: "ack", s: seq});
    if (!ended && !msgBox.hidden && !msgTimer) hideMsg();
  });
}

function placeCursor(x, y) {
  cur.style.transform = `translate(${cv.offsetLeft + x * cv.clientWidth}px,${cv.offsetTop + y * cv.clientHeight}px)`;
}

function control(o) {
  switch (o.t) {
    case "hello":
      viewOnly = !!o.view_only; showCursor = !!o.cursor; hostRepeats = o.repeat !== false; retry = 0;
      serverVideo = !!o.video; needKey = true;
      serverAudio = !!o.audio && typeof AudioDecoder === "function";
      configure();
      break;
    case "info":
      if (!W) show("Please wait", o.msg, false); else note(o.msg);
      break;
    case "err":
      ended = true; show("The screen is not available", o.msg, true);
      break;
    case "bye":
      ended = true; show("Disconnected", o.msg, true);
      break;
    case "cur":
      if (showCursor && typeof o.x === "number" && !mouseInside) { cur.hidden = false; placeCursor(o.x, o.y); }
      else cur.hidden = true;
      break;
  }
}

// ------------------------------------------------------------------ connection
function connect() {
  ended = false;
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({k: TOKEN}));
  ws.onmessage = m => {
    if (typeof m.data === "string") { try { control(JSON.parse(m.data)); } catch (e) { /* ignore */ } }
    else picture(m.data);
  };
  ws.onclose = e => {
    held.clear(); buttons.clear();
    if (e.code === 4401) { location.replace("/login"); return; }
    if (ended) return;
    if (e.code === 4403) { ended = true; show("Refused", "This page is out of date. Reload it.", true); return; }
    if (W) note("Reconnecting…");
    setTimeout(connect, Math.min(5000, 400 * (++retry)));
  };
}
retryBtn.addEventListener("click", () => location.reload());

// ------------------------------------------------------------------ mouse
function pos(e) {
  const r = cv.getBoundingClientRect();
  return {x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
          y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))};
}
let lastMove = 0, moveTimer = 0;
function sendMove() {
  moveTimer = 0;
  if (pending) { lastMove = performance.now(); send({t: "m", x: +pending.x.toFixed(5), y: +pending.y.toFixed(5)}); pending = null; }
}
function moveTo(p) {                       // pointer moves go out at once (at most one per 8 ms): lowest latency
  pending = p;
  const wait = 8 - (performance.now() - lastMove);
  if (wait <= 0) sendMove();
  else if (!moveTimer) moveTimer = setTimeout(sendMove, wait);
}
function flush() {
  raf = 0;
  sendMove();
  if (Math.abs(wheel.dx) >= 1 || Math.abs(wheel.dy) >= 1) {
    const dx = Math.trunc(wheel.dx), dy = Math.trunc(wheel.dy);
    wheel.dx -= dx; wheel.dy -= dy;
    send({t: "w", dx, dy});
  }
}
const later = () => { if (!raf) raf = requestAnimationFrame(flush); };   // at most one move per screen refresh
const button = b => (b === 1 ? 1 : b === 2 ? 2 : 0);
const canControl = () => !viewOnly && ws && ws.readyState === 1;

// Mouse capture (Ctrl+Alt+M; Esc releases it): movement goes as relative motion for mouse-look, unaccelerated where
// the browser allows - the way game-streaming clients do it.
let rel = {dx: 0, dy: 0}, relTimer = 0, lastRel = 0;
function sendRel() {
  relTimer = 0;
  const dx = Math.round(rel.dx), dy = Math.round(rel.dy);
  if (!dx && !dy) return;
  rel.dx -= dx; rel.dy -= dy; lastRel = performance.now();
  send({t: "r", dx, dy});
}
function toggleCapture() {
  if (locked()) { document.exitPointerLock(); return; }
  if (!cv.requestPointerLock) return;
  try {
    const p = cv.requestPointerLock({unadjustedMovement: true});
    if (p && p.catch) p.catch(() => { try { cv.requestPointerLock(); } catch (e) { /* not allowed */ } });
  } catch (e) { try { cv.requestPointerLock(); } catch (x) { /* not allowed */ } }
}
document.addEventListener("pointerlockchange", () => { if (locked()) cv.focus(); configure(); });

cv.addEventListener("mousedown", e => {
  e.preventDefault(); cv.focus({preventScroll: true});
  if (!canControl()) return;
  const b = button(e.button);
  pending = null; buttons.add(b);
  if (locked()) { sendRel(); send({t: "b", b, d: true}); return; }
  const p = pos(e);
  send({t: "b", b, d: true, x: p.x, y: p.y});
});
window.addEventListener("mouseup", e => {
  const b = button(e.button);
  if (!buttons.has(b)) return;
  buttons.delete(b); flush();
  if (locked()) { sendRel(); send({t: "b", b, d: false}); return; }
  const p = pos(e);
  send({t: "b", b, d: false, x: p.x, y: p.y});
});
window.addEventListener("mousemove", e => {
  if (!canControl()) return;
  if (locked()) {                 // relative motion, out at once (coalesced to at most one message per 4 ms)
    rel.dx += e.movementX; rel.dy += e.movementY;
    const wait = 4 - (performance.now() - lastRel);
    if (wait <= 0) sendRel();
    else if (!relTimer) relTimer = setTimeout(sendRel, wait);
    return;
  }
  if (!mouseInside && !buttons.size) return;
  moveTo(pos(e));
});
cv.addEventListener("mouseenter", () => { mouseInside = true; cur.hidden = true; });
cv.addEventListener("mouseleave", () => { mouseInside = false; });
cv.addEventListener("wheel", e => {
  e.preventDefault();
  if (!canControl()) return;
  const k = (e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1) * 1.2;   // 120 units = one notch (~100 px)
  wheel.dx += e.deltaX * k; wheel.dy += e.deltaY * k; later();
}, {passive: false});
cv.addEventListener("contextmenu", e => e.preventDefault());
cv.addEventListener("dragstart", e => e.preventDefault());

// Touch screens work as a mouse: tap = click, hold = right-click, one-finger drag = drag, two fingers = scroll.
const touches = new Map();
let gesture = null;
const centroid = () => { let x = 0, y = 0; for (const t of touches.values()) { x += t.x; y += t.y; } return {x: x / touches.size, y: y / touches.size}; };
const click = (b, p) => { send({t: "b", b, d: true, x: p.x, y: p.y}); send({t: "b", b, d: false, x: p.x, y: p.y}); };
function cursorAt(p) { if (showCursor) { cur.hidden = false; placeCursor(p.x, p.y); } }

cv.addEventListener("pointerdown", e => {
  if (e.pointerType !== "touch") return;
  e.preventDefault();                                 // no emulated mouse events after this touch
  try { cv.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
  touches.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (!canControl()) return;
  if (touches.size === 1) {
    const p = pos(e);
    gesture = {mode: "tap", x0: e.clientX, y0: e.clientY, p,
      timer: setTimeout(() => { if (gesture && gesture.mode === "tap") { gesture.mode = "done"; click(2, p); } }, 550)};
    send({t: "m", x: p.x, y: p.y}); cursorAt(p);
  } else if (touches.size === 2) {
    if (gesture) { clearTimeout(gesture.timer); if (gesture.mode === "drag") send({t: "b", b: 0, d: false}); }
    const c = centroid();
    gesture = {mode: "scroll", cx: c.x, cy: c.y};
  }
});
cv.addEventListener("pointermove", e => {
  if (e.pointerType !== "touch" || !touches.has(e.pointerId)) return;
  e.preventDefault();
  touches.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (!gesture || !canControl()) return;
  if (gesture.mode === "scroll") {
    if (touches.size < 2) return;
    const c = centroid();
    wheel.dx += (gesture.cx - c.x) * 3; wheel.dy += (gesture.cy - c.y) * 3;
    gesture.cx = c.x; gesture.cy = c.y; later();
    return;
  }
  const p = pos(e);
  if (gesture.mode === "tap" && Math.hypot(e.clientX - gesture.x0, e.clientY - gesture.y0) > 10) {
    clearTimeout(gesture.timer); gesture.mode = "drag";
    send({t: "b", b: 0, d: true, x: gesture.p.x, y: gesture.p.y});
  }
  if (gesture.mode === "drag") { moveTo(p); cursorAt(p); }
});
function touchEnd(e) {
  if (e.pointerType !== "touch" || !touches.has(e.pointerId)) return;
  e.preventDefault();
  touches.delete(e.pointerId);
  if (gesture && canControl()) {
    if (gesture.mode === "tap") { clearTimeout(gesture.timer); click(0, gesture.p); gesture.mode = "done"; }
    else if (gesture.mode === "drag") { flush(); const p = pos(e); send({t: "b", b: 0, d: false, x: p.x, y: p.y}); gesture.mode = "done"; }
  }
  if (!touches.size) gesture = null;
}
cv.addEventListener("pointerup", touchEnd);
cv.addEventListener("pointercancel", touchEnd);

// ------------------------------------------------------------------ keyboard
const focused = () => { const a = document.activeElement; return locked() || a === cv || (a === document.body && !!document.fullscreenElement); };
function key(e, down) {
  if (!focused() || viewOnly) return;
  const c = e.code;
  if (!c || c === "Unidentified" || e.isComposing) return;
  e.preventDefault();
  if (down && c === "KeyM" && e.ctrlKey && e.altKey && !e.shiftKey && !e.metaKey) {   // Cloudora's one shortcut
    if (!e.repeat) toggleCapture();
    return;
  }
  if (down) {
    if (e.repeat && hostRepeats && held.has(c)) return;      // the computer repeats held keys itself
    held.add(c);
  } else {
    if (!held.has(c)) return;
    held.delete(c);
  }
  send({t: "k", c, d: down});
}
window.addEventListener("keydown", e => key(e, true), true);
window.addEventListener("keyup", e => key(e, false), true);
function releaseAll() {
  if (held.size || buttons.size) send({t: "rel"});
  held.clear(); buttons.clear();
}
window.addEventListener("blur", releaseAll);
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseAll(); });

show("Connecting…", "", false);
detectCodecs().then(list => { decodable = list; configure(); });
connect();
cv.focus();
})();
