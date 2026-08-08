/* Aurora Live — viewer app. Token-gated HLS playback via hls.js. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  token: localStorage.getItem("aurora_token") || null,
  channels: [],
  current: null,
  hls: null,
  statusTimer: null,
  statsTimer: null,
  retry: 0,
};

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/login") {
    showGate("Session expired — sign in again.");
    throw new Error("unauthorized");
  }
  return r;
}

async function apiJSON(path, opts = {}) {
  const r = await api(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ----------------------------------------------------------------- gate */
function showGate(msg) {
  stopPlayback();
  clearInterval(state.statusTimer);
  $("app").classList.add("hidden");
  $("gate").classList.remove("hidden");
  $("gate-error").textContent = msg || "";
}

$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("gate-code").value.trim();
  if (!code) return;
  $("gate-error").textContent = "";
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (r.status === 429) throw new Error("Too many attempts — wait a minute.");
    if (!r.ok) throw new Error("Invalid access code.");
    const data = await r.json();
    state.token = data.token;
    localStorage.setItem("aurora_token", data.token);
    await enterApp();
  } catch (err) {
    $("gate-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", () => {
  state.token = null;
  localStorage.removeItem("aurora_token");
  showGate();
});

/* ---------------------------------------------------------------- guide */
async function enterApp() {
  const data = await apiJSON("/api/channels");
  state.channels = data.channels;
  $("gate").classList.add("hidden");
  $("app").classList.remove("hidden");
  renderGuide();
  showGuide();
  startStatusPolling();
}

function renderGuide() {
  const root = $("categories");
  root.innerHTML = "";
  const byCat = {};
  for (const ch of state.channels) (byCat[ch.category] ||= []).push(ch);
  for (const [cat, list] of Object.entries(byCat)) {
    const h = document.createElement("div");
    h.className = "cat-name";
    h.textContent = cat;
    const cards = document.createElement("div");
    cards.className = "cards";
    for (const ch of list) {
      const c = document.createElement("div");
      c.className = "card";
      c.innerHTML = `
        <div class="card-head">
          <div class="mono" style="background:${ch.color}">${ch.name[0]}</div>
          <div>
            <div class="card-name">${ch.name}</div>
            <div class="card-meta">sources: ${ch.profiles.join(" + ")} · AES-128</div>
          </div>
        </div>
        <div class="card-desc">${ch.description}</div>`;
      c.addEventListener("click", () => openChannel(ch));
      cards.appendChild(c);
    }
    root.appendChild(h);
    root.appendChild(cards);
  }
}

function showGuide() {
  stopPlayback();
  $("player-view").classList.add("hidden");
  $("guide-view").classList.remove("hidden");
}

/* --------------------------------------------------------------- player */
function openChannel(ch) {
  state.current = ch;
  $("guide-view").classList.add("hidden");
  $("player-view").classList.remove("hidden");
  $("np-name").textContent = ch.name;
  $("st-origin").textContent = "…";
  connect(ch.url);
}

$("back-btn").addEventListener("click", showGuide);

function toast(msg, ms = 4000) {
  const t = $("player-toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function destroyHls() {
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  clearInterval(state.statsTimer);
}

function stopPlayback() {
  destroyHls();
  const v = $("video");
  v.pause();
  v.removeAttribute("src");
  state.current = null;
}

function connect(url) {
  destroyHls();
  state.retry = 0;
  const video = $("video");
  const masterUrl = url + "?token=" + encodeURIComponent(state.token);

  if (!Hls.isSupported()) {
    // native HLS (Safari)
    video.src = masterUrl;
    video.play().catch(() => {});
    return;
  }

  const hls = new Hls({
    liveSyncDurationCount: 3,
    liveMaxLatencyDurationCount: 8,
    maxBufferLength: 30,
    maxMaxBufferLength: 60,
    backBufferLength: 60,
    abrEwmaDefaultEstimate: 900000,
    fragLoadingTimeOut: 20000,
    manifestLoadingTimeOut: 15000,
    levelLoadingTimeOut: 15000,
    enableWorker: true,
  });
  state.hls = hls;

  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    video.play().catch(() => {});
    buildQualityMenu();
  });

  hls.on(Hls.Events.ERROR, (_e, data) => {
    if (!data.fatal) return;
    const code = data.response && data.response.code;
    if (code === 401 || code === 403) {
      showGate("Stream credentials rejected — sign in again.");
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
      hls.recoverMediaError();
      return;
    }
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      state.retry += 1;
      if (state.retry <= 6) {
        const wait = Math.min(1000 * 2 ** state.retry, 15000);
        toast(`Reconnecting (${state.retry})…`, wait);
        setTimeout(() => state.hls && state.hls.startLoad(), wait);
        return;
      }
      toast("Stream unreachable — rebuilding player.");
      setTimeout(() => state.current && connect(state.current.url), 2000);
      return;
    }
    toast("Playback error — rebuilding player.");
    setTimeout(() => state.current && connect(state.current.url), 2000);
  });

  hls.loadSource(masterUrl);
  hls.attachMedia(video);

  state.statsTimer = setInterval(updateStats, 1000);
}

function buildQualityMenu() {
  const sel = $("quality");
  sel.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "-1";
  auto.textContent = "Auto";
  sel.appendChild(auto);
  (state.hls.levels || []).forEach((lv, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = lv.height ? `${lv.height}p · ${Math.round(lv.bitrate / 1000)} kbps`
                               : `audio · ${Math.round(lv.bitrate / 1000)} kbps`;
    sel.appendChild(o);
  });
  sel.value = "-1";
}
$("quality").addEventListener("change", (e) => {
  if (state.hls) state.hls.currentLevel = parseInt(e.target.value, 10);
});

/* ---------------------------------------------------------------- stats */
function updateStats() {
  const hls = state.hls;
  const v = $("video");
  if (!hls || !state.current) return;
  const lv = hls.levels && hls.levels[hls.currentLevel];
  $("st-level").textContent = hls.currentLevel === -1 ? "auto" : hls.currentLevel;
  $("st-res").textContent = lv && lv.height ? `${lv.width}×${lv.height}` : "audio only";
  $("st-bw").textContent = hls.bandwidthEstimate
    ? `${Math.round(hls.bandwidthEstimate / 1000)} kbps` : "–";
  let ahead = 0;
  try {
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.currentTime >= v.buffered.start(i) && v.currentTime <= v.buffered.end(i)) {
        ahead = v.buffered.end(i) - v.currentTime;
        break;
      }
    }
  } catch (_) {}
  $("st-buf").textContent = `${ahead.toFixed(1)} s`;
  $("st-lat").textContent = hls.latency != null ? `${hls.latency.toFixed(1)} s` : "–";
}

/* ---------------------------------------------------- status / failover */
async function startStatusPolling() {
  clearInterval(state.statusTimer);
  const poll = async () => {
    try {
      const s = await apiJSON("/api/status");
      $("conn-dot").className = "dot dot-ok";
      $("conn-label").textContent = "connected";
      if (state.current && s.channels[state.current.id]) {
        const c = s.channels[state.current.id];
        $("st-origin").textContent =
          `${c.active_profile} · ${c.active_generation || "warming…"}` +
          (c.warming_generation ? `  (next: ${c.warming_generation})` : "");
        $("st-kid").textContent = c.current_kid || "–";
        $("st-rot").textContent = c.rotations;
        $("st-fo").textContent = c.failovers;
        const prof = c.profiles[c.active_profile];
        if (prof && !prof.healthy && !c.warming_generation) {
          $("conn-dot").className = "dot dot-bad";
          $("conn-label").textContent = "signal degraded";
        }
      }
    } catch (_) {
      $("conn-dot").className = "dot dot-bad";
      $("conn-label").textContent = "connection lost";
    }
  };
  await poll();
  state.statusTimer = setInterval(poll, 5000);
}

$("failover-btn").addEventListener("click", async () => {
  if (!state.current) return;
  try {
    // block whichever profile is currently active, so the demo always bites
    const s = await apiJSON("/api/status");
    const ch = s.channels[state.current.id];
    const profile = (ch && ch.active_profile) || "primary";
    await apiJSON("/api/simulate-failure", {
      method: "POST",
      body: JSON.stringify({ channel: state.current.id, profile, seconds: 40 }),
    });
    toast(`"${profile}" encoder killed — watch the pipeline fail over automatically.`, 6000);
  } catch (e) {
    toast("Could not trigger failover test: " + e.message);
  }
});

$("stats-btn").addEventListener("click", () => {
  $("stats-panel").classList.toggle("hidden");
});

/* ----------------------------------------------------------------- boot */
(async function boot() {
  if (!state.token) {
    showGate();
    return;
  }
  try {
    await enterApp();
  } catch (_) {
    showGate();
  }
})();
