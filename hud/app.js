/* Dashboard: the orb, the live caption, and the machine stats.
   Everything is driven by the server-sent events from /events. */

const canvas = document.getElementById("orb");
const ctx = canvas.getContext("2d");
const stateText = document.getElementById("state-text");
const heardEl = document.getElementById("heard");
const saidEl = document.getElementById("said");
const hintEl = document.getElementById("hint");

/* Themes, switched by voice on the server. The accent follows the state
   within a palette; a theme changes which family that is, and the surfaces
   around it through data-theme on the body. */
const PALETTES = {
  standard: {
    booting:   "#5d7d8f",
    idle:      "#4fe3ff",
    listening: "#46e6a0",
    thinking:  "#ffb347",
    speaking:  "#9d7bff",
    muted:     "#ff6b7a",
  },
  stealth: {
    booting:   "#6f6a4f",
    idle:      "#d6b760",
    listening: "#9ab875",
    thinking:  "#e0c25c",
    speaking:  "#b3a06a",
    muted:     "#c96b5e",
  },
  combat: {
    booting:   "#8f5d5d",
    idle:      "#ff5346",
    listening: "#ff8c42",
    thinking:  "#ffb84d",
    speaking:  "#ff6f61",
    muted:     "#ff2d2d",
  },
};
let COLORS = PALETTES.standard;
const LABELS = {
  booting:   "starting",
  idle:      "listening",
  listening: "hearing you",
  thinking:  "thinking",
  speaking:  "speaking",
  muted:     "microphone off",
};

let state = "booting";
let mic = 0, out = 0;        // raw levels from the server
let shown = 0;               // smoothed level actually drawn
let ripples = [];
let wave = new Array(72).fill(0);   // recent levels, drawn around the orb
let cpu = 0, mem = 0;               // telemetry arcs read these

function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* ------------------------------------------------------------------ events */
const source = new EventSource("/events?t=" + encodeURIComponent(window.JARVIS_TOKEN));

source.onmessage = (event) => {
  let msg;
  try { msg = JSON.parse(event.data); } catch { return; }

  if (msg.type === "state") {
    const changed = msg.value !== state;
    // the label and colour update even when the value is unchanged: after a
    // reconnect the replayed state used to be swallowed by an early return and
    // the header stayed on "reconnecting" while everything actually worked
    state = msg.value;
    if (changed && msg.value === "listening") {
      ripples.push(performance.now());
      clearExchange();                      // a new turn: wipe the last one
    }
    const colour = COLORS[state] || COLORS.idle;
    document.documentElement.style.setProperty("--accent", colour);
    stateText.textContent = LABELS[state] || state;
    const dot = document.querySelector(".dot");
    dot.style.background = colour;
    dot.style.boxShadow = `0 0 12px ${colour}`;
    hintEl.classList.toggle("hidden", state !== "idle" && !heardEl.textContent);
    showMicState();
    radarBlip(state === "thinking" ? "sys" : "mic");
  } else if (msg.type === "level") {
    mic = msg.mic || 0;
    out = msg.out || 0;
    wave.push(Math.min(1, state === "speaking" ? out : mic));
    wave.shift();
  } else if (msg.type === "theme") {
    const t = PALETTES[msg.value] ? msg.value : "standard";
    document.body.dataset.theme = t;
    COLORS = PALETTES[t];
    const colour = COLORS[state] || COLORS.idle;
    document.documentElement.style.setProperty("--accent", colour);
    // one glitch at the moment it lands, like a display being re-tuned
    document.body.classList.remove("theme-switch");
    void document.body.offsetWidth;
    document.body.classList.add("theme-switch");
    radarBlip("sys");
  } else if (msg.type === "heard") {
    setLine(heardEl, msg.text);
    heardEl.classList.toggle("partial", !!msg.partial);
    hintEl.classList.add("hidden");
  } else if (msg.type === "turn") {
    document.getElementById("delay").textContent = msg.latency.toFixed(1) + "s";
  } else if (msg.type === "stats") {
    cpu = msg.cpu || 0;
    mem = msg.mem || 0;
    setStat("cpu", msg.cpu, "%");
    setStat("mem", msg.mem, "%");
    document.getElementById("temp").textContent = msg.temp != null ? msg.temp + " C" : "--";
  } else if (msg.type === "log") {
    addLog(msg.who, msg.text);
    if (msg.who === "you") { setLine(heardEl, msg.text); heardEl.classList.remove("partial"); }
    if (msg.who === "jarvis") setLine(saidEl, msg.text);
    radarBlip(msg.who === "doing" ? "warn" : msg.who === "jarvis" ? "out" : "mic");
  }
};

source.onerror = () => { stateText.textContent = "reconnecting"; };

function setLine(el, text) {
  el.textContent = text;
  el.classList.add("show");
}

function clearExchange() {
  for (const el of [heardEl, saidEl]) {
    el.textContent = "";
    el.classList.remove("show", "partial");
  }
  hintEl.classList.remove("hidden");
}

function setStat(key, value, unit) {
  document.getElementById(key).textContent = value + unit;
  document.getElementById(key + "-bar").style.width = Math.min(100, value) + "%";
}

function addLog(who, text) {
  const box = document.getElementById("log");
  const row = document.createElement("div");
  row.className = who;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "doing" ? "command" : who;
  row.appendChild(label);
  row.appendChild(document.createTextNode(text));
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
  while (box.childElementCount > 120) box.removeChild(box.firstChild);
}

/* --------------------------------------------------------------- mic toggle */
const micBtn = document.getElementById("mic");
const micText = document.getElementById("mic-text");

/* The state is the truth, not a local flag: the server knows whether the
   microphone is actually open, and it is the same switch whoever pressed it. */
function showMicState() {
  const off = state === "muted";
  micBtn.classList.toggle("off", off);
  micText.textContent = off ? "mic off" : "mic on";
  hintEl.textContent = off ? "microphone is off, press M or click mic" : 'say \u201chey maki\u201d';
  hintEl.classList.toggle("hidden", !off && state !== "idle" && !heardEl.textContent);
}

function toggleMic() {
  const on = state === "muted";        // muted now means we are turning it on
  micBtn.disabled = true;              // one press, one request
  fetch("/mic", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jarvis-Token": window.JARVIS_TOKEN },
    body: JSON.stringify({ on }),
  }).catch(() => {}).finally(() => { micBtn.disabled = false; });
}

micBtn.addEventListener("click", toggleMic);
window.addEventListener("keydown", (e) => {
  // never steal the key while he is typing a request
  const el = e.target;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
  if (e.key === "m" || e.key === "M") toggleMic();
});

/* --------------------------------------------------------------- typed in */
const input = document.getElementById("say");
input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !input.value.trim()) return;
  const text = input.value.trim();
  input.value = "";
  // No local log row here: the server logs "you" when the turn starts, and
  // drawing our own made every typed message appear twice. The caption below
  // is the instant feedback; the activity line follows from the server.
  clearExchange();
  setLine(heardEl, text);
  // the token is minted per run and only ever handed to the page the server
  // itself served, so a request from any other page cannot get in
  fetch("/say", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Jarvis-Token": window.JARVIS_TOKEN },
    body: JSON.stringify({ text }),
  }).catch(() => {});
});

/* ------------------------------------------------------------- boot film */
/* Typed kernel log, then the panels take over. Skipped with #noboot in the
   address, which is how screenshots of the steady state are taken. */
const BOOT_LINES = [
  "initialising jarvis kernel 4.2 .............. <ok>OK</ok>",
  "audio subsystem, pipewire .................... <ok>OK</ok>",
  "speech recogniser, local ..................... <ok>OK</ok>",
  "voice synthesiser, local ..................... <ok>OK</ok>",
  "model link ................................... <ok>OK</ok>",
  "security watch ................................ <ok>OK</ok>",
  "all systems nominal. good day.",
];

async function bootSequence() {
  const boot = document.getElementById("boot");
  if (!boot || location.hash === "#noboot") { boot && boot.remove(); return; }
  const out = document.getElementById("boot-lines");
  let delay = 260;
  for (const line of BOOT_LINES) {
    await new Promise(r => setTimeout(r, delay));
    delay = 300;
    const row = document.createElement("div");
    out.appendChild(row);
    row.innerHTML = line.replace(/<ok>/g, '<span class="ok">').replace(/<\/ok>/g, "</span>");
    row.style.opacity = "0";
    row.style.transition = "opacity 160ms ease";
    requestAnimationFrame(() => { row.style.opacity = "1"; });
  }
  await new Promise(r => setTimeout(r, 700));
  boot.classList.add("done");
  setTimeout(() => boot.remove(), 1000);
}
bootSequence();

/* ------------------------------------------------------------------ radar */
const radar = document.getElementById("radar");
const rctx = radar ? radar.getContext("2d") : null;
let sweepA = -Math.PI / 2;
let sweepLast = 0;
let blips = [];                            // { a, r, t, colour }
const BLIP_LIFE = 6000;

function radarBlip(kind) {
  if (!rctx) return;
  const colour = kind === "warn" ? "#ffb347"
    : kind === "out" ? COLORS.speaking
    : kind === "sys" ? "#8fa4b0" : COLORS.listening;
  blips.push({
    a: Math.random() * Math.PI * 2,
    r: 0.3 + Math.random() * 0.62,
    t: performance.now(),
    colour,
  });
  if (blips.length > 14) blips = blips.slice(-14);
}

function radarDraw(now) {
  if (!rctx) return;
  const w = radar.width, h = radar.height;
  const cx = w / 2, cy = h / 2 + 4;
  const R = Math.min(w, h) * 0.44;
  const dt = Math.min(100, now - sweepLast || 16);
  sweepLast = now;
  sweepA += dt * 0.0016;                   // one revolution in about four seconds
  rctx.clearRect(0, 0, w, h);

  rctx.strokeStyle = "rgba(120, 200, 230, 0.16)";
  rctx.lineWidth = 1;
  for (let k = 1; k <= 3; k++) {
    rctx.beginPath();
    rctx.arc(cx, cy, R * k / 3, 0, Math.PI * 2);
    rctx.stroke();
  }
  rctx.beginPath();
  rctx.moveTo(cx - R, cy); rctx.lineTo(cx + R, cy);
  rctx.moveTo(cx, cy - R); rctx.lineTo(cx, cy + R);
  rctx.globalAlpha = 0.35;
  rctx.stroke();
  rctx.globalAlpha = 1;

  // the sweep: a wedge that fades behind the line, like a real scope
  for (let k = 0; k < 22; k++) {
    const a = sweepA - k * 0.035;
    rctx.beginPath();
    rctx.moveTo(cx, cy);
    rctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    const c = COLORS.idle;
    rctx.strokeStyle = c;
    rctx.globalAlpha = 0.28 * (1 - k / 22);
    rctx.stroke();
  }
  rctx.globalAlpha = 1;

  blips = blips.filter(b => now - b.t < BLIP_LIFE);
  for (const b of blips) {
    const age = (now - b.t) / BLIP_LIFE;
    const x = cx + Math.cos(b.a) * R * b.r;
    const y = cy + Math.sin(b.a) * R * b.r;
    rctx.beginPath();
    rctx.arc(x, y, 3 + age * 2, 0, Math.PI * 2);
    rctx.fillStyle = b.colour;
    rctx.globalAlpha = (1 - age) * 0.9;
    rctx.fill();
    rctx.globalAlpha = (1 - age) * 0.35;
    rctx.beginPath();
    rctx.arc(x, y, 5 + age * 9, 0, Math.PI * 2);
    rctx.strokeStyle = b.colour;
    rctx.stroke();
  }
  rctx.globalAlpha = 1;
  requestAnimationFrame(radarDraw);
}
if (rctx) requestAnimationFrame(radarDraw);

/* ------------------------------------------------------------------ visual */
function fit() {
  const dpr = window.devicePixelRatio || 1;
  const box = canvas.parentElement.getBoundingClientRect();
  canvas.width = Math.max(1, box.width * dpr);
  canvas.height = Math.max(1, box.height * dpr);
  canvas.style.width = box.width + "px";
  canvas.style.height = box.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", fit);
fit();

function draw(now) {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) * 0.175;
  const colour = COLORS[state] || COLORS.idle;
  const live = state === "listening" ? mic : state === "speaking" ? out : 0;
  const breath = state === "idle" ? 0.05 * (1 + Math.sin(now / 1000)) : 0;
  shown += (Math.min(live, 1) + breath - shown) * 0.16;

  ctx.clearRect(0, 0, w, h);

  /* faint concentric guides */
  ctx.strokeStyle = colour;
  ctx.globalAlpha = 0.05;
  ctx.lineWidth = 1;
  for (let k = 1; k <= 3; k++) {
    ctx.beginPath();
    ctx.arc(cx, cy, R * (1.3 + k * 0.22), 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  /* ticks, longer as it hears more */
  const ticks = 84;
  for (let i = 0; i < ticks; i++) {
    const a = (i / ticks) * Math.PI * 2 - Math.PI / 2;
    const big = i % 7 === 0;
    const len = (big ? 9 : 5) + shown * 13 * (0.4 + 0.6 * Math.abs(Math.sin(now / 700 + i)));
    const r0 = R * 1.36, r1 = r0 + len;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
    ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = (big ? 0.5 : 0.22) + shown * 0.3;
    ctx.lineWidth = big ? 1.7 : 1;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  /* rotating arcs, two directions */
  const speed = state === "thinking" ? 900 : 2600;
  for (let k = 0; k < 3; k++) {
    const rr = R * (1.2 + k * 0.1);
    const spin = (now / (speed + k * 700)) * (k % 2 ? -1 : 1);
    ctx.beginPath();
    ctx.arc(cx, cy, rr, spin, spin + Math.PI * (0.42 + 0.24 * k));
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.34 - k * 0.08;
    ctx.lineWidth = 1.3;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  /* waveform ring: the last second and a half of sound, wrapped around the
     orb as radial spokes, the way a suit's HUD would show a voice */
  const spokes = wave.length;
  const spin = now / 24000;
  for (let i = 0; i < spokes; i++) {
    const v = wave[(i + Math.floor(now / 50)) % spokes];
    if (v < 0.015) continue;
    const a = (i / spokes) * Math.PI * 2 - Math.PI / 2 + spin;
    const r0 = R * 1.62;
    const r1 = r0 + 4 + v * R * 0.5;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
    ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.12 + v * 0.5;
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  /* telemetry arcs: cpu and memory as partial rings, the diagnostics readout
     of a ship rather than numbers in a box */
  const arcs = [[cpu, R * 2.42, 0.30], [mem, R * 2.66, 0.18]];
  for (const [value, rr, alpha] of arcs) {
    const span = Math.PI * 1.5;
    const start = -Math.PI / 2 - span / 2;
    ctx.beginPath();
    ctx.arc(cx, cy, rr, start, start + span);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = 0.10;
    ctx.lineWidth = 2.4;
    ctx.stroke();
    const end = start + span * Math.min(1, value / 100);
    ctx.beginPath();
    ctx.arc(cx, cy, rr, start, end);
    ctx.strokeStyle = value > 85 ? COLORS.muted : colour;
    ctx.globalAlpha = alpha + 0.35;
    ctx.stroke();
    if (value > 1) {
      ctx.beginPath();
      ctx.arc(cx + Math.cos(end) * rr, cy + Math.sin(end) * rr, 2.4, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.globalAlpha = 0.8;
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;

  /* the live ring: an organic closed curve, louder when it hears more */
  const pts = 120;
  ctx.beginPath();
  for (let i = 0; i <= pts; i++) {
    const a = (i / pts) * Math.PI * 2 - Math.PI / 2;
    const wob = Math.sin(a * 6 + now / 230) * 0.5
              + Math.sin(a * 11 - now / 360) * 0.3
              + Math.sin(a * 3 + now / 170) * 0.2;
    const r = R * 1.5 + wob * (3 + shown * 24);
    const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.closePath();
  ctx.strokeStyle = colour;
  ctx.globalAlpha = 0.35 + shown * 0.5;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.globalAlpha = 1;

  /* ripples, one per wake */
  ripples = ripples.filter((t) => now - t < 1500);
  for (const t of ripples) {
    const p = (now - t) / 1500;
    ctx.beginPath();
    ctx.arc(cx, cy, R * (1.4 + p * 2.2), 0, Math.PI * 2);
    ctx.strokeStyle = colour;
    ctx.globalAlpha = (1 - p) * 0.4;
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  /* the core: a dark sphere with a lit rim, so it reads as an object rather
     than a smudge of glow */
  const glow = R * (1.5 + shown * 0.38);
  const halo = ctx.createRadialGradient(cx, cy, R * 0.8, cx, cy, glow);
  halo.addColorStop(0, rgba(colour, 0.20 + shown * 0.26));
  halo.addColorStop(1, rgba(colour, 0));
  ctx.beginPath();
  ctx.arc(cx, cy, glow, 0, Math.PI * 2);
  ctx.fillStyle = halo;
  ctx.fill();

  /* concentric, so the sphere reads clean: dark glass with a bright rim and a
     single highlight, rather than two competing blobs of light */
  const sphere = ctx.createRadialGradient(cx, cy, R * 0.05, cx, cy, R);
  sphere.addColorStop(0.00, rgba(colour, 0.10));
  sphere.addColorStop(0.42, rgba(colour, 0.09));
  sphere.addColorStop(0.72, rgba(colour, 0.30));
  sphere.addColorStop(0.90, rgba(colour, 0.62));
  sphere.addColorStop(0.97, rgba(colour, 0.98));
  sphere.addColorStop(1.00, rgba(colour, 0.05));
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = sphere;
  ctx.fill();

  ctx.beginPath();
  ctx.arc(cx, cy, R - 0.8, 0, Math.PI * 2);
  ctx.strokeStyle = rgba(colour, 0.9);
  ctx.lineWidth = 1.1;
  ctx.stroke();

  /* faint inner structure: without something inside, the sphere is a flat disc */
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1;
  for (const [frac, alpha] of [[0.58, 0.10], [0.30, 0.07]]) {
    ctx.beginPath();
    ctx.arc(cx, cy, R * frac, 0, Math.PI * 2);
    ctx.globalAlpha = alpha + shown * 0.12;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  const spec = ctx.createRadialGradient(cx - R * 0.28, cy - R * 0.42, 0,
                                        cx - R * 0.28, cy - R * 0.42, R * 0.52);
  spec.addColorStop(0, "rgba(255,255,255,0.34)");
  spec.addColorStop(0.45, "rgba(255,255,255,0.10)");
  spec.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath();
  ctx.arc(cx - R * 0.28, cy - R * 0.42, R * 0.52, 0, Math.PI * 2);
  ctx.fillStyle = spec;
  ctx.fill();

  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
