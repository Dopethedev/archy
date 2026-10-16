/* Dashboard: pictures for the four states, driven by server-sent events. */

const canvas = document.getElementById("orb");
const ctx = canvas.getContext("2d");
const stateText = document.getElementById("state-text");
const stateDot = document.querySelector(".state .dot");
const hint = document.getElementById("hint");
const logBox = document.getElementById("log");

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
let mic = 0, out = 0;      // raw levels from the server
let shown = 0;             // smoothed level actually drawn
let ripples = [];
let pulseAt = 0;

/* ------------------------------------------------------------------ events */
// the token rides in the URL because EventSource cannot send headers
const source = new EventSource("/events?t=" + encodeURIComponent(window.JARVIS_TOKEN));

source.onmessage = (event) => {
  let msg;
  try { msg = JSON.parse(event.data); } catch { return; }

  if (msg.type === "state") {
    if (msg.value !== state) {
      if (msg.value === "listening") { ripples.push(performance.now()); pulseAt = performance.now(); }
      state = msg.value;
      stateText.textContent = LABELS[state] || state;
      stateDot.style.background = COLORS[state] || COLORS.idle;
      stateDot.style.color = COLORS[state] || COLORS.idle;
      hint.style.opacity = state === "idle" ? "1" : "0";
    }
  } else if (msg.type === "level") {
    mic = msg.mic || 0;
    out = msg.out || 0;
  } else if (msg.type === "stats") {
    setStat("cpu", msg.cpu, "%");
    setStat("mem", msg.mem, "%");
    document.getElementById("temp").textContent = msg.temp != null ? msg.temp + " C" : "--";
  } else if (msg.type === "log") {
    addLog(msg.who, msg.text);
  }
};

source.onerror = () => { stateText.textContent = "reconnecting"; };

function setStat(key, value, unit) {
  document.getElementById(key).textContent = value + unit;
  document.getElementById(key + "-bar").style.width = Math.min(100, value) + "%";
}

function addLog(who, text) {
  const row = document.createElement("div");
  row.className = who;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "you" ? "you" : who === "jarvis" ? "jarvis" : who;
  row.appendChild(label);
  row.appendChild(document.createTextNode(text));
  logBox.appendChild(row);
  logBox.scrollTop = logBox.scrollHeight;
  while (logBox.childElementCount > 120) logBox.removeChild(logBox.firstChild);
}

/* --------------------------------------------------------------- typed in */
const input = document.getElementById("say");
input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !input.value.trim()) return;
  const text = input.value.trim();
  input.value = "";
  addLog("you", text);
  // the token is minted per run and only ever handed to the page that the
  // server itself served, so a request from any other page cannot get in
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
  canvas.width = box.width * dpr;
  canvas.height = box.height * dpr;
  canvas.style.width = box.width + "px";
  canvas.style.height = box.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", fit);
fit();

function draw(now) {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) * 0.21;
  const color = COLORS[state] || COLORS.idle;

  // target level depends on what the assistant is doing
  const target = state === "listening" ? mic : state === "speaking" ? out : 0;
  const idleBreath = state === "idle" ? 0.045 * (1 + Math.sin(now / 900)) : 0;
  shown += (Math.min(target, 1) + idleBreath - shown) * 0.18;

  ctx.clearRect(0, 0, w, h);

  // ripples, one per wake
  ripples = ripples.filter((t) => now - t < 1400);
  for (const t of ripples) {
    const p = (now - t) / 1400;
    ctx.beginPath();
    ctx.arc(cx, cy, R * (1 + p * 2.1), 0, Math.PI * 2);
    ctx.strokeStyle = color;
    ctx.globalAlpha = (1 - p) * 0.35;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // radial bars
  const bars = 72;
  for (let i = 0; i < bars; i++) {
    const a = (i / bars) * Math.PI * 2 - Math.PI / 2;
    const wobble = 0.5 + 0.5 * Math.sin(now / 260 + i * 0.7);
    const active = Math.max(shown, 0.02);
    const len = 6 + active * 62 * (0.45 + 0.55 * wobble);
    const r0 = R * 1.32, r1 = r0 + len;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
    ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.15 + 0.65 * active + 0.15 * wobble;
    ctx.lineWidth = 2.1;
    ctx.lineCap = "round";
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // rotating rings
  for (let k = 0; k < 3; k++) {
    const rr = R * (1.13 + k * 0.11);
    const spin = now / (2400 + k * 1500) * (k % 2 ? -1 : 1);
    ctx.beginPath();
    ctx.arc(cx, cy, rr, spin, spin + Math.PI * (0.5 + 0.28 * k));
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.3 - k * 0.07;
    ctx.lineWidth = 1.2;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // the core
  const glow = R * (1 + shown * 0.30);
  const grad = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, glow);
  grad.addColorStop(0, "rgba(255,255,255,0.92)");
  grad.addColorStop(0.34, color);
  grad.addColorStop(1, "rgba(3,6,11,0)");
  ctx.beginPath();
  ctx.arc(cx, cy, glow, 0, Math.PI * 2);
  ctx.fillStyle = grad;
  ctx.globalAlpha = 0.85;
  ctx.shadowBlur = 45 + shown * 60;
  ctx.shadowColor = color;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 1;

  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
