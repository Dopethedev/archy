/* Dashboard: the orb, the live caption, and the machine stats.
   Everything is driven by the server-sent events from /events. */

const canvas = document.getElementById("orb");
const ctx = canvas.getContext("2d");
const stateText = document.getElementById("state-text");
const heardEl = document.getElementById("heard");
const saidEl = document.getElementById("said");
const hintEl = document.getElementById("hint");

const COLORS = {
  booting:   "#5d7d8f",
  idle:      "#4fe3ff",
  listening: "#46e6a0",
  thinking:  "#ffb347",
  speaking:  "#9d7bff",
};
const LABELS = {
  booting:   "starting",
  idle:      "listening",
  listening: "hearing you",
  thinking:  "thinking",
  speaking:  "speaking",
};

let state = "booting";
let mic = 0, out = 0;        // raw levels from the server
let shown = 0;               // smoothed level actually drawn
let ripples = [];

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
    if (msg.value === state) return;
    if (msg.value === "listening") {
      ripples.push(performance.now());
      clearExchange();                      // a new turn: wipe the last one
    }
    state = msg.value;
    const colour = COLORS[state] || COLORS.idle;
    document.documentElement.style.setProperty("--accent", colour);
    stateText.textContent = LABELS[state] || state;
    const dot = document.querySelector(".dot");
    dot.style.background = colour;
    dot.style.boxShadow = `0 0 12px ${colour}`;
    hintEl.classList.toggle("hidden", state !== "idle" && !heardEl.textContent);
  } else if (msg.type === "level") {
    mic = msg.mic || 0;
    out = msg.out || 0;
  } else if (msg.type === "heard") {
    setLine(heardEl, msg.text);
    heardEl.classList.toggle("partial", !!msg.partial);
    hintEl.classList.add("hidden");
  } else if (msg.type === "turn") {
    document.getElementById("delay").textContent = msg.latency.toFixed(1) + "s";
  } else if (msg.type === "stats") {
    setStat("cpu", msg.cpu, "%");
    setStat("mem", msg.mem, "%");
    document.getElementById("temp").textContent = msg.temp != null ? msg.temp + " C" : "--";
  } else if (msg.type === "log") {
    addLog(msg.who, msg.text);
    if (msg.who === "you") { setLine(heardEl, msg.text); heardEl.classList.remove("partial"); }
    if (msg.who === "jarvis") setLine(saidEl, msg.text);
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

/* --------------------------------------------------------------- typed in */
const input = document.getElementById("say");
input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !input.value.trim()) return;
  const text = input.value.trim();
  input.value = "";
  addLog("you", text);
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
