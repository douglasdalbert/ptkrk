import { ConfirmDialog } from "/cantor/assets/confirm-dialog.js";

const show = document.querySelector("#show");
const video = document.querySelector("#video");
const overlay = document.querySelector("#video-overlay");
const playButton = document.querySelector("#play");
const error = document.querySelector("#error");
const connectionStatus = document.querySelector("#connect-status");
const lyricTrack = document.querySelector("#track");
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
let activeCaptionBar = -1;
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
  clearInterval(playbackSyncTimer);
  playbackSyncTimer = null;
  video.pause();
  video.removeAttribute("src");
  video.load();
  overlay.hidden = true;
  lyricTrack.hidden = true;
  lyricText.textContent = "";
  liveScore.hidden = true;
  liveScore.textContent = "";
  captionBars = [];
  activeCaptionBar = -1;
  playButton.hidden = true;
  document.querySelector("#intermission").hidden = false;
}

async function startPlayback(item) {
  activeId = item.id;
  captionBars = [];
  activeCaptionBar = -1;
  lyricTrack.hidden = true;
  lyricText.textContent = "";
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
  const index = captionBars.findIndex(bar => time >= bar.start_ms && time < bar.end_ms);
  if (index === activeCaptionBar) return;
  activeCaptionBar = index;
  if (index < 0) {
    lyricTrack.hidden = true;
    lyricText.textContent = "";
    return;
  }
  lyricText.textContent = captionBars[index].text;
  lyricTrack.hidden = false;
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
    if (message.type === "score_update") {
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
  lyricTrack.style.top = ["7%", "48%", "88%"][barPosition];
}

document.querySelector("#move-track-action").addEventListener("click", () => moveTrack(1));

function cycleColor() {
  colorIndex = (colorIndex + 1) % colors.length;
  lyricTrack.style.borderColor = colors[colorIndex];
  lyricTrack.style.color = colors[colorIndex];
  lyricTrack.style.backgroundColor = `${colors[colorIndex]}55`;
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