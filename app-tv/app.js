const show = document.querySelector("#show");
const video = document.querySelector("#video");
const overlay = document.querySelector("#video-overlay");
const playButton = document.querySelector("#play");
const error = document.querySelector("#error");
let socket;
let retryTimer;
let activeId = null;
let finishing = false;
let skip = null;
let barPosition = 1;
let colorIndex = 0;
let party = null;
const colors = ["#bda145", "#ff70ac", "#6bded0", "#c9fa45", "#f5f5ee"];

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
  video.pause();
  video.removeAttribute("src");
  video.load();
  overlay.hidden = true;
  playButton.hidden = true;
  document.querySelector("#intermission").hidden = false;
}

async function startPlayback(item) {
  activeId = item.id;
  video.src = `/api/tv/${encodeURIComponent(item.id)}/video`;
  document.querySelector("#intermission").hidden = true;
  overlay.hidden = false;
  try {
    await video.play();
    playButton.hidden = true;
  } catch {
    playButton.hidden = false;
  }
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
  const others = items.filter(item => item.id !== next?.id).slice(0, 3);
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
  document.querySelector("#next-singer").textContent = next?.singer_name || "";
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
    singer.textContent = item.singer_name;
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
  socket = current;
  current.onopen = () => current.send(JSON.stringify({token: ""}));
  current.onmessage = event => {
    document.querySelector("#connect-status").textContent = "CONECTADO";
    render(JSON.parse(event.data));
  };
  current.onclose = event => {
    if (socket !== current) return;
    document.querySelector("#connect-status").textContent = "RECONECTANDO";
    retryTimer = setTimeout(connect, 2000);
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
    qr.src = `/api/tv/qr?party=${encodeURIComponent(party)}`;
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

document.addEventListener("keydown", event => {
  if (show.hidden || event.target instanceof HTMLInputElement) return;
  if (event.ctrlKey && event.altKey && event.key.toLowerCase() === "n") {
    event.preventDefault();
    command("/api/tv/reset", {method:"POST"}).then(() => {
      stopPlayback();
      party = null;
      return command("/api/tv/state");
    }).then(render).catch(problem => { error.textContent = problem.message; });
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    barPosition = (barPosition + (event.key === "ArrowDown" ? 1 : 2)) % 3;
    document.querySelector("#track").style.top = ["7%", "48%", "88%"][barPosition];
  } else if (event.key.toLowerCase() === "c" && !event.ctrlKey && !event.altKey) {
    colorIndex = (colorIndex + 1) % colors.length;
    document.querySelector("#track").style.borderColor = colors[colorIndex];
    document.querySelector("#track").style.color = colors[colorIndex];
    document.querySelector("#track").style.backgroundColor = `${colors[colorIndex]}55`;
  }
});

setup().catch(problem => { error.textContent = problem.message; });