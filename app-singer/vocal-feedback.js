const faces = ["bad", "falling", "good", "excellent"];
export const vocalLabels = ["Vamos recuperar!", "Está se engasgando?!?", "Está indo bem...", "Dando show!"];

export function renderVocalFeedback(element, score = null, calibrationMs = 20000) {
  const calibrating = !score || score.level < 0;
  const level = Math.max(0, Math.min(3, score?.level ?? 0));
  const face = calibrating ? "waiting" : faces[level];
  const target = score?.calibration_target_ms || calibrationMs;
  const learned = Math.min(target, score?.calibration_ms || 0);
  const percent = calibrating ? learned * 100 / target : Math.max(0, Math.min(100, score.percent));
  element.dataset.state = face;
  element.querySelector("use").setAttribute("href", `/assets/vocal-pugs.svg#${face}`);
  element.querySelector(".vocal-label").textContent = calibrating ?
    `Conhecendo sua voz · ${Math.floor(learned / 1000)}/${Math.ceil(target / 1000)}s` : vocalLabels[level];
  const meter = element.querySelector("progress");
  meter.value = percent;
  meter.setAttribute("aria-label", calibrating ? "Calibração da voz" : "Solta a voz!");
  element.querySelector(".vocal-points").textContent = calibrating ? "Cante para calibrar" :
    `${Math.round(score.points).toLocaleString("pt-BR")} / ${score.ranking_max.toLocaleString("pt-BR")} pontos`;
}
