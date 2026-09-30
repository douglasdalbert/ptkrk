import { ConfirmDialog } from "/cantor/assets/confirm-dialog.js";

const show = document.querySelector("#show");
const video = document.querySelector("#video");
const overlay = document.querySelector("#video-overlay");
const playButton = document.querySelector("#play");
const error = document.querySelector("#error");
const connectionStatus = document.querySelector("#connect-status");
const lyricTrack = document.querySelector("#track");
const vocalMarker = document.querySelector("#vocal-marker");
const lyricText = document.querySelector("#lyric-text");
const liveScore = document.querySelector("#live-score");
let socket;
let retryTimer;
let activeId = null;
let playbackSyncTimer = null;
let finishing = false;
let skip = null;
let barPosition = 1;
let colorIndex = 0;
let party = null;
let captionBars = [];
let hitBlocks = new Set();
let captionFrame = null;
const visibleBlocks = new Map();
const approachMs = 3000;
const colors = ["#bda145", "#ff70ac", "#6bded0", "#c9fa45", "#f5f5ee"];
const confirmDialog = new ConfirmDialog();

function setConnectionStatus(state, label) {
  connectionStatus.dataset.state = state;
  connectionStatus.title = label;
  connectionStatus.querySelector(".sr-only").textContent = label;
}

async function command(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", ...options,
    headers: { ...(options.headers || {}), ...(options.method === "POST" ? {"X-Karaoke-TV": "local"} : {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Não foi possível atualizar a TV.");
  }
  return response.status === 204 ? null : response.json();
}

function stopPlayback() {
  activeId = null;
  cancelAnimationFrame(captionFrame);
  captionFrame = null;
  clearInterval(playbackSyncTimer);
  playbackSyncTimer = null;
  video.pause();
  video.removeAttribute("src");
  video.load();
  overlay.hidden = true;
  lyricTrack.hidden = true;
  lyricText.replaceChildren();
  liveScore.hidden = true;
  liveScore.textContent = "";
  captionBars = [];
  hitBlocks = new Set();
  visibleBlocks.clear();
  playButton.hidden = true;
  document.querySelector("#intermission").hidden = false;
}

async function startPlayback(item) {
  activeId = item.id;
  captionBars = [];
  hitBlocks = new Set();
  visibleBlocks.clear();
  lyricTrack.hidden = false;
  lyricText.replaceChildren();
  liveScore.hidden = true;
  liveScore.textContent = "";
  video.src = `/api/tv/${encodeURIComponent(item.id)}/video`;
  loadCaptionBars(item.id);
  clearInterval(playbackSyncTimer);
  playbackSyncTimer = setInterval(() => {
    if (socket?.readyState !== WebSocket.OPEN || !activeId) return;
    socket.send(JSON.stringify({
      type: "playback_sync",
      request_id: activeId,
      position_ms: Math.round(video.currentTime * 1000),
      playing: !video.paused && !video.ended,
    }));
  }, 50);
  document.querySelector("#intermission").hidden = true;
  overlay.hidden = false;
  try {
    await video.play();
    playButton.hidden = true;
  } catch {
    playButton.hidden = false;
  }
}

async function loadCaptionBars(requestId) {
  try {
    const response = await fetch(`/api/tv/${encodeURIComponent(requestId)}/captions`, {
      credentials: "same-origin",
    });
    if (!response.ok) return;
    const result = await response.json();
    if (activeId !== requestId) return;
    captionBars = result.bars || [];
    renderCaptionBar();
  } catch {
    captionBars = [];
  }
}

function renderCaptionBar() {
  const time = video.currentTime * 1000;
  const width = overlay.clientWidth;
  const speed = width / (2 * approachMs);
  const visible = new Set();
  captionBars.forEach((bar, index) => {
    const left = width / 2 + (bar.start_ms - time) * speed;
    if (left > width || left < -width / 2) return;
    visible.add(index);
    let block = visibleBlocks.get(index);
    if (!block) {
      block = document.createElement("span");
      block.className = "lyric-block";
      block.textContent = bar.text;
      block.style.setProperty("--lane", index % 3);
      block.style.setProperty("--block-color", colors[colorIndex]);
      lyricText.append(block);
      visibleBlocks.set(index, block);
    }
    block.dataset.result = hitBlocks.has(index) ? "hit" :
      time > (bar.score_window_end_ms ?? bar.end_ms) ? "miss" : "waiting";
    block.style.transform = `translate3d(${left}px, 0, 0)`;
  });
  for (const [index, block] of visibleBlocks) {
    if (!visible.has(index)) {
      block.remove();
      visibleBlocks.delete(index);
    }
  }
  lyricTrack.hidden = !visible.size && !activeId;
}

function animateCaptionBars() {
  renderCaptionBar();
  captionFrame = video.paused || video.ended ? null : requestAnimationFrame(animateCaptionBars);
}

function animateVocalMarker() {
  vocalMarker.getAnimations().forEach(animation => animation.cancel());
  vocalMarker.animate([
    { transform: "translateX(-50%) scaleX(1)", backgroundColor: "#ffffff6b", boxShadow: "0 0 8px #000" },
    { transform: "translateX(-50%) scaleX(3)", backgroundColor: "#64d4c3", boxShadow: "0 0 18px #64d4c3" },
    { transform: "translateX(-50%) scaleX(1)", backgroundColor: "#ffffff6b", boxShadow: "0 0 8px #000" },
  ], { duration: 420, easing: "ease-out" });
}

function render(snapshot) {
  if (party && party !== snapshot.party) stopPlayback();
  if (party !== snapshot.party) {
    party = snapshot.party;
    refreshJoin();
  }
  const items = snapshot.items.filter(item => item.position);
  const current = snapshot.invitation && items.find(item => item.id === snapshot.invitation.request_id);
  const next = current || items.slice(0, 4).find(item => item.status === "ready");
  const others = items.filter(item => activeId ? item.id !== activeId : true).slice(0, 4);
  const skipped = snapshot.skip;
  skip = skipped;
  document.querySelector("#skip-notice").hidden = !skipped;
  if (skipped) updateSkipClock();

  if (current && snapshot.invitation.accepted && !skipped) {
    if (activeId !== current.id) startPlayback(current);
  } else if (activeId && (!current || current.id !== activeId || !snapshot.invitation.accepted)) {
    stopPlayback();
  }

  const status = document.querySelector("#next-status");
  status.textContent = current ? (current.status === "ready" ? "PRÓXIMA MÚSICA" : "PREPARANDO") :
    next ? "AGUARDANDO CANTOR" : items.length ? "PREPARANDO MÚSICAS" : "AGUARDANDO MÚSICAS";
  document.querySelector("#next-title").textContent = next?.title || (next ? next.video_id : "A festa começa aqui.");
  document.querySelector("#next-singer").textContent = next ?
    [next.singer_name, ...next.backvocals.map(vocal => vocal.singer_name)].join(" + ") : "";
  const preview = document.querySelector("#next-preview");
  preview.hidden = !next || next.status !== "ready";
  preview.onerror = () => { preview.hidden = true; };
  if (!preview.hidden) preview.src = `/api/tv/${encodeURIComponent(next.id)}/preview`;

  const upcoming = document.querySelector("#upcoming");
  upcoming.replaceChildren(...others.map(item => {
    const article = document.createElement("article");
    if (item.status === "ready") {
      const image = document.createElement("img");
      image.src = `/api/tv/${encodeURIComponent(item.id)}/preview`;
      image.alt = "";
      image.onerror = () => image.remove();
      article.append(image);
    }
    const text = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.title || item.video_id;
    const singer = document.createElement("small");
    singer.textContent = [item.singer_name, ...item.backvocals.filter(vocal => vocal.joined === 1).map(vocal => vocal.singer_name)].join(" + ");
    text.append(name, singer);
    article.append(text);
    return article;
  }));
}

function updateSkipClock() {
  if (skip) document.querySelector("#skip-clock").textContent = `${Math.max(0, Math.ceil((skip.deadline_ms - Date.now()) / 1000))}s`;
}
setInterval(updateSkipClock, 250);

function connect() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const current = new WebSocket(`${protocol}//${location.host}/ws/requests`);
  let failed = false;
  socket = current;
  current.onopen = () => current.send(JSON.stringify({token: ""}));
  current.onmessage = event => {
    failed = false;
    setConnectionStatus("connected", "Conectado");
    const message = JSON.parse(event.data);
    if (message.type === "vocal_activity") {
      if (message.request_id === activeId) animateVocalMarker();
      return;
    }
    if (message.type === "score_update") {
      if (message.request_id !== activeId) return;
      if (message.result === "hit" && message.block_index != null) hitBlocks.add(message.block_index);
      liveScore.textContent = `${message.name}: ${message.points.toFixed(1)} / ${message.ranking_max}`;
      liveScore.hidden = false;
      return;
    }
    if (message.type === "requests") render(message);
  };
  current.onerror = () => {
    if (socket !== current) return;
    failed = true;
    setConnectionStatus("failed", "Falha na conexão");
  };
  current.onclose = event => {
    if (socket !== current) return;
    if (event.code === 1008) {
      setConnectionStatus("failed", "Falha na conexão");
      return;
    }
    if (!failed) setConnectionStatus("reconnecting", "Reconectando");
    retryTimer = setTimeout(() => {
      setConnectionStatus("reconnecting", "Reconectando");
      connect();
    }, 2000);
  };
}

async function setup() {
  const state = await command("/api/tv/state");
  render(state);
  connect();
}

async function refreshJoin() {
  try {
    const join = await command("/api/tv/join");
    document.querySelector("#join-url").textContent = join.url;
    const qr = document.querySelector("#qr");
    qr.src = "/api/tv/qr";
    qr.hidden = false;
  } catch (problem) {
    document.querySelector("#join-url").textContent = problem.message;
  }
}

playButton.addEventListener("click", async () => {
  try { await video.play(); playButton.hidden = true; } catch { error.textContent = "Não foi possível reproduzir o vídeo."; }
});

video.addEventListener("ended", async () => {
  if (!activeId || finishing) return;
  finishing = true;
  const finishedId = activeId;
  try {
    await command(`/api/tv/${encodeURIComponent(finishedId)}/finish`, {method:"POST"});
    if (activeId === finishedId) stopPlayback();
  } catch (problem) { error.textContent = problem.message; }
  finally { finishing = false; }
});
video.addEventListener("timeupdate", renderCaptionBar);
video.addEventListener("play", () => {
  if (captionFrame == null) captionFrame = requestAnimationFrame(animateCaptionBars);
});
video.addEventListener("pause", () => {
  cancelAnimationFrame(captionFrame);
  captionFrame = null;
  renderCaptionBar();
});

document.addEventListener("keydown", event => {
  if (show.hidden || event.target instanceof HTMLInputElement) return;
  if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "n") {
    event.preventDefault();
    resetParty();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    moveTrack(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key.toLowerCase() === "c" && !event.ctrlKey && !event.altKey) {
    cycleColor();
  }
});

function moveTrack(direction) {
  barPosition = (barPosition + (direction > 0 ? 1 : 2)) % 3;
  lyricTrack.style.top = [
    "max(7%, 105px)",
    "50%",
    "min(88%, calc(100% - 105px))",
  ][barPosition];
}

document.querySelector("#move-track-action").addEventListener("click", () => moveTrack(1));

function cycleColor() {
  colorIndex = (colorIndex + 1) % colors.length;
  for (const block of visibleBlocks.values()) {
    block.style.setProperty("--block-color", colors[colorIndex]);
  }
}

async function resetParty() {
  const confirmed = await confirmDialog.open({
    title: "Iniciar nova festa?",
    message: "Todos os cantores sairão e a fila, pontuações e vídeos baixados serão apagados.",
    confirmLabel: "Iniciar nova festa",
  });
  if (!confirmed) return;
  try {
    await command("/api/tv/reset", {method:"POST"});
    stopPlayback();
    party = null;
    render(await command("/api/tv/state"));
  } catch (problem) { error.textContent = problem.message; }
}

document.querySelector("#color-action").addEventListener("click", cycleColor);
document.querySelector("#reset-action").addEventListener("click", resetParty);

setup().catch(problem => {
  setConnectionStatus("failed", "Falha na conexão");
  error.textContent = problem.message;
});