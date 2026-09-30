import { ConfirmDialog } from "./confirm-dialog.js";

const storageKey = "karaoke.singer";
const entry = document.querySelector("#entry");
const sessionView = document.querySelector("#session");
const connectionLabel = document.querySelector("#connection");
const entryForm = document.querySelector("#entry-form");
const requestForm = document.querySelector("#request-form");
const requestList = document.querySelector("#request-list");
const previewCache = new Map();
const acknowledgedFailures = new Set();
const invitationView = document.querySelector("#invitation");
const acceptButton = document.querySelector("#accept-button");
const groupButton = document.querySelector("#group-button");
const groupDialog = document.querySelector("#group-dialog");
const groupList = document.querySelector("#group-list");
const groupStart = document.querySelector("#group-start");
const confirmDialog = new ConfirmDialog();
const skippingView = document.querySelector("#skipping");
const singingOverlay = document.querySelector("#singing-overlay");
const microphoneButton = document.querySelector("#microphone-button");
const microphoneWaveform = document.querySelector("#microphone-waveform");
const microphonePermissionButton = document.querySelector("#microphone-permission-button");
const microphonePermissionStatus = document.querySelector("#microphone-permission-status");
const scoreStatus = document.querySelector("#score-status");
const actionFooter = document.querySelector("#action-footer");
const skipAction = document.querySelector("#skip-action");
const skipButton = document.querySelector("#skip-button");
let singer = null;
let currentParty = null;
let socket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let currentInvitation = null;
let currentSkip = null;
let currentSong = null;
let allowSkip = false;
let latestItems = [];
let partySingers = [];
let joinedGroupRooms = new Set();
let groupRoomReady = false;
let groupPollTimer = null;
let pendingGroupPrompt = null;
let failureAlertActive = false;
let playbackClock = null;
let activeBars = [];
let microphoneStream = null;
let microphoneContext = null;
let microphoneAnalyser = null;
let microphoneFrame = null;
let loudFrameCount = 0;
let currentScores = [];
let currentRankingMax = 1000;
let scoredBlocks = new Set();
let scoredOffCueWindows = new Set();
let scoredRequestId = null;
let offCueRearmMs = 500;
let microphoneRmsThreshold = 0.04;

function stopMicrophone() {
  if (microphoneFrame !== null) cancelAnimationFrame(microphoneFrame);
  microphoneFrame = null;
  microphoneStream?.getTracks().forEach(track => track.stop());
  microphoneStream = null;
  microphoneAnalyser = null;
  if (microphoneContext && microphoneContext.state !== "closed") microphoneContext.close();
  microphoneContext = null;
  loudFrameCount = 0;
  drawMicrophoneWaveform();
    microphoneButton.textContent = "Ativar microfone";
    microphoneButton.disabled = false; // Enable the microphone button
}

function connectionState(text, state = "connecting") {
  connectionLabel.dataset.state = state;
  connectionLabel.title = text;
  connectionLabel.setAttribute("aria-label", text);
}

function showError(element, message) {
  element.textContent = message;
  element.classList.add("error");
  element.setAttribute("role", "alert");
  element.hidden = false;
}

function clearError(element) {
  element.classList.remove("error");
  element.setAttribute("role", "status");
}

function youtubeCode(input) {
  const value = input.trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(value)) return value;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Informe um link ou código válido do YouTube.");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port) {
    throw new Error("Informe um link HTTPS do YouTube.");
  }
  let code = "";
  if (["youtube.com", "www.youtube.com", "m.youtube.com"].includes(url.hostname) && url.pathname === "/watch") {
    const values = url.searchParams.getAll("v");
    if (values.length === 1) code = values[0];
  } else if (url.hostname === "youtu.be" && /^\/[A-Za-z0-9_-]{11}$/.test(url.pathname)) {
    code = url.pathname.slice(1);
  }
  if (!/^[A-Za-z0-9_-]{11}$/.test(code)) throw new Error("Informe um link ou código válido do YouTube.");
  return code;
}

function clearSession() {
  if (confirmDialog.dialog.open) confirmDialog.dialog.close();
  if (groupDialog.open) groupDialog.close();
  singer = null;
  currentParty = null;
  currentInvitation = null;
  currentSkip = null;
  currentSong = null;
  playbackClock = null;
  activeBars = [];
  scoredBlocks.clear();
  currentScores = [];
  currentRankingMax = 1000;
  scoredRequestId = null;
  stopMicrophone();
  allowSkip = false;
  latestItems = [];
  partySingers = [];
  joinedGroupRooms.clear();
  groupRoomReady = false;
  clearInterval(groupPollTimer);
  groupPollTimer = null;
  pendingGroupPrompt = null;
  acknowledgedFailures.clear();
  if (singingOverlay.open) singingOverlay.close();
  invitationView.hidden = true;
  skippingView.hidden = true;
  skipAction.hidden = true;
  actionFooter.hidden = true;
  for (const selector of ["#request-message", "#skip-message", "#invitation-message", "#entry-error"]) {
    const message = document.querySelector(selector);
    clearError(message);
    message.textContent = "";
  }
  document.querySelector("#entry-error").hidden = true;
  document.querySelector("#failure-alert").hidden = true;
  document.querySelector("#failure-alert").textContent = "";
  document.querySelector("#sign-out").hidden = true;
  document.querySelector("#identity-label").textContent = "/ CANTOR";
  localStorage.removeItem(storageKey);
  clearTimeout(reconnectTimer);
  socket?.close();
  socket = null;
  for (const preview of previewCache.values()) {
    if (preview.url) URL.revokeObjectURL(preview.url);
  }
  previewCache.clear();
  requestList.replaceChildren();
  entry.hidden = false;
  sessionView.hidden = true;
  connectionState("Aguardando");
}

function partyEnded() {
  clearSession();
  const error = document.querySelector("#entry-error");
  showError(error, "Festa encerrada. Entre novamente.");
  return new Error(error.textContent);
}

function showSession(identity) {
  if (!identity.singer_id && identity.client_id) {
    identity.singer_id = identity.client_id;
    delete identity.client_id;
    localStorage.setItem(storageKey, JSON.stringify(identity));
  }
  singer = identity;
  currentParty = identity.party || null;
  entry.hidden = true;
  sessionView.hidden = false;
  document.querySelector("#identity-label").textContent = `/ ${identity.name}`;
  document.querySelector("#sign-out").hidden = false;
  connectionState("Conectando");
  connect();
}

async function api(path, options = {}) {
  const requestSinger = singer;
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(singer ? { Authorization: `Bearer ${singer.token}` } : {}),
    },
  });
  if (requestSinger && singer !== requestSinger) throw new Error("Sessão alterada. Tente novamente.");
  if (response.status === 401) {
    throw partyEnded();
  }
  const responseParty = response.headers.get("X-Karaoke-Party");
  if (requestSinger && responseParty) {
    if (requestSinger.party && requestSinger.party !== responseParty) throw partyEnded();
    if (!requestSinger.party) {
      requestSinger.party = responseParty;
      currentParty = responseParty;
      localStorage.setItem(storageKey, JSON.stringify(requestSinger));
    }
  }
  if (response.status === 204) return null;
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : "Não foi possível concluir a operação.");
  }
  if (!requestSinger && path === "/api/singers" && payload.party !== responseParty) {
    throw new Error("A festa reiniciou. Informe seu nome novamente.");
  }
  return payload;
}

function connect() {
  if (!singer) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const current = new WebSocket(`${protocol}//${location.host}/ws/requests`);
  socket = current;
  current.addEventListener("open", () => {
    if (current !== socket) return;
    current.send(JSON.stringify({ token: singer.token }));
  });
  current.addEventListener("message", (event) => {
    if (current !== socket) return;
    const message = JSON.parse(event.data);
    if (message.type === "playback_sync") {
      playbackClock = {...message, receivedAt: performance.now()};
      return;
    }
    if (message.type === "score_update") {
      if (message.singer_id !== singer?.singer_id) return;
      const action = message.result === "hit" ? "Acertou" : "Som fora de um bloco";
      scoreStatus.textContent = `${action}: ${message.hits} acertos, ${message.penalties} erros. ${message.points} / ${message.ranking_max} pontos.`;
      document.querySelector("#song-points").textContent = message.points.toFixed(1);
      document.querySelector("#song-rank").textContent = `${message.points.toFixed(1)} / ${message.ranking_max}`;
      return;
    }
    if (message.type === "group_state") {
      applyGroupRoomState(message);
      return;
    }
    if (message.type === "requests") {
      if (currentParty && message.party !== currentParty) {
        partyEnded();
        return;
      }
      currentParty = message.party;
      if (!singer.party) {
        singer.party = message.party;
        localStorage.setItem(storageKey, JSON.stringify(singer));
      }
      reconnectDelay = 1000;
      connectionState("Conectado", "connected");
      latestItems = message.items;
      partySingers = message.singers || [];
      currentScores = message.scores || [];
      currentRankingMax = message.ranking_max || 1000;
      offCueRearmMs = message.off_cue_rearm_ms || 500;
      microphoneRmsThreshold = message.microphone_rms_threshold || 0.04;
      activeBars = message.active_bars || [];
      if (message.invitation?.request_id !== scoredRequestId) {
        scoredRequestId = message.invitation?.request_id || null;
        scoredBlocks.clear();
        scoredOffCueWindows.clear();
      }
      renderScores();
      renderSkip(message);
      renderInvitation(message.invitation, message.items);
      renderSingingOverlay(message.invitation, message.items);
      renderGroupList();
      const activeItem = message.items.find(item => item.id === message.invitation?.request_id);
      const ownPendingInvite = activeItem?.backvocals.some(vocal =>
        vocal.singer_id === singer.singer_id && vocal.joined === 0);
      if (activeItem && (ownPendingInvite || (groupDialog.open && activeItem.singer_id === singer.singer_id))) {
        joinGroupRoom(activeItem.id);
      }
      promptGroupInvite(message.invitation, message.items);
      renderRequests(message.items);
      showFailedRequest(message.items);
    }
  });
  current.addEventListener("close", (event) => {
    if (current !== socket || !singer) return;
    socket = null;
    if (event.code === 1008) {
      partyEnded();
      return;
    }
    joinedGroupRooms.clear();
    connectionState("Falha na conexão", "failed");
    reconnectTimer = setTimeout(() => {
      connectionState("Reconectando");
      connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  });
}

function joinGroupRoom(requestId) {
  if (socket?.readyState !== WebSocket.OPEN || joinedGroupRooms.has(requestId)) return;
  joinedGroupRooms.add(requestId);
  socket.send(JSON.stringify({ type: "join_group", request_id: requestId }));
}

function applyGroupRoomState(message) {
  const item = latestItems.find(request => request.id === message.request_id);
  if (!item || item.video_id !== message.video_id) return;
  for (const member of message.members) {
    const vocal = item.backvocals.find(entry => entry.singer_id === member.singer_id);
    if (vocal) Object.assign(vocal, member);
  }
  renderGroupList();
}

function startGroupRoomPolling(requestId) {
  clearInterval(groupPollTimer);
  groupPollTimer = setInterval(async () => {
    if (!groupDialog.open || currentInvitation?.request_id !== requestId || !groupRoomReady) {
      clearInterval(groupPollTimer);
      groupPollTimer = null;
      return;
    }
    try {
      const state = await api(`/api/requests/${encodeURIComponent(requestId)}/group/state`);
      applyGroupRoomState(state);
    } catch (problem) {
      if (singer && groupDialog.open) showError(document.querySelector("#group-message"), problem.message);
    }
  }, 3000);
}

function previewFor(item, placeholder) {
  if (item.status !== "ready") return;
  let cached = previewCache.get(item.video_id);
  if (!cached) {
    cached = { url: null, task: null };
    const previewSinger = singer;
    cached.task = fetch(`/api/requests/${encodeURIComponent(item.id)}/preview`, {
      headers: { Authorization: `Bearer ${previewSinger.token}` },
    }).then(async (response) => {
      if (singer !== previewSinger) return;
      if (response.status === 401 || (previewSinger.party && response.headers.get("X-Karaoke-Party") !== previewSinger.party)) {
        partyEnded();
        return;
      }
      if (!response.ok) return;
      cached.url = URL.createObjectURL(await response.blob());
    }).catch(() => {});
    previewCache.set(item.video_id, cached);
  }
  cached.task?.then(() => {
    if (!placeholder.isConnected || !cached.url) return;
    const image = document.createElement("img");
    image.src = cached.url;
    image.alt = "";
    placeholder.replaceWith(image);
  });
}

function renderRequests(items) {
  const visibleItems = items.filter(item => item.status !== "failed" ||
    ((item.singer_id === singer?.singer_id || item.backvocals.some(vocal => vocal.singer_id === singer?.singer_id && vocal.joined === 1)) &&
      !acknowledgedFailures.has(item.id)));
  document.querySelector("#request-count").textContent = String(visibleItems.length);
  document.querySelector("#empty").hidden = visibleItems.length !== 0;
  const statuses = { pending: "Em fila", processing: "Processando", ready: "Pronto", failed: "Falhou" };
  const content = document.createDocumentFragment();
  for (const item of visibleItems) {
    const row = document.createElement("li");
    row.className = "request-item";
    const placeholder = document.createElement("span");
    placeholder.className = "preview-empty";
    placeholder.textContent = "♫";
    previewFor(item, placeholder);
    const details = document.createElement("div");
    details.className = "request-details";
    const title = document.createElement("strong");
    title.textContent = item.title || `youtube.com/watch?v=${item.video_id}`;
    const singerName = document.createElement("small");
    const songSinger = document.createElement("span");
    songSinger.textContent = item.singer_name;
    if (item.singer_id === singer?.singer_id) songSinger.className = "current-singer";
    singerName.append(songSinger);
    for (const vocal of item.backvocals.filter(vocal => vocal.joined === 1)) {
      const vocalName = document.createElement("span");
      vocalName.textContent = vocal.singer_name;
      if (vocal.singer_id === singer?.singer_id) vocalName.className = "current-singer";
      singerName.append(document.createTextNode(" + "), vocalName);
    }
    details.append(title, singerName);
    const status = document.createElement("span");
    status.className = `status ${item.status}`;
    const state = document.createElement("span");
    state.className = "state-icon";
    state.setAttribute("aria-hidden", "true");
    state.textContent = { pending: "○", processing: "", ready: "✓", failed: "\u26A0\uFE0E" }[item.status] || "";
    const stateText = document.createElement("span");
    stateText.textContent = statuses[item.status] || item.status;
    status.append(state, stateText);
    if (item.position) {
      const position = document.createElement("span");
      position.className = "queue-position";
      position.textContent = `#${item.position}`;
      status.prepend(position);
    }
    row.append(placeholder, details, status);
    const mine = singer && (item.singer_id === singer.singer_id ||
      item.backvocals.some(vocal => vocal.singer_id === singer.singer_id && vocal.joined === 1));
    const acceptedMine = currentInvitation?.request_id === item.id &&
      (item.singer_id === singer?.singer_id ? currentInvitation.lead_accepted :
        currentInvitation.backvocals.some(vocal => vocal.singer_id === singer?.singer_id && vocal.accepted));
    if (mine && !acceptedMine) {
      const remove = document.createElement("button");
      remove.className = "remove-button";
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "Remover música";
      remove.setAttribute("aria-label", `Remover ${title.textContent}`);
      remove.addEventListener("click", async () => {
        const cached = previewCache.get(item.video_id);
        const preview = cached?.task?.then(() => cached.url);
        const confirmed = await confirmDialog.open({
          title: item.singer_id === singer.singer_id ? "Remover música?" : "Sair do grupo?",
          message: item.singer_id === singer.singer_id ? `Quer remover "${title.textContent}"?` :
            `Quer deixar de cantar "${title.textContent}"? A música continua para os demais.`,
          preview,
        });
        if (!confirmed) return;
        remove.disabled = true;
        try {
          await api(`/api/requests/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        } catch (problem) {
          showError(document.querySelector("#request-message"), problem.message);
          remove.disabled = false;
        }
      });
      row.append(remove);
    }
    content.append(row);
  }
  requestList.replaceChildren(content);
}

function showFailedRequest(items) {
  latestItems = items;
  if (!singer || failureAlertActive) return;
  const failed = items.find(item => item.status === "failed" &&
    (item.singer_id === singer.singer_id || item.backvocals.some(vocal => vocal.singer_id === singer.singer_id && vocal.joined === 1)) &&
    !acknowledgedFailures.has(item.id));
  if (!failed) return;
  const owner = singer;
  failureAlertActive = true;
  (async () => {
    const alert = document.querySelector("#failure-alert");
    alert.textContent = `Não foi possível preparar "${failed.title || `youtube.com/watch?v=${failed.video_id}`}". ${failed.error || "O vídeo não pôde ser preparado."}`;
    alert.hidden = false;
    try {
      await new Promise(resolve => setTimeout(resolve, 2000));
      if (singer !== owner) return;
      acknowledgedFailures.add(failed.id);
      alert.hidden = true;
      alert.textContent = "";
      renderRequests(latestItems);
      await api(`/api/requests/${encodeURIComponent(failed.id)}`, { method: "DELETE" });
    } catch (problem) {
      if (singer === owner) showError(document.querySelector("#request-message"), problem.message);
    } finally {
      failureAlertActive = false;
      if (singer === owner) showFailedRequest(latestItems);
    }
  })();
}

function renderInvitation(invitation, items) {
  if (!invitation || !singer || !items.some((item) => item.id === invitation.request_id &&
    (item.singer_id === singer.singer_id || item.backvocals.some(vocal => vocal.singer_id === singer.singer_id)))) {
    currentInvitation = null;
    invitationView.hidden = true;
    updateFooter();
    return;
  }
  const isNew = currentInvitation?.request_id !== invitation.request_id;
  currentInvitation = invitation;
  const item = items.find((request) => request.id === invitation.request_id);
  const myVocal = item.backvocals.find(vocal => vocal.singer_id === singer.singer_id);
  const acceptedMine = item.singer_id === singer.singer_id ? invitation.lead_accepted :
    myVocal && (myVocal.accepted || (invitation.accepted && myVocal.joined === 1 && !myVocal.score_eligible));
  const lateJoin = invitation.accepted && myVocal?.joined === 1 && !myVocal.score_eligible;
  invitationView.hidden = (acceptedMine && !lateJoin) || (myVocal && myVocal.joined !== 1);
  document.querySelector("#invitation-song").textContent = item.title || item.video_id;
  acceptButton.hidden = acceptedMine;
  groupButton.hidden = item.singer_id !== singer.singer_id || acceptedMine || !!currentSkip;
  acceptButton.disabled = !!currentSkip;
  const invitationMessage = document.querySelector("#invitation-message");
  clearError(invitationMessage);
  invitationMessage.textContent = lateJoin ? "Você entrou depois do início e não participa da pontuação desta música." :
    acceptedMine ? "Confirmado. Aguarde os demais cantores." : "";
  updateFooter();
  if (isNew && !acceptedMine && "vibrate" in navigator) navigator.vibrate([250, 150, 250]);
}

function renderSingingOverlay(invitation, items) {
  const item = items.find(request => request.id === invitation?.request_id);
  const isParticipant = item && singer && (item.singer_id === singer.singer_id ||
    item.backvocals.some(vocal => vocal.singer_id === singer.singer_id && vocal.joined === 1));
  const shouldShow = invitation?.accepted && isParticipant;
  if (shouldShow && !singingOverlay.open) {
    singingOverlay.showModal();
    drawMicrophoneWaveform();
  }
  if (!shouldShow && singingOverlay.open) {
    singingOverlay.close();
    stopMicrophone();
    playbackClock = null;
      microphoneButton.textContent = "Ativar microfone"; // Reset button text when closing
      microphoneButton.disabled = false; // Enable the microphone button when closing
  }
  microphoneButton.hidden = !shouldShow;
  const eligible = shouldShow && (item.singer_id === singer?.singer_id ? invitation.lead_accepted :
    item.backvocals.some(vocal => vocal.singer_id === singer?.singer_id && vocal.score_eligible && vocal.accepted));
  microphoneButton.disabled = !eligible || !navigator.mediaDevices?.getUserMedia;
  if (!navigator.mediaDevices?.getUserMedia) {
    scoreStatus.textContent = "Microfone requer HTTPS seguro neste aparelho.";
  }
}

function renderScores() {
  const score = currentScores.find(item => item.singer_id === singer?.singer_id);
  document.querySelector("#song-points").textContent = score ? score.points.toFixed(1) : "0.0";
  document.querySelector("#song-rank").textContent = score ?
    `${score.points.toFixed(1)} / ${score.ranking_max}` : `0.0 / ${currentRankingMax}`;
}

function estimatePlaybackPosition() {
  if (!playbackClock || !playbackClock.playing) return null;
  return Math.round(playbackClock.position_ms + performance.now() - playbackClock.receivedAt);
}

function drawMicrophoneWaveform(samples = null) {
  const width = microphoneWaveform.clientWidth;
  const height = microphoneWaveform.clientHeight;
  if (!width || !height) return;
  const pixelRatio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * pixelRatio);
  const pixelHeight = Math.round(height * pixelRatio);
  if (microphoneWaveform.width !== pixelWidth || microphoneWaveform.height !== pixelHeight) {
    microphoneWaveform.width = pixelWidth;
    microphoneWaveform.height = pixelHeight;
  }
  const context = microphoneWaveform.getContext("2d");
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.beginPath();
  context.moveTo(0, height / 2);
  context.lineTo(width, height / 2);
  context.lineWidth = 1;
  context.strokeStyle = "#ffffff4d";
  context.stroke();
  if (!samples) return;
  context.beginPath();
  for (let x = 0; x < width; x += 1) {
    const sampleIndex = Math.floor(x / width * (samples.length - 1));
    const y = height / 2 - samples[sampleIndex] * (height * 0.45);
    if (x === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  }
  context.lineWidth = 2;
  context.strokeStyle = "#64d4c3";
  context.stroke();
}

function sampleMicrophone() {
  if (!microphoneAnalyser || !microphoneStream) return;
  const samples = new Float32Array(microphoneAnalyser.fftSize);
  microphoneAnalyser.getFloatTimeDomainData(samples);
  drawMicrophoneWaveform(samples);
  let squareSum = 0;
  for (const sample of samples) squareSum += sample * sample;
  const rms = Math.sqrt(squareSum / samples.length);
  const requestId = currentInvitation?.request_id;
  const position = estimatePlaybackPosition();
  if (rms >= microphoneRmsThreshold) {
    loudFrameCount += 1;
    if (loudFrameCount >= 2 && requestId && position === null) {
      scoreStatus.textContent = "Aguardando sincronismo da TV.";
    }
    if (loudFrameCount >= 2 && requestId && position !== null && socket?.readyState === WebSocket.OPEN) {
      const bar = activeBars.find(candidate =>
        position >= candidate.score_window_start_ms && position <= candidate.score_window_end_ms);
      if (bar && !scoredBlocks.has(bar.block_index)) {
        scoredBlocks.add(bar.block_index);
        socket.send(JSON.stringify({
          type: "vocal_onset",
          request_id: requestId,
          event_id: crypto.randomUUID(),
          position_ms: position,
        }));
      } else if (!bar) {
        const offCueWindow = Math.floor(position / offCueRearmMs);
        if (!scoredOffCueWindows.has(offCueWindow)) {
          scoredOffCueWindows.add(offCueWindow);
          socket.send(JSON.stringify({
            type: "vocal_onset",
            request_id: requestId,
            event_id: crypto.randomUUID(),
            position_ms: position,
          }));
        }
      }
    }
  } else {
    loudFrameCount = 0;
  }
  microphoneFrame = requestAnimationFrame(sampleMicrophone);
}

microphoneButton.addEventListener("click", async () => {
  microphoneButton.disabled = true;
  try {
    microphoneStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    microphoneContext = new AudioContext();
    const source = microphoneContext.createMediaStreamSource(microphoneStream);
    microphoneAnalyser = microphoneContext.createAnalyser();
    microphoneAnalyser.fftSize = 1024;
    source.connect(microphoneAnalyser);
    drawMicrophoneWaveform();
    scoreStatus.textContent = "Microfone ativo. Cante junto com as barras.";
    microphoneButton.textContent = "Microfone ativo";
    sampleMicrophone();
  } catch {
    stopMicrophone();
    microphoneButton.disabled = false;
    scoreStatus.textContent = "Não foi possível acessar o microfone. Verifique a permissão e o HTTPS.";
  }
});

singingOverlay.addEventListener("cancel", event => event.preventDefault());

microphonePermissionButton.addEventListener("click", async () => {
  const getUserMedia = navigator.mediaDevices?.getUserMedia;
  if (!getUserMedia) {
    microphonePermissionStatus.dataset.state = "error";
    microphonePermissionStatus.textContent = "Microfone indisponível. Use um navegador compatível em uma conexão HTTPS segura.";
    microphonePermissionStatus.hidden = false;
    return;
  }

  microphonePermissionButton.disabled = true;
  microphonePermissionStatus.hidden = true;
  try {
    const stream = await getUserMedia.call(navigator.mediaDevices, {
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    stream.getTracks().forEach(track => track.stop());
    microphonePermissionButton.dataset.permission = "granted";
    microphonePermissionButton.setAttribute("aria-label", "Permissão de microfone concedida");
    microphonePermissionButton.title = "Permissão de microfone concedida";
    microphonePermissionStatus.dataset.state = "granted";
    microphonePermissionStatus.textContent = "Permissão concedida. O microfone só será usado para pontuar quando for sua vez.";
    microphonePermissionStatus.hidden = false;
  } catch {
    microphonePermissionStatus.dataset.state = "error";
    microphonePermissionStatus.textContent = "Não foi possível acessar o microfone. Verifique a permissão e o HTTPS.";
    microphonePermissionStatus.hidden = false;
  } finally {
    microphonePermissionButton.disabled = false;
  }
});

if (!navigator.mediaDevices?.getUserMedia) {
  microphonePermissionButton.disabled = true;
  microphonePermissionButton.setAttribute("aria-label", "Microfone requer HTTPS seguro neste aparelho");
  microphonePermissionButton.title = "Microfone requer HTTPS seguro neste aparelho";
  microphonePermissionStatus.dataset.state = "error";
  microphonePermissionStatus.textContent = "Microfone indisponível. Use um navegador compatível em uma conexão HTTPS segura.";
  microphonePermissionStatus.hidden = false;
}

function renderGroupList() {
  if (!groupDialog.open || !currentInvitation || !singer) return;
  const item = latestItems.find(request => request.id === currentInvitation.request_id);
  if (!item) { groupDialog.close(); return; }
  const content = document.createDocumentFragment();
  for (const person of partySingers.filter(person => person.id !== singer.singer_id)) {
    const vocal = item.backvocals.find(vocal => vocal.singer_id === person.id);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "group-person";
    button.disabled = !!vocal || !!currentInvitation.lead_accepted || !groupRoomReady;
    const name = document.createElement("span");
    name.textContent = person.name;
    const response = document.createElement("span");
    response.className = "response";
    if (vocal) {
      response.classList.add(vocal.joined === 1 ? "joined" : vocal.joined === 0 ? "pending" : "declined");
      response.textContent = vocal.joined === 1 ? "✓" : vocal.joined < 0 ? "×" : "";
      const responseLabel = vocal.joined === 1 && !vocal.score_eligible ? "aceitou após o início; fora da pontuação" :
        vocal.joined === 1 ? "aceitou" : vocal.joined === -1 ? "recusou" : "aguardando resposta";
      button.setAttribute("aria-label", `${person.name}: ${responseLabel}`);
    } else {
      response.textContent = "+";
      button.setAttribute("aria-label", `Convidar ${person.name}`);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/requests/${encodeURIComponent(item.id)}/invite/${encodeURIComponent(person.id)}`, { method: "POST" });
          response.textContent = "";
          response.classList.add("pending");
        } catch (problem) {
          showError(document.querySelector("#group-message"), problem.message);
          button.disabled = false;
        }
      });
    }
    button.append(name, response);
    content.append(button);
  }
  groupList.replaceChildren(content);
  if (!groupList.childNodes.length) groupList.textContent = "Nenhuma outra pessoa na festa.";
  groupStart.disabled = !groupRoomReady || !!currentInvitation.lead_accepted || !!currentSkip;
}

function promptGroupInvite(invitation, items) {
  if (!singer || !invitation) return;
  const item = items.find(request => request.id === invitation.request_id);
  const vocal = item?.backvocals.find(member => member.singer_id === singer.singer_id && member.joined === 0);
  if (!vocal || pendingGroupPrompt === item.id) return;
  pendingGroupPrompt = item.id;
  const offer = async () => {
    if (groupDialog.open) groupDialog.close();
    await confirmDialog.open({
      title: "Cantar em equipe?",
      message: `${item.singer_name} convidou você para cantar "${item.title || item.video_id}".`,
      confirmLabel: "Aceito",
      cancelLabel: "Recusar",
      onSubmit: async (accepted) => {
        if (currentInvitation?.request_id !== item.id) return true;
        try {
          await api(`/api/requests/${encodeURIComponent(item.id)}/invite/respond?accepted=${accepted}`, { method: "POST" });
          const currentItem = latestItems.find(request => request.id === item.id);
          const currentVocal = currentItem?.backvocals.find(member => member.singer_id === singer?.singer_id);
          if (currentVocal) {
            currentVocal.joined = accepted ? 1 : -1;
            currentVocal.score_eligible = accepted && !currentInvitation.accepted;
          }
          return true;
        } catch (problem) {
          const newestVocal = latestItems.find(request => request.id === item.id)?.backvocals
            .find(member => member.singer_id === singer?.singer_id);
          if (problem.message.toLocaleLowerCase().includes("convite indisponível")) return true;
          if (newestVocal?.joined !== 0) return true;
          showError(document.querySelector("#request-message"), problem.message);
          return false;
        }
      },
    });
    const currentItem = latestItems.find(request => request.id === item.id);
    const currentVocal = currentItem?.backvocals.find(member => member.singer_id === singer?.singer_id);
    if (currentVocal?.joined !== 0) {
      pendingGroupPrompt = null;
      return;
    }
  };
  if (confirmDialog.dialog.open) {
    confirmDialog.dialog.addEventListener("close", offer, { once: true });
  } else {
    offer();
  }
}

groupButton.addEventListener("click", async () => {
  document.querySelector("#group-message").textContent = "";
  const requestId = currentInvitation?.request_id;
  if (!requestId) return;
  groupRoomReady = false;
  groupDialog.showModal();
  renderGroupList();
  try {
    await api(`/api/requests/${encodeURIComponent(requestId)}/group/open`, { method: "POST" });
    if (!groupDialog.open || currentInvitation?.request_id !== requestId) return;
    groupRoomReady = true;
    joinGroupRoom(requestId);
    renderGroupList();
    startGroupRoomPolling(requestId);
  } catch (problem) {
    showError(document.querySelector("#group-message"), problem.message);
  }
});
document.querySelector("#group-close").addEventListener("click", () => groupDialog.close());
groupDialog.addEventListener("close", () => {
  const requestId = currentInvitation?.request_id;
  clearInterval(groupPollTimer);
  groupPollTimer = null;
  if (requestId && joinedGroupRooms.has(requestId) && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "leave_group", request_id: requestId }));
    joinedGroupRooms.delete(requestId);
  }
  groupRoomReady = false;
});
groupStart.addEventListener("click", async () => {
  groupStart.disabled = true;
  try {
    await api(`/api/requests/${encodeURIComponent(currentInvitation.request_id)}/accept`, { method: "POST" });
    groupDialog.close();
  } catch (problem) {
    showError(document.querySelector("#group-message"), problem.message);
    groupStart.disabled = false;
  }
});

function renderSkip(message) {
  currentSkip = message.skip;
  currentSong = message.items.find((item) => item.id === message.invitation?.request_id) || null;
  allowSkip = message.allow_skip;
  skippingView.hidden = !currentSkip;
  skipButton.disabled = !!currentSkip;
  if (currentInvitation && !currentInvitation.accepted) acceptButton.disabled = !!currentSkip;
  if (!currentSkip) {
    const skipMessage = document.querySelector("#skip-message");
    clearError(skipMessage);
    skipMessage.textContent = "";
  }
  updateSkipClock();
  updateFooter();
}

function updateFooter() {
  const showingInvitation = !invitationView.hidden;
  skipAction.hidden = showingInvitation || !currentSong || !allowSkip;
  actionFooter.hidden = !singer || (!showingInvitation && skipAction.hidden && skippingView.hidden);
}

function updateSkipClock() {
  if (!currentSkip) return;
  const seconds = Math.max(0, Math.ceil((currentSkip.deadline_ms - Date.now()) / 1000));
  document.querySelector("#skip-clock").textContent = `${seconds}s`;
}

setInterval(updateSkipClock, 250);

skipButton.addEventListener("click", async () => {
  if (!currentSong || currentSkip) return;
  const requestId = currentSong.id;
  const confirmed = await confirmDialog.open({
    title: "Pular música?",
    message: `Quer pular "${currentSong.title || currentSong.video_id}"?`,
    preview: previewCache.get(currentSong.video_id)?.task?.then(() => previewCache.get(currentSong.video_id)?.url),
    confirmLabel: "Pular música",
  });
  if (!confirmed) return;
  skipButton.disabled = true;
  try {
    await api(`/api/requests/${encodeURIComponent(requestId)}/skip`, { method: "POST" });
  } catch (problem) {
    showError(document.querySelector("#skip-message"), problem.message);
    skipButton.disabled = false;
  }
});

acceptButton.addEventListener("click", async () => {
  if (!currentInvitation || currentInvitation.accepted) return;
  acceptButton.disabled = true;
  try {
    const result = await api(`/api/requests/${encodeURIComponent(currentInvitation.request_id)}/accept`, { method: "POST" });
    currentInvitation = result.invitation;
    renderSingingOverlay(result.invitation, latestItems);
    document.querySelector("#invitation-message").textContent = "Confirmado. Aguarde a TV.";
  } catch (problem) {
    showError(document.querySelector("#invitation-message"), problem.message);
    acceptButton.disabled = false;
  }
});

entryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = entryForm.querySelector("button");
  const error = document.querySelector("#entry-error");
  button.disabled = true;
  error.hidden = true;
  try {
    if (!entryForm.elements.name.value.trim()) throw new Error("Informe seu nome.");
    const identity = await api("/api/singers", {
      method: "POST",
      body: JSON.stringify({ name: entryForm.elements.name.value.trim() }),
    });
    localStorage.setItem(storageKey, JSON.stringify(identity));
    showSession(identity);
  } catch (problem) {
    showError(error, problem.message);
  } finally {
    button.disabled = false;
  }
});

requestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = requestForm.querySelector("button");
  const message = document.querySelector("#request-message");
  button.disabled = true;
  clearError(message);
  message.textContent = "Enviando…";
  try {
    const code = youtubeCode(requestForm.elements.video.value);
    await api("/api/requests", {
      method: "POST",
      body: JSON.stringify({ youtubeCode: code }),
    });
    requestForm.reset();
    message.textContent = "Pedido recebido.";
  } catch (problem) {
    showError(message, problem.message);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#sign-out").addEventListener("click", clearSession);

try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (saved && typeof saved.name === "string" && typeof saved.token === "string") showSession(saved);
} catch {
  localStorage.removeItem(storageKey);
}
if (!singer) connectionState("Aguardando");
