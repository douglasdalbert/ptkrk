import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const tvSource = readFileSync(new URL("../app-tv/app.js", import.meta.url), "utf8");
const singerSource = readFileSync(new URL("../app-singer/app.js", import.meta.url), "utf8");

function snapshot() {
  return {
    invitation: { request_id: "song", accepted: true, lead_accepted: true },
    items: [{
      id: "song", singer_id: "ana", singer_name: "Ana",
      backvocals: [
        { singer_id: "bia", singer_name: "Bia", joined: 1, accepted: 1, score_eligible: 1 },
        { singer_id: "caio", singer_name: "Caio", joined: 1, accepted: 1, score_eligible: 1 },
        { singer_id: "late", singer_name: "Late", joined: 1, accepted: 0, score_eligible: 0 },
        { singer_id: "pending", singer_name: "Pending", joined: 0, accepted: 0, score_eligible: 1 },
        { singer_id: "declined", singer_name: "Declined", joined: -1, accepted: 0, score_eligible: 1 },
      ],
    }],
    microphone_ready_singers: [],
  };
}

function tvContext() {
  const warning = { hidden: true };
  const names = { textContent: "" };
  const context = {
    scoreShow: null, warning, names,
    document: { querySelector: selector => selector === "#microphone-warning" ? warning : names },
  };
  runInNewContext(tvSource.slice(tvSource.indexOf("function renderMicrophoneWarning("),
    tvSource.indexOf("function render(snapshot)")), context);
  return context;
}

test("TV lists every missing microphone by name and removes each independently", () => {
  const tv = tvContext();
  const state = snapshot();
  tv.renderMicrophoneWarning(state);
  assert.equal(tv.warning.hidden, false);
  assert.equal(tv.names.textContent, "Ana + Bia + Caio");
  state.microphone_ready_singers = ["bia"];
  tv.renderMicrophoneWarning(state);
  assert.equal(tv.names.textContent, "Ana + Caio");
  state.microphone_ready_singers = ["ana", "bia", "caio"];
  tv.renderMicrophoneWarning(state);
  assert.equal(tv.warning.hidden, true);
  state.microphone_ready_singers = ["ana", "caio"];
  tv.renderMicrophoneWarning(state);
  assert.equal(tv.warning.hidden, false);
  assert.equal(tv.names.textContent, "Bia");
});

test("TV hides warning before acceptance, during skip, between songs and on final score", () => {
  const tv = tvContext();
  for (const change of [
    state => { state.invitation.accepted = false; },
    state => { state.skip = { request_id: "song" }; },
    state => { state.invitation = null; },
    state => { state.items = []; },
    () => { tv.scoreShow = {}; },
  ]) {
    const state = snapshot();
    change(state);
    tv.renderMicrophoneWarning(state);
    assert.equal(tv.warning.hidden, true);
  }
});

function singerContext() {
  const track = { readyState: "live", enabled: true, muted: false };
  const context = {
    singingOverlay: { open: true },
    currentInvitation: { request_id: "song", accepted: true },
    microphoneRequestId: "song",
    microphoneContext: { state: "running" },
    microphoneStream: { getAudioTracks: () => [track] }, track,
    WebSocket: { OPEN: 1 }, messages: [],
  };
  context.socket = { readyState: 1, send: data => context.messages.push(JSON.parse(data)) };
  runInNewContext(singerSource.slice(singerSource.indexOf("function sendMicrophoneStatus("),
    singerSource.indexOf("setInterval(sendMicrophoneStatus")), context);
  return context;
}

test("silent live capture is ready, without requiring singing or playback sync", () => {
  const singer = singerContext();
  singer.sendMicrophoneStatus();
  assert.deepEqual(singer.messages, [{ type: "microphone_status", request_id: "song", ready: true }]);
});

test("browser permission alone, previous song and interrupted capture are not ready", () => {
  for (const change of [
    singer => { singer.microphoneStream = null; },
    singer => { singer.microphoneRequestId = "previous"; },
    singer => { singer.microphoneContext.state = "suspended"; },
    singer => { singer.track.readyState = "ended"; },
    singer => { singer.track.enabled = false; },
    singer => { singer.track.muted = true; },
    singer => { singer.singingOverlay.open = false; },
  ]) {
    const singer = singerContext();
    change(singer);
    singer.sendMicrophoneStatus();
    assert.equal(singer.messages.at(-1).ready, false);
  }
});

test("reconnection republishes current capture but sends nothing while disconnected", () => {
  const singer = singerContext();
  singer.socket.readyState = 3;
  singer.sendMicrophoneStatus();
  assert.equal(singer.messages.length, 0);
  singer.socket.readyState = 1;
  singer.sendMicrophoneStatus();
  assert.equal(singer.messages.at(-1).ready, true);
  singer.currentInvitation = null;
  singer.sendMicrophoneStatus();
  assert.equal(singer.messages.length, 1);
});

function activationContext() {
  const singer = singerContext();
  const stream = singer.microphoneStream;
  const track = singer.track;
  track.events = {};
  track.addEventListener = (name, callback) => { track.events[name] = callback; };
  track.stop = () => { track.readyState = "ended"; };
  stream.getTracks = stream.getAudioTracks;
  Object.assign(singer, {
    microphoneStream: null, microphoneContext: null, microphoneRequestId: null,
    microphoneStarting: false, microphoneFrame: null, microphoneAnalyser: null,
    microphoneButton: { addEventListener: (name, callback) => { singer.activate = callback; } },
    calls: [],
    navigator: { mediaDevices: { getUserMedia: async () => { singer.calls.push("getUserMedia"); return stream; } } },
    initialAudioState: "running", resumeAllowed: true,
    setTimeout: (callback) => { callback(); return 0; },
    console: { error() {} },
    AudioContext: class {
      constructor() { this.state = singer.initialAudioState; singer.calls.push("AudioContext"); }
      resume = async () => {
        singer.calls.push("resume");
        if (singer.resumeAllowed) this.state = "running";
      };
      close() { this.state = "closed"; }
      createMediaStreamSource() { return { connect() {} }; }
      createAnalyser() { return {}; }
      addEventListener(name, callback) { this[name] = callback; }
    },
    vocalSampler: { reset() {} }, scoreStatus: {}, scoringMode: "captions",
    drawMicrophoneWaveform() {}, setMicrophoneSpeaking() {}, estimatePlaybackPosition: () => null,
    sampleMicrophone() {}, cancelAnimationFrame() {},
  });
  runInNewContext(singerSource.slice(singerSource.indexOf("function stopMicrophone("),
    singerSource.indexOf("function connectionState(")), singer);
  runInNewContext(singerSource.slice(singerSource.indexOf("function microphoneErrorMessage("),
    singerSource.indexOf('singingOverlay.addEventListener("cancel"')), singer);
  return singer;
}

test("activation reports readiness only after capture starts, and track loss restores warning", async () => {
  const singer = activationContext();
  await singer.activate();
  assert.equal(singer.microphoneButton.textContent, "Microfone ativo");
  assert.equal(singer.microphoneButton.disabled, true);
  assert.equal(singer.messages.at(-1).ready, true);
  singer.track.events.ended();
  assert.equal(singer.messages.at(-1).ready, false);
  assert.equal(singer.microphoneButton.disabled, false);
  assert.equal(singer.microphoneStream, null);
});

test("denied permission leaves microphone pending and allows retry", async () => {
  const singer = activationContext();
  singer.navigator.mediaDevices.getUserMedia = async () => {
    throw Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
  };
  await singer.activate();
  assert.equal(singer.messages.at(-1).ready, false);
  assert.equal(singer.microphoneButton.disabled, false);
  assert.match(singer.scoreStatus.textContent, /Permissão do microfone negada/);
});

test("other microphone failures name their cause", async () => {
  for (const [name, message] of [
    ["NotFoundError", /Nenhum microfone/], ["NotReadableError", /em uso por outro app/],
    ["TypeError", /\(TypeError\)/],
  ]) {
    const singer = activationContext();
    singer.navigator.mediaDevices.getUserMedia = async () => { throw Object.assign(new Error(name), { name }); };
    await singer.activate();
    assert.match(singer.scoreStatus.textContent, message);
  }
});

test("audio is resumed inside the tap, before awaiting the microphone prompt (Firefox)", async () => {
  const singer = activationContext();
  await singer.activate();
  assert.deepEqual(singer.calls.slice(0, 3), ["AudioContext", "resume", "getUserMedia"]);
});

test("a context still suspended after the prompt keeps the capture and asks for one more tap", async () => {
  const singer = activationContext();
  singer.initialAudioState = "suspended";
  singer.resumeAllowed = false;
  await singer.activate();
  assert.notEqual(singer.microphoneStream, null);
  assert.equal(singer.track.readyState, "live");
  assert.equal(singer.messages.at(-1).ready, false);
  assert.equal(singer.microphoneButton.textContent, "Retomar microfone");
  assert.equal(singer.microphoneButton.disabled, false);
  singer.resumeAllowed = true;
  await singer.activate();
  assert.equal(singer.microphoneButton.textContent, "Microfone ativo");
  assert.equal(singer.messages.at(-1).ready, true);
});

test("suspending the audio context reports not ready and offers reactivation", async () => {
  const singer = activationContext();
  await singer.activate();
  singer.microphoneContext.state = "suspended";
  singer.microphoneContext.statechange();
  assert.equal(singer.messages.at(-1).ready, false);
  assert.equal(singer.microphoneButton.disabled, false);
  assert.equal(singer.microphoneButton.textContent, "Retomar microfone");
  assert.equal(singer.track.readyState, "live");
  await singer.activate();
  assert.equal(singer.messages.at(-1).ready, true);
});

test("permission granted after song changes cannot activate the next song", async () => {
  const singer = activationContext();
  const stream = { getTracks: () => [singer.track] };
  let grant;
  singer.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
  const pending = singer.activate();
  singer.currentInvitation = { request_id: "next", accepted: true };
  grant(stream);
  await pending;
  assert.equal(singer.track.readyState, "ended");
  assert.equal(singer.microphoneStream, null);
  assert.equal(singer.microphoneStarting, false);
  assert.equal(singer.microphoneContext, null);
  assert.equal(singer.messages.length, 0);
});
