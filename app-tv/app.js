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
const boostBar = document.querySelector("#boost-bar");
const boostLabel = document.querySelector("#boost-label");
const boostStars = document.querySelector("#boost-stars");
const starColors = ["#ff2bd6", "#00e5ff", "#39ff88", "#fff34d", "#b58cff"];
const svgNamespace = "http://www.w3.org/2000/svg";
let boostFillPercent = 25;
let boostDurationMs = 15000;
let boostMultiplier = 1.5;
let boostCharge = 0;
let boostRemainingMs = 0;
let boostFrameAt = null;
let boostedBlocks = new Set();
let socket;
let retryTimer;
let activeId = null;
let playbackSyncTimer = null;
let playbackCalibrationStartedAt = 0;
let lastPlaybackSyncAt = 0;
let finishing = false;
let skip = null;
let barPosition = 1;
let colorIndex = 0;
let party = null;
let activeSingerId = null;
let captionBars = [];
let scoreToleranceMs = 100;
let scoreBlockMs = 1000;
let scoreLaneCount = 3;
let offCuePenalty = 1;
let offCueRearmMs = 500;
let noisePercent = null;
let noiseTimer = null;
let noiseRequest = null;
let noiseResetting = false;
let noiseHoldTimer = null;
let noiseHeldKey = null;
let lastOffcuePenaltyAt = null;
let hitBlocks = new Set();
let blockResults = new Map();
let sentBlockResults = new Set();
let sentOffcuePenalties = new Set();
let microphoneStateEvents = [];
let captionFrame = null;
const visibleBlocks = new Map();
const approachMs = 3000;
const laneResetPauseMs = 5000;
const colors = ["#bda145", "#ff70ac", "#6bded0", "#c9fa45", "#f5f5ee"];
const confirmDialog = new ConfirmDialog();
const scorePanel = document.querySelector("#score-panel");
const scoreBackdrop = document.querySelector("#score-backdrop");
const scoreStar = document.querySelector("#score-star");
const scoreValue = document.querySelector("#score-value");
const scoreStreamers = document.querySelector("#score-streamers");
const missColor = "#f47783";
const hitColor = "#5ac8e4";
const goldColor = "#ffe94d";
let rankingMax = 1000;
let activeScorePoints = 0;
let starLowPercent = 25;
let starHighPercent = 75;
let starNicePercent = 91;
let starCloseMs = 30000;
let scoreShow = null;
let audioContext = null;
let noise = null;

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

function formatNumber(value, digits = 1) {
  return value.toLocaleString("pt-BR", { maximumFractionDigits: digits });
}

function updateBoostBar() {
  const active = boostRemainingMs > 0;
  const level = active ? boostRemainingMs / boostDurationMs : boostCharge;
  boostBar.style.setProperty("--boost", level.toFixed(4));
  boostBar.dataset.state = active ? "active" : boostCharge >= 1 ? "full" : "charging";
  boostBar.setAttribute("aria-valuenow", String(Math.round(level * 100)));
  boostLabel.textContent = !activeId ? "" :
    active ? `Energia ×${formatNumber(boostMultiplier)} · ${Math.ceil(boostRemainingMs / 1000)}s` :
    boostCharge >= 1 ? "Barra cheia · cante mais alto!" : `Energia ${Math.floor(boostCharge * 100)}%`;
}

function sendBoostState() {
  if (socket?.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({
    type: "boost_state", request_id: activeId, active: boostRemainingMs > 0,
    ready: boostRemainingMs <= 0 && boostCharge >= 1,
  }));
}

function resetBoost() {
  const wasShared = boostRemainingMs > 0 || boostCharge >= 1;
  boostCharge = 0;
  boostRemainingMs = 0;
  boostFrameAt = null;
  boostedBlocks = new Set();
  show.classList.remove("boost-active");
  boostBar.classList.remove("just-filled");
  boostStars.replaceChildren();
  updateBoostBar();
  if (wasShared) sendBoostState();
}

function creditBoostHit(blockIndex) {
  if (boostRemainingMs > 0) {
    boostedBlocks.add(blockIndex);
    spawnBoostStar();
    return;
  }
  if (boostCharge >= 1 || !captionBars.length) return;
  const needed = Math.max(1, Math.ceil(captionBars.length * boostFillPercent / 100));
  boostCharge = Math.min(1, boostCharge + 1 / needed);
  if (boostCharge >= 1) {
    boostBar.classList.remove("just-filled");
    void boostBar.offsetWidth;
    boostBar.classList.add("just-filled");
    sendBoostState();
  }
  updateBoostBar();
}

boostBar.addEventListener("animationend", event => {
  if (event.animationName === "boost-filled") boostBar.classList.remove("just-filled");
});

function startBoost() {
  if (!activeId || boostCharge < 1 || boostRemainingMs > 0) return;
  boostRemainingMs = boostDurationMs;
  boostFrameAt = video.paused ? null : performance.now();
  show.classList.add("boost-active");
  updateBoostBar();
  sendBoostState();
}

function endBoost() {
  boostRemainingMs = 0;
  boostCharge = 0;
  boostFrameAt = null;
  show.classList.remove("boost-active");
  updateBoostBar();
  sendBoostState();
}

function tickBoost() {
  if (boostRemainingMs <= 0) return;
  const now = performance.now();
  if (boostFrameAt !== null) {
    boostRemainingMs -= now - boostFrameAt;
    if (boostRemainingMs <= 0) {
      endBoost();
      return;
    }
    updateBoostBar();
  }
  boostFrameAt = video.paused || video.ended ? null : now;
}

function spawnBoostStar() {
  const width = overlay.clientWidth;
  const height = overlay.clientHeight;
  if (!width || !height) return;
  // Same vertical anchors as moveTrack(); stars avoid the lane where the lyric blocks are.
  const anchors = [Math.max(height * 0.07, 105), height / 2, Math.min(height * 0.88, height - 105)];
  const regions = anchors.filter((_, index) => index !== barPosition);
  const spread = Math.min(height * 0.09, 105) / 2;
  const y = regions[Math.floor(Math.random() * regions.length)] + (Math.random() * 2 - 1) * spread;
  const x = width * (0.1 + Math.random() * 0.8);

  const star = document.createElementNS(svgNamespace, "svg");
  star.setAttribute("viewBox", "0 0 100 100");
  star.classList.add("boost-star");
  star.style.left = `${x}px`;
  star.style.top = `${y}px`;
  star.style.setProperty("--star-color", starColors[Math.floor(Math.random() * starColors.length)]);
  star.style.setProperty("--star-turn", `${Math.round(Math.random() * 60 - 30)}deg`);
  const shape = document.createElementNS(svgNamespace, "polygon");
  shape.setAttribute("points", "50,3 62,36 97,37 69,58 80,93 50,73 20,93 31,58 3,37 38,36");
  const label = document.createElementNS(svgNamespace, "text");
  label.setAttribute("x", "50");
  label.setAttribute("y", "57");
  label.textContent = `${formatNumber(boostMultiplier)}x`;
  star.append(shape, label);
  star.addEventListener("animationend", () => star.remove(), { once: true });
  boostStars.append(star);
  while (boostStars.children.length > 12) boostStars.firstElementChild.remove();
}

function stopPlayback() {
  closeScorePanel();
  activeId = null;
  activeScorePoints = 0;
  resetBoost();
  cancelAnimationFrame(captionFrame);
  captionFrame = null;
  clearInterval(playbackSyncTimer);
  playbackSyncTimer = null;
  playbackCalibrationStartedAt = 0;
  video.pause();
  video.removeAttribute("src");
  video.load();
  overlay.hidden = true;
  lyricTrack.hidden = true;
  lyricText.replaceChildren();
  captionBars = [];
  hitBlocks = new Set();
  blockResults = new Map();
  sentBlockResults = new Set();
  sentOffcuePenalties = new Set();
  lastOffcuePenaltyAt = null;
  microphoneStateEvents = [];
  visibleBlocks.clear();
  playButton.hidden = true;
  document.querySelector("#intermission").hidden = false;
}

async function startPlayback(item) {
  closeScorePanel();
  activeId = item.id;
  activeScorePoints = 0;
  resetBoost();
  captionBars = [];
  hitBlocks = new Set();
  blockResults = new Map();
  sentBlockResults = new Set();
  sentOffcuePenalties = new Set();
  lastOffcuePenaltyAt = null;
  microphoneStateEvents = [];
  visibleBlocks.clear();
  lyricTrack.hidden = false;
  lyricText.replaceChildren();
  video.volume = 1;
  video.src = `/api/tv/${encodeURIComponent(item.id)}/video`;
  playbackCalibrationStartedAt = performance.now();
  lastPlaybackSyncAt = 0;
  loadCaptionBars(item.id);
  clearInterval(playbackSyncTimer);
  playbackSyncTimer = setInterval(() => {
    const now = performance.now();
    const calibration = now - playbackCalibrationStartedAt < 10_000;
    if (!calibration && now - lastPlaybackSyncAt < 500) return;
    sendPlaybackSync();
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

function sendPlaybackSync() {
  if (socket?.readyState !== WebSocket.OPEN || !activeId) return;
  lastPlaybackSyncAt = performance.now();
  socket.send(JSON.stringify({
    type: "playback_sync",
    request_id: activeId,
    position_ms: Math.round(video.currentTime * 1000),
    playing: !video.paused && !video.ended,
  }));
}

for (const eventName of ["playing", "pause", "waiting", "stalled", "seeked"])
  video.addEventListener(eventName, sendPlaybackSync);

async function loadCaptionBars(requestId) {
  try {
    const response = await fetch(`/api/tv/${encodeURIComponent(requestId)}/captions`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      captionBars = [];
      return;
    }
    const result = await response.json();
    if (activeId !== requestId) return;
    captionBars = result.bars || [];
    renderCaptionBar();
  } catch {
    captionBars = [];
  }
}

function renderCaptionBar() {
  // The song is over while the star is shown; late microphone events must not change the score.
  if (scoreShow) return;
  const time = video.currentTime * 1000;
  const width = overlay.clientWidth;
  const speed = width / (2 * approachMs);
  const visible = new Set();
  evaluateOffCueSpeech(time);
  captionBars.forEach((bar, index) => {
    const blockIndex = bar.block_index ?? index;
    if (!blockResults.has(blockIndex) && time >= bar.start_ms + scoreToleranceMs + 350) {
      const timeline = microphoneStateEvents
        .filter(activity => activity.singer_id === activeSingerId &&
          Number.isInteger(activity.state_since_ms))
        .sort((left, right) => left.state_since_ms - right.state_since_ms);
      const stateAtStart = timeline.filter(activity => activity.state_since_ms <= bar.start_ms).at(-1);
      const startedInWindow = timeline.some(activity => activity.speaking &&
        activity.state_since_ms > bar.start_ms &&
        activity.state_since_ms <= bar.start_ms + scoreToleranceMs);
      const hit = !!stateAtStart?.speaking || startedInWindow;
      blockResults.set(blockIndex, hit);
      if (hit) creditBoostHit(blockIndex);
    }
    if (blockResults.has(blockIndex) && !sentBlockResults.has(blockIndex) &&
        socket?.readyState === WebSocket.OPEN && activeSingerId) {
      socket.send(JSON.stringify({
        type: "block_result",
        request_id: activeId,
        event_id: crypto.randomUUID(),
        singer_id: activeSingerId,
        block_index: blockIndex,
        hit: blockResults.get(blockIndex),
        boosted: boostedBlocks.has(blockIndex),
      }));
      sentBlockResults.add(blockIndex);
    }
    const left = width / 2 + (bar.start_ms - time) * speed;
    if (left > width || left < -width / 2) return;
    visible.add(index);
    let block = visibleBlocks.get(index);
    if (!block) {
      block = document.createElement("span");
      block.className = "lyric-block";
      block.textContent = bar.text;
      block.style.setProperty("--lane", laneForBlock(index));
      block.style.setProperty("--block-color", colors[colorIndex]);
      lyricText.append(block);
      visibleBlocks.set(index, block);
    }
    block.dataset.result = hitBlocks.has(blockIndex) ? "hit" :
      blockResults.has(blockIndex) ? (blockResults.get(blockIndex) ? "hit" : "miss") :
    time >= bar.start_ms ? "waiting" : "approaching";
    block.dataset.boosted = String(boostedBlocks.has(blockIndex));
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

function laneForBlock(index) {
  let lane = 0;
  for (let current = 1; current <= index; current += 1) {
    const gap = captionBars[current].start_ms - captionBars[current - 1].start_ms;
    lane = gap > scoreBlockMs + laneResetPauseMs ? 0 : (lane + 1) % scoreLaneCount;
  }
  return lane;
}

function showOffcuePenalty(positionMs) {
  const penalty = document.createElement("span");
  penalty.className = "lyric-block offcue-penalty";
  penalty.dataset.result = "miss";
  penalty.textContent = `-${offCuePenalty} ponto${offCuePenalty === 1 ? "" : "s"}`;
  penalty.style.setProperty("--lane", "0");
  penalty.addEventListener("animationend", () => penalty.remove(), { once: true });
  lyricText.append(penalty);
}

function evaluateOffCueSpeech(currentPositionMs) {
  if (!activeSingerId || !captionBars.length || socket?.readyState !== WebSocket.OPEN) return;
  const timeline = microphoneStateEvents
    .filter(activity => activity.singer_id === activeSingerId && Number.isInteger(activity.state_since_ms))
    .sort((left, right) => left.state_since_ms - right.state_since_ms);
  const latestState = timeline.filter(activity => activity.state_since_ms <= currentPositionMs).at(-1);
  if (!latestState?.speaking) return;
  const insideBlockWindow = captionBars.some(bar =>
    currentPositionMs >= Math.max(0, bar.start_ms - scoreToleranceMs) &&
    currentPositionMs <= bar.start_ms + scoreBlockMs);
  if (insideBlockWindow || (lastOffcuePenaltyAt !== null &&
      currentPositionMs - lastOffcuePenaltyAt < offCueRearmMs)) return;

  const penaltyPositionMs = Math.round(currentPositionMs);
  socket.send(JSON.stringify({
    type: "offcue_penalty",
    request_id: activeId,
    event_id: crypto.randomUUID(),
    singer_id: activeSingerId,
    position_ms: penaltyPositionMs,
  }));
  sentOffcuePenalties.add(penaltyPositionMs);
  lastOffcuePenaltyAt = currentPositionMs;
  showOffcuePenalty(penaltyPositionMs);
}

function animateCaptionBars() {
  renderCaptionBar();
  tickBoost();
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
    clearTimeout(noiseTimer);
    noiseTimer = null;
    noisePercent = null;
    party = snapshot.party;
    refreshJoin();
  }
  if (noiseTimer === null && noiseRequest === null) {
    noisePercent = Math.round(snapshot.microphone_rms_threshold * 100);
    updateNoiseLabel();
  }
  scoreToleranceMs = snapshot.score_tolerance_ms ?? scoreToleranceMs;
  scoreBlockMs = snapshot.score_block_ms ?? scoreBlockMs;
  scoreLaneCount = snapshot.score_lane_count ?? scoreLaneCount;
  offCuePenalty = snapshot.off_cue_penalty ?? offCuePenalty;
  offCueRearmMs = snapshot.off_cue_rearm_ms ?? offCueRearmMs;
  boostFillPercent = snapshot.boost_fill_percent ?? boostFillPercent;
  boostDurationMs = snapshot.boost_duration_ms ?? boostDurationMs;
  boostMultiplier = snapshot.boost_multiplier ?? boostMultiplier;
  rankingMax = snapshot.ranking_max ?? rankingMax;
  starLowPercent = snapshot.star_low_percent ?? starLowPercent;
  starHighPercent = snapshot.star_high_percent ?? starHighPercent;
  starNicePercent = snapshot.star_nice_percent ?? starNicePercent;
  starCloseMs = snapshot.star_close_ms ?? starCloseMs;
  const items = snapshot.items.filter(item => item.position);
  const current = snapshot.invitation && items.find(item => item.id === snapshot.invitation.request_id);
  const next = current || items.slice(0, 4).find(item => item.status === "ready");
  const others = items.filter(item => activeId ? item.id !== activeId : true).slice(0, 4);
  const skipped = snapshot.skip;
  skip = skipped;
  document.querySelector("#skip-notice").hidden = !skipped;
  if (skipped) updateSkipClock();

  if (current && snapshot.invitation.accepted && !skipped) {
    activeSingerId = current.singer_id;
    if (activeId !== current.id) startPlayback(current);
  } else if (activeId && (!current || current.id !== activeId || !snapshot.invitation.accepted)) {
    stopPlayback();
  }
  const leadScore = activeId && snapshot.scores?.find(score => score.singer_id === activeSingerId);
  if (leadScore) activeScorePoints = leadScore.points;

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
  current.onopen = () => {
    current.send(JSON.stringify({token: ""}));
    sendBoostState();
  };
  current.onmessage = event => {
    failed = false;
    setConnectionStatus("connected", "Conectado");
    const message = JSON.parse(event.data);
    if (message.type === "vocal_activity") {
      if (message.request_id === activeId) {
        microphoneStateEvents.push(message);
        if (message.speaking) animateVocalMarker();
        renderCaptionBar();
      }
      return;
    }
    if (message.type === "boost_request") {
      if (message.request_id === activeId) startBoost();
      return;
    }
    if (message.type === "score_update") {
      if (message.request_id !== activeId) return;
      if (message.result === "hit" && message.block_index != null) hitBlocks.add(message.block_index);
      if (message.block_index != null && ["hit", "miss"].includes(message.result)) {
        blockResults.set(message.block_index, message.result === "hit");
      }
      renderCaptionBar();
      if (message.singer_id === activeSingerId) activeScorePoints = message.points;
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

const playAction = document.querySelector("#play-action");
new MutationObserver(() => { playAction.disabled = playButton.hidden; })
  .observe(playButton, {attributes:true, attributeFilter:["hidden"]});
playAction.addEventListener("click", () => playButton.click());

function updateNoiseLabel() {
  document.querySelector("#noise-label").textContent = `Ruído ${noisePercent}%`;
}

async function saveNoise() {
  noiseTimer = null;
  if (noiseRequest || noiseResetting) return;
  const percent = noisePercent;
  noiseRequest = command("/api/tv/noise", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({percent}),
  });
  try {
    await noiseRequest;
  } catch (problem) {
    error.textContent = problem.message;
    if (noisePercent === percent) noisePercent = null;
  } finally {
    noiseRequest = null;
    if (noisePercent !== null && noisePercent !== percent && noiseTimer === null && !noiseResetting) saveNoise();
  }
}

function adjustNoise(step) {
  if (noisePercent === null || noiseResetting) return;
  const next = Math.max(1, Math.min(50, noisePercent + step));
  if (next === noisePercent) return;
  noisePercent = next;
  updateNoiseLabel();
  clearTimeout(noiseTimer);
  noiseTimer = setTimeout(saveNoise, 450);
}

function stopNoiseHold() {
  clearInterval(noiseHoldTimer);
  noiseHoldTimer = null;
  noiseHeldKey = null;
}

window.addEventListener("blur", stopNoiseHold);

video.addEventListener("ended", () => {
  if (!activeId || finishing || scoreShow) return;
  const finishedId = activeId;
  const points = Math.min(Math.max(activeScorePoints, 0), rankingMax);
  resetBoost();
  openScorePanel({
    percent: points * 100 / rankingMax,
    points,
    videoSrc: video.currentSrc,
    onClose: () => finishSong(finishedId),
  });
});

async function finishSong(finishedId) {
  if (finishing) return;
  finishing = true;
  try {
    await command(`/api/tv/${encodeURIComponent(finishedId)}/finish`, {method:"POST"});
    if (activeId === finishedId) stopPlayback();
  } catch (problem) { error.textContent = problem.message; }
  finally { finishing = false; }
}
video.addEventListener("timeupdate", renderCaptionBar);
video.addEventListener("play", () => {
  if (captionFrame == null) captionFrame = requestAnimationFrame(animateCaptionBars);
});
video.addEventListener("pause", () => {
  cancelAnimationFrame(captionFrame);
  captionFrame = null;
  tickBoost();
  boostFrameAt = null;
  renderCaptionBar();
});

document.addEventListener("keydown", event => {
  if (show.hidden || event.target instanceof HTMLInputElement) return;
  const noiseStep = ["+", "="].includes(event.key) ? 1 : ["-", "_"].includes(event.key) ? -1 : 0;
  if (noiseStep && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    if (noiseHeldKey !== event.code) {
      stopNoiseHold();
      noiseHeldKey = event.code;
      adjustNoise(noiseStep);
      noiseHoldTimer = setInterval(() => adjustNoise(noiseStep), 100);
    }
  } else if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "n") {
    event.preventDefault();
    resetParty();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    moveTrack(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key.toLowerCase() === "c" && !event.ctrlKey && !event.altKey) {
    cycleColor();
  } else if (event.key === " " && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    if (!playAction.disabled) playButton.click();
  }
});
document.addEventListener("keyup", event => {
  if (event.code === noiseHeldKey) stopNoiseHold();
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
  stopNoiseHold();
  noiseResetting = true;
  clearTimeout(noiseTimer);
  noiseTimer = null;
  try {
    if (noiseRequest) await noiseRequest.catch(() => {});
    await command("/api/tv/reset", {method:"POST"});
    stopPlayback();
    party = null;
    render(await command("/api/tv/state"));
  } catch (problem) { error.textContent = problem.message; }
  finally { noiseResetting = false; }
}

document.querySelector("#color-action").addEventListener("click", cycleColor);
document.querySelector("#reset-action").addEventListener("click", resetParty);

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function hexToRgb(hex) {
  const value = parseInt(hex.slice(1), 16);
  return [value >> 16 & 255, value >> 8 & 255, value & 255];
}

function mixColor(from, to, amount) {
  const start = hexToRgb(from);
  const end = hexToRgb(to);
  const t = clamp(amount, 0, 1);
  return `rgb(${start.map((channel, index) => Math.round(channel + (end[index] - channel) * t)).join(",")})`;
}

function starColorAt(percent) {
  const low = clamp(starLowPercent, 0, 100);
  const high = clamp(starHighPercent, low, 100);
  if (percent < low) return mixColor(missColor, colors[colorIndex], percent / low);
  if (percent < high) return mixColor(colors[colorIndex], hitColor, (percent - low) / (high - low));
  return mixColor(hitColor, goldColor, high >= 100 ? 1 : (percent - high) / (100 - high));
}

function setStarProgress(percent, points) {
  scoreStar.style.setProperty("--level", percent.toFixed(2));
  scoreStar.style.setProperty("--fill-color", starColorAt(percent));
  scoreValue.textContent = String(points);
}

function openScorePanel({ percent, points, videoSrc = "", onClose = null }) {
  closeScorePanel();
  const finalPercent = clamp(Number(percent) || 0, 0, 100);
  const finalPoints = Math.floor(clamp(Number(points) || 0, 0, rankingMax));
  const perfect = finalPercent >= starNicePercent;
  const current = { timers: [], intervals: [], output: createAudioOutput(), onClose };
  scoreShow = current;
  const later = (callback, delay) => current.timers.push(setTimeout(callback, delay));

  scorePanel.classList.remove("perfect");
  scorePanel.classList.toggle("has-video", !!videoSrc);
  scoreStreamers.replaceChildren();
  setStarProgress(0, 0);
  scorePanel.hidden = false;
  if (videoSrc) {
    scoreBackdrop.muted = true;
    scoreBackdrop.src = videoSrc;
    scoreBackdrop.play().catch(() => {});
  }

  const growMs = 2500 + 5500 * finalPercent / 100;
  if (current.output) playDrumRoll(current.output, growMs / 1000);
  const startedAt = performance.now();
  const grow = setInterval(() => {
    const progress = Math.min(1, (performance.now() - startedAt) / growMs);
    const eased = 1 - (1 - progress) ** 3;
    setStarProgress(finalPercent * eased, Math.floor(finalPoints * eased));
    if (progress < 1) return;
    clearInterval(grow);
    celebrate();
  }, 160);
  current.intervals.push(grow);

  function celebrate() {
    let musicSeconds = perfect ? 15 : 5;
    const streamerMultiplier = finalPercent >= 100 ? 3 : 1;
    if (current.output) {
      playCrash(current.output, current.output.context.currentTime + 0.02);
      musicSeconds = perfect ? playPerfectSong(current.output) : playFanfare(current.output);
    }
    if (perfect) {
      scorePanel.classList.add("perfect");
      launchStreamers(70 * streamerMultiplier);
      if (finalPercent >= 100) launchCelebrationStars(18);
      const confetti = setInterval(() => {
        launchStreamers(5 * streamerMultiplier);
        if (finalPercent >= 100) launchCelebrationStars(3);
      }, 280);
      current.intervals.push(confetti);
      later(() => clearInterval(confetti), musicSeconds * 1000);
    }
    later(() => {
      if (videoSrc) {
        scoreBackdrop.currentTime = 0;
        scoreBackdrop.volume = 0.5;
        scoreBackdrop.muted = false;
        scoreBackdrop.play().catch(() => {});
      }
      later(() => closeScorePanel(true), starCloseMs);
    }, musicSeconds * 1000);
  }
}

function closeScorePanel(completed = false) {
  const current = scoreShow;
  if (!current) return;
  scoreShow = null;
  current.timers.forEach(clearTimeout);
  current.intervals.forEach(clearInterval);
  current.output?.disconnect();
  scoreBackdrop.pause();
  scoreBackdrop.removeAttribute("src");
  scoreBackdrop.load();
  scoreStreamers.replaceChildren();
  scorePanel.hidden = true;
  scorePanel.classList.remove("perfect", "has-video");
  if (completed) current.onClose?.();
}

function launchStreamers(count) {
  const palette = [...starColors, ...colors, hitColor, missColor];
  for (let index = 0; index < count; index += 1) {
    const streamer = document.createElementNS(svgNamespace, "svg");
    streamer.setAttribute("viewBox", "0 0 28 120");
    streamer.classList.add("streamer");
    streamer.style.left = `${Math.random() * 100}%`;
    streamer.style.animationDelay = `${Math.random() * 0.6}s`;
    streamer.style.setProperty("--streamer-color", palette[Math.floor(Math.random() * palette.length)]);
    streamer.style.setProperty("--fall", `${2.8 + Math.random() * 2.4}s`);
    streamer.style.setProperty("--drift", `${Math.round(Math.random() * 260 - 130)}px`);
    streamer.style.setProperty("--spin-from", `${Math.round(Math.random() * 80 - 40)}deg`);
    streamer.style.setProperty("--spin-to", `${Math.round(Math.random() * 720 - 360)}deg`);
    const ribbon = document.createElementNS(svgNamespace, "path");
    ribbon.setAttribute("d", "M14 4 C 26 16, 2 28, 14 40 S 2 64, 14 76 S 26 100, 14 116");
    streamer.append(ribbon);
    streamer.addEventListener("animationend", event => { if (event.target === streamer) streamer.remove(); });
    scoreStreamers.append(streamer);
  }
  while (scoreStreamers.children.length > 480) scoreStreamers.firstElementChild.remove();
}

function launchCelebrationStars(count) {
  const palette = [...starColors, "#fff", "#ffe94d"];
  for (let index = 0; index < count; index += 1) {
    const star = document.createElementNS(svgNamespace, "svg");
    star.setAttribute("viewBox", "0 0 100 100");
    star.classList.add("celebration-star");
    star.style.left = `${Math.random() * 100}%`;
    star.style.top = `${Math.random() * 100}%`;
    star.style.setProperty("--star-color", palette[Math.floor(Math.random() * palette.length)]);
    star.style.setProperty("--star-size", `${22 + Math.random() * 36}px`);
    star.style.setProperty("--star-turn", `${Math.round(Math.random() * 100 - 50)}deg`);
    star.style.animationDelay = `${Math.random() * 0.45}s`;
    const shape = document.createElementNS(svgNamespace, "polygon");
    shape.setAttribute("points", "50,2 61,36 98,38 69,59 80,96 50,75 20,96 31,59 2,38 39,36");
    star.append(shape);
    star.addEventListener("animationend", () => star.remove(), { once: true });
    scoreStreamers.append(star);
  }
}

function createAudioOutput() {
  try {
    audioContext ??= new AudioContext();
    audioContext.resume().catch(() => {});
    const output = audioContext.createGain();
    output.gain.value = 0.8;
    output.connect(audioContext.destination);
    return output;
  } catch {
    return null;
  }
}

function noiseBuffer(context) {
  if (noise?.sampleRate === context.sampleRate) return noise;
  noise = context.createBuffer(1, context.sampleRate * 2, context.sampleRate);
  const data = noise.getChannelData(0);
  for (let index = 0; index < data.length; index += 1) data[index] = Math.random() * 2 - 1;
  return noise;
}

function playNoise(output, when, length, level, filterType, frequency) {
  const context = output.context;
  const source = context.createBufferSource();
  source.buffer = noiseBuffer(context);
  const filter = context.createBiquadFilter();
  filter.type = filterType;
  filter.frequency.value = frequency;
  const gain = context.createGain();
  gain.gain.setValueAtTime(level, when);
  gain.gain.exponentialRampToValueAtTime(0.0001, when + length);
  source.connect(filter).connect(gain).connect(output);
  source.start(when, Math.random() * Math.max(0, 1.9 - length), length + 0.02);
}

function playKick(output, when) {
  const context = output.context;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.frequency.setValueAtTime(150, when);
  oscillator.frequency.exponentialRampToValueAtTime(40, when + 0.14);
  gain.gain.setValueAtTime(0.7, when);
  gain.gain.exponentialRampToValueAtTime(0.0001, when + 0.2);
  oscillator.connect(gain).connect(output);
  oscillator.start(when);
  oscillator.stop(when + 0.22);
}

function playTone(output, midi, when, length, type = "square", level = 0.07) {
  const context = output.context;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = type;
  oscillator.frequency.value = 440 * 2 ** ((midi - 69) / 12);
  gain.gain.setValueAtTime(0.0001, when);
  gain.gain.exponentialRampToValueAtTime(level, when + 0.015);
  gain.gain.setValueAtTime(level, when + Math.max(0.03, length - 0.06));
  gain.gain.exponentialRampToValueAtTime(0.0001, when + length);
  oscillator.connect(gain).connect(output);
  oscillator.start(when);
  oscillator.stop(when + length + 0.02);
}

function playDrumRoll(output, seconds) {
  const start = output.context.currentTime + 0.02;
  for (let offset = 0; offset < seconds; offset += 0.05) {
    const level = (0.12 + 0.4 * offset / seconds) * (0.85 + Math.random() * 0.3);
    playNoise(output, start + offset, 0.045, level, "bandpass", 1800);
  }
}

function playCrash(output, when) {
  playKick(output, when);
  playNoise(output, when, 1.6, 0.45, "highpass", 5000);
}

function playFanfare(output) {
  const beat = 0.4;
  const start = output.context.currentTime + 0.05;
  const melody = [[72, 0, .5], [76, .5, .5], [79, 1, .5], [84, 1.5, 1.5], [79, 3, .5], [84, 3.5, .5],
    [88, 4, 2], [86, 6, .5], [88, 6.5, .5], [91, 7, 3]];
  const bass = [[48, 0, 1.5], [55, 1.5, 1.5], [48, 3, 1], [53, 4, 2], [55, 6, 1], [48, 7, 3]];
  for (const [note, at, length] of melody) playTone(output, note, start + at * beat, length * beat, "square", 0.06);
  for (const [note, at, length] of bass) playTone(output, note, start + at * beat, length * beat, "triangle", 0.18);
  for (const at of [0, 1.5, 3, 4, 6, 7]) playKick(output, start + at * beat);
  playCrash(output, start + 7 * beat);
  return 10 * beat + 0.4;
}

function playPerfectSong(output) {
  const beat = 0.36;
  const start = output.context.currentTime + 0.05;
  const chords = [[60, 64, 67], [55, 59, 62], [57, 60, 64], [53, 57, 60]];
  const hook = [[76, 0, 1], [79, 1, .5], [81, 1.5, .5], [79, 2, 1], [76, 3, 1],
    [74, 4, 1], [79, 5, 1], [83, 6, 1], [81, 7, 1],
    [81, 8, 1], [84, 9, .5], [83, 9.5, .5], [81, 10, 1], [79, 11, 1],
    [77, 12, 1], [81, 13, 1], [79, 14, 2]];
  for (let bar = 0; bar < 9; bar += 1) {
    const chord = chords[bar % chords.length];
    const barStart = start + bar * 4 * beat;
    for (let step = 0; step < 8; step += 1) {
      const when = barStart + step * beat / 2;
      playTone(output, chord[[0, 1, 2, 1][step % 4]] + 12, when, beat / 2, "triangle", 0.05);
      playNoise(output, when, 0.04, 0.08, "highpass", 7000);
    }
    for (let count = 0; count < 4; count += 1) {
      const when = barStart + count * beat;
      playTone(output, chord[0] - 12, when, beat * 0.9, "triangle", 0.2);
      if (count % 2 === 0) playKick(output, when);
      else playNoise(output, when, 0.14, 0.3, "bandpass", 1800);
    }
  }
  for (let repeat = 0; repeat < 9 * 4; repeat += 16) {
    for (const [note, at, length] of hook) {
      if (repeat + at < 36) playTone(output, note, start + (repeat + at) * beat, length * beat, "square", 0.06);
    }
  }
  const ending = start + 36 * beat;
  for (const note of [60, 64, 67, 72, 76, 84]) playTone(output, note, ending, 3.5 * beat, "square", 0.045);
  playTone(output, 36, ending, 3.5 * beat, "triangle", 0.22);
  playCrash(output, ending);
  return 40 * beat + 0.4;
}

setup().catch(problem => {
  setConnectionStatus("failed", "Falha na conexão");
  error.textContent = problem.message;
});