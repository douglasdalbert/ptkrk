import { ConfirmDialog } from "./confirm-dialog.js";

const storageKey = "karaoke.singer";
const entry = document.querySelector("#entry");
const sessionView = document.querySelector("#session");
const connectionLabel = document.querySelector("#connection");
const entryForm = document.querySelector("#entry-form");
const requestForm = document.querySelector("#request-form");
const requestList = document.querySelector("#request-list");
const previewCache = new Map();
const invitationView = document.querySelector("#invitation");
const acceptButton = document.querySelector("#accept-button");
const confirmDialog = new ConfirmDialog();
const skippingView = document.querySelector("#skipping");
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

function connectionState(text, online = false) {
  connectionLabel.textContent = text;
  connectionLabel.classList.toggle("online", online);
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
  singer = null;
  currentParty = null;
  currentInvitation = null;
  currentSkip = null;
  currentSong = null;
  invitationView.hidden = true;
  skippingView.hidden = true;
  skipAction.hidden = true;
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
  error.textContent = "Festa encerrada. Entre novamente.";
  error.hidden = false;
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
  document.querySelector("#welcome").textContent = identity.name;
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
      connectionState("Ao vivo", true);
      renderInvitation(message.invitation, message.items);
      renderRequests(message.items);
      renderSkip(message);
    }
  });
  current.addEventListener("close", (event) => {
    if (current !== socket || !singer) return;
    socket = null;
    if (event.code === 1008) {
      partyEnded();
      return;
    }
    connectionState("Reconectando");
    reconnectTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  });
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
  document.querySelector("#request-count").textContent = String(items.length);
  document.querySelector("#empty").hidden = items.length !== 0;
  const statuses = { pending: "Em fila", processing: "Processando", ready: "Pronto", failed: "Falhou" };
  const content = document.createDocumentFragment();
  for (const item of items) {
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
    singerName.textContent = item.singer_name;
    details.append(title, singerName);
    const status = document.createElement("span");
    status.className = `status ${item.status}`;
    const state = document.createElement("span");
    state.className = "state-icon";
    state.setAttribute("aria-hidden", "true");
    state.textContent = { pending: "○", processing: "", ready: "✓", failed: "!" }[item.status] || "";
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
    if (singer && item.singer_id === singer.singer_id &&
        !(currentInvitation?.request_id === item.id && currentInvitation.accepted)) {
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
          title: "Remover música?",
          message: `Quer remover "${title.textContent}"?`,
          preview,
        });
        if (!confirmed) return;
        remove.disabled = true;
        try {
          await api(`/api/requests/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        } catch (problem) {
          document.querySelector("#request-message").textContent = problem.message;
          remove.disabled = false;
        }
      });
      row.append(remove);
    }
    content.append(row);
  }
  requestList.replaceChildren(content);
}

function renderInvitation(invitation, items) {
  if (!invitation || !singer || !items.some((item) => item.id === invitation.request_id && item.singer_id === singer.singer_id)) {
    currentInvitation = null;
    invitationView.hidden = true;
    return;
  }
  const isNew = currentInvitation?.request_id !== invitation.request_id;
  currentInvitation = invitation;
  invitationView.hidden = false;
  const item = items.find((request) => request.id === invitation.request_id);
  document.querySelector("#invitation-song").textContent = item.title || item.video_id;
  acceptButton.hidden = invitation.accepted;
  acceptButton.disabled = false;
  document.querySelector("#invitation-message").textContent = invitation.accepted ? "Confirmado. Aguarde a TV." : "";
  if (isNew && !invitation.accepted && "vibrate" in navigator) navigator.vibrate([250, 150, 250]);
}

function renderSkip(message) {
  currentSkip = message.skip;
  currentSong = message.items.find((item) => item.id === message.invitation?.request_id) || null;
  skippingView.hidden = !currentSkip;
  skipAction.hidden = !message.allow_skip || !currentSong;
  skipButton.disabled = !!currentSkip;
  if (currentInvitation && !currentInvitation.accepted) acceptButton.disabled = !!currentSkip;
  if (!currentSkip) document.querySelector("#skip-message").textContent = "";
  updateSkipClock();
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
    message: `Quer pular "${currentSong.title || currentSong.video_id}"? A próxima começa em 5 segundos.`,
    preview: previewCache.get(currentSong.video_id)?.task?.then(() => previewCache.get(currentSong.video_id)?.url),
    confirmLabel: "Pular música",
  });
  if (!confirmed) return;
  skipButton.disabled = true;
  try {
    await api(`/api/requests/${encodeURIComponent(requestId)}/skip`, { method: "POST" });
  } catch (problem) {
    document.querySelector("#skip-message").textContent = problem.message;
    skipButton.disabled = false;
  }
});

acceptButton.addEventListener("click", async () => {
  if (!currentInvitation || currentInvitation.accepted) return;
  acceptButton.disabled = true;
  try {
    await api(`/api/requests/${encodeURIComponent(currentInvitation.request_id)}/accept`, { method: "POST" });
    document.querySelector("#invitation-message").textContent = "Confirmado. Aguarde a TV.";
  } catch (problem) {
    document.querySelector("#invitation-message").textContent = problem.message;
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
    const identity = await api("/api/singers", {
      method: "POST",
      body: JSON.stringify({ name: entryForm.elements.name.value.trim() }),
    });
    localStorage.setItem(storageKey, JSON.stringify(identity));
    showSession(identity);
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

requestForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = requestForm.querySelector("button");
  const message = document.querySelector("#request-message");
  button.disabled = true;
  message.classList.remove("error");
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
    message.classList.add("error");
    message.textContent = problem.message;
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
