const faces = ["bad", "falling", "good", "excellent"];
export const vocalLabels = ["Vamos recuperar!", "Está se engasgando?!?", "Está indo bem...", "Dando show!"];

export const vocalMessages = {
  steady: ["Boa! Continue assim", "Sua voz está firme!", "Isso! Mantenha essa constância", "Você encontrou seu ritmo!"],
  streak: ["Você está embalando!", "Que sequência boa!", "Está sustentando bem a voz!", "Continue, está dando show!"],
  unstable: ["Tente manter a voz mais firme", "Sua voz oscilou um pouco", "Busque uma emissão mais estável"],
  quiet: ["Sua voz ficou baixa", "Tente manter o volume", "Confira a distância do microfone"],
  recovering: ["Boa, voltou ao padrão!", "Está recuperando a constância!", "Isso! A voz ficou firme de novo"],
  waiting: ["Aguardando sua voz"],
  uncertain: ["Ainda não conseguimos avaliar esse trecho"],
  calibrating: ["Cante naturalmente, estamos conhecendo sua voz"],
};

const feedbackStates = new WeakMap();

function showMessage(element, feedback) {
  const state = feedbackStates.get(element);
  if (!state || !vocalMessages[feedback]) return;
  state.pending = feedback;
  if (state.timer) return;
  const delay = Math.max(0, state.changedAt + 3000 - performance.now());
  if (delay > 0) {
    state.timer = setTimeout(() => {
      state.timer = null;
      showMessage(element, state.pending);
    }, delay);
    return;
  }
  const node = element.querySelector(".vocal-message");
  const messages = vocalMessages[feedback];
  const index = state.indices[feedback] ?? 0;
  node.textContent = messages[index % messages.length];
  node.dataset.feedback = feedback;
  state.indices[feedback] = index + 1;
  state.changedAt = performance.now();
}

export function setVocalDetectionFeedback(element, status) {
  const state = feedbackStates.get(element);
  if (!state || state.calibrating) return;
  if (status === "quiet" || status === "uncertain") {
    showMessage(element, status === "quiet" ? "waiting" : "uncertain");
  }
}

export function renderVocalFeedback(element, score = null, calibrationMs = 5000, development = false) {
  const calibrating = !score || score.level < 0;
  const level = Math.max(0, Math.min(3, score?.level ?? 0));
  const face = calibrating ? "waiting" : faces[level];
  const target = score?.calibration_target_ms || calibrationMs;
  const learned = Math.min(target, score?.calibration_ms || 0);
  const key = `${score?.request_id ?? ""}:${score?.singer_id ?? ""}`;
  let state = feedbackStates.get(element);
  if (!state || state.key !== key || (calibrating && !state.calibrating)) {
    clearTimeout(state?.timer);
    clearTimeout(state?.staleTimer);
    state = { key, indices: {}, changedAt: -Infinity, calibrating };
    feedbackStates.set(element, state);
  }
  state.calibrating = calibrating;
  const event = `${score?.calibration_ms}:${score?.evaluated_ms}:${score?.hits}:${score?.penalties}`;
  const newEvent = event !== state.event;
  const newFeedback = score?.feedback && state.feedbackEvent !== event;
  if (newEvent || newFeedback) {
    state.event = event;
    if (newFeedback) state.feedbackEvent = event;
    clearTimeout(state.staleTimer);
    if (calibrating || newFeedback || state.changedAt === -Infinity) {
      showMessage(element, calibrating ? "calibrating" : score?.feedback ?? "waiting");
    }
    if (!calibrating) {
      state.staleTimer = setTimeout(() => showMessage(element, "waiting"), 5000);
    }
  }
  element.dataset.state = face;
  element.querySelector("use").setAttribute("href", `/assets/vocal-pugs.svg#${face}`);
  element.querySelector(".vocal-label").textContent = calibrating ?
    `Conhecendo sua voz · ${Math.floor(learned / 1000)}/${Math.ceil(target / 1000)}` : vocalLabels[level];
  const meter = element.querySelector("progress");
  meter.hidden = !calibrating;
  meter.value = calibrating ? learned * 100 / target : 0;
  meter.setAttribute("aria-label", "Calibração da voz");
  const points = element.querySelector(".vocal-points");
  points.hidden = !development;
  points.textContent = calibrating ? "Cante para calibrar" :
    `${Math.round(score.points).toLocaleString("pt-BR")} / ${score.ranking_max.toLocaleString("pt-BR")} pontos`;
}
